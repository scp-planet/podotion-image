from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.support import PNG_B64, SCRIPT_PATH


def load_module():
    spec = importlib.util.spec_from_file_location("podotion_image_requests", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def mock_json_response(payload: dict):
    response = mock.MagicMock()
    response.headers = Message()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def http_error(status: int, payload: dict, *, headers: dict[str, str] | None = None):
    message = Message()
    for name, value in (headers or {}).items():
        message[name] = value
    return urllib.error.HTTPError(
        url="https://ai.podotion.com/v1/images/generations",
        code=status,
        msg="provider error",
        hdrs=message,
        fp=io.BytesIO(json.dumps(payload).encode("utf-8")),
    )


class ProviderRequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def provider(self, token: str = "sk-test-direct"):
        return self.module.ProviderConfig(
            provider_id="podotion-direct",
            name="Podotion",
            base_url="https://ai.podotion.com/v1",
            bearer_token=token,
            credential_mode="podotion_image_sk",
        )

    def post(self, provider, prompt="cat"):
        return self.module.post_provider_request(
            provider,
            "https://ai.podotion.com/v1/images/generations",
            b"{}",
            "application/json",
            prompt,
        )

    def test_http_error_redacts_direct_token_and_prompt(self) -> None:
        token = "sk-sensitive-provider-token"
        prompt = "confidential launch artwork prompt"
        error = http_error(
            400,
            {"error": {"message": f"request for {prompt} used token {token}"}},
        )
        with mock.patch.object(self.module, "_open_provider_request", side_effect=error):
            with self.assertRaises(self.module.ProviderRequestError) as raised:
                self.post(self.provider(token), prompt)
        report = raised.exception.as_json()
        self.assertNotIn(token, json.dumps(report))
        self.assertNotIn(prompt, json.dumps(report))

    def test_503_is_never_retried(self) -> None:
        error = http_error(
            503,
            {"error": {"message": "origin bad gateway", "details": {"retry_after": 60}}},
        )
        success = mock_json_response({"data": [{"b64_json": PNG_B64}]})
        with mock.patch.object(
            self.module, "_open_provider_request", side_effect=[error, success]
        ) as request, mock.patch.object(self.module.time, "sleep") as sleep:
            with self.assertRaises(self.module.ProviderRequestError) as raised:
                self.post(self.provider())
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(raised.exception.http_status, 503)
        self.assertEqual(raised.exception.attempts, 1)
        self.assertEqual(raised.exception.retry_after, 60.0)

    def test_provider_timeout_is_fixed_at_600_seconds(self) -> None:
        success = mock_json_response({"data": [{"b64_json": PNG_B64}]})
        with mock.patch.object(
            self.module, "_open_provider_request", return_value=success
        ) as request:
            self.post(self.provider())
        self.assertEqual(request.call_args.args[1], 600)

    def test_403_is_not_retried_and_has_structured_fields(self) -> None:
        error = http_error(
            403,
            {"error": {"message": "Image generation is not enabled for this group"}},
            headers={"x-request-id": "req-403", "cf-ray": "ray-403"},
        )
        with mock.patch.object(
            self.module, "_open_provider_request", side_effect=error
        ) as request, mock.patch.object(self.module.time, "sleep") as sleep:
            with self.assertRaises(self.module.ProviderRequestError) as raised:
                self.post(self.provider())
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(raised.exception.error_kind, "upstream_error")
        self.assertEqual(raised.exception.request_id, "req-403")

    def test_invalid_image_result_is_an_output_decode_error(self) -> None:
        response = {
            "data": [{"b64_json": "bad!"}],
        }
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            self.module, "load_direct_provider", return_value=self.provider()
        ), mock.patch.object(self.module, "post_images", return_value=response):
            args = SimpleNamespace(
                credential_file=None,
                prompt="cat",
                prompt_file=None,
                output_dir=temp_dir,
                size=None,
                ratio=None,
                request_key="invalid-image-0001",
                force_new=False,
            )
            with self.assertRaises(self.module.ProviderRequestError) as raised:
                self.module.run_generation(args, "generate")
            png_files = list(Path(temp_dir).glob("*.png"))
            status = self.module.get_request_status(temp_dir, "invalid-image-0001")
        self.assertEqual(raised.exception.error_kind, "output_decode_error")
        self.assertEqual(raised.exception.details["candidate_count"], 1)
        warning = raised.exception.details["invalid_candidates"][0]
        self.assertEqual(warning["result_index"], 1)
        self.assertEqual(warning["source"], "$.data[0].b64_json")
        self.assertEqual(warning["value_length"], 4)
        self.assertEqual(png_files, [])
        self.assertEqual(status["effective_status"], "completed_unusable")

    def test_state_write_failure_rolls_back_saved_image_batch(self) -> None:
        response = {"data": [{"b64_json": PNG_B64}]}
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            self.module, "load_direct_provider", return_value=self.provider()
        ), mock.patch.object(
            self.module, "post_images", return_value=response
        ), mock.patch.object(
            self.module, "write_last_state", side_effect=OSError("simulated state failure")
        ):
            args = SimpleNamespace(
                credential_file=None,
                prompt="cat",
                prompt_file=None,
                output_dir=temp_dir,
                size=None,
                ratio=None,
                request_key="state-failure-0001",
                force_new=False,
            )
            with self.assertRaises(self.module.ProviderRequestError) as raised:
                self.module.run_generation(args, "generate")
            png_files = list(Path(temp_dir).glob("*.png"))
            status = self.module.get_request_status(temp_dir, "state-failure-0001")

        self.assertEqual(raised.exception.error_kind, "output_save_error")
        self.assertEqual(png_files, [])
        self.assertEqual(status["effective_status"], "completed_unusable")

    def test_download_url_rejects_dns_name_resolving_to_loopback(self) -> None:
        resolved = [
            (
                self.module.socket.AF_INET,
                self.module.socket.SOCK_STREAM,
                6,
                "",
                ("127.0.0.1", 443),
            )
        ]
        with mock.patch.object(self.module.socket, "getaddrinfo", return_value=resolved):
            with self.assertRaisesRegex(RuntimeError, "non-public|local"):
                self.module.validate_download_url("https://cdn.example.test/generated.png")

    def test_provider_redirects_are_rejected_before_forwarding_credentials(self) -> None:
        handler = self.module._RejectProviderRedirectHandler()
        request = self.module.urllib.request.Request(
            "https://ai.podotion.com/v1/images/generations",
            headers={"Authorization": "Bearer secret"},
        )
        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://other.example.test/collect",
        )
        self.assertIsNone(redirected)


if __name__ == "__main__":
    unittest.main()

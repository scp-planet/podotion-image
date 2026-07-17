from __future__ import annotations

import importlib.util
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.support import PNG_B64, SCRIPT_PATH


def load_module():
    spec = importlib.util.spec_from_file_location("podotion_image_request_state", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RequestStateTests(unittest.TestCase):
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

    def args(self, output_dir: str, request_key: str, **overrides):
        values = {
            "credential_file": None,
            "prompt": "draw a cat",
            "prompt_file": None,
            "output_dir": output_dir,
            "size": None,
            "ratio": None,
            "image": None,
            "last": False,
            "request_key": request_key,
            "force_new": False,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def response(self):
        return {"data": [{"b64_json": PNG_B64}]}

    def test_same_request_key_reuses_success_without_second_post(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            self.module, "load_direct_provider", return_value=self.provider()
        ), mock.patch.object(
            self.module, "post_images", return_value=self.response()
        ) as post:
            args = self.args(temp_dir, "same-key-0001")
            first = self.module.run_generation(args, "generate")
            second = self.module.run_generation(args, "generate")

        self.assertEqual(post.call_count, 1)
        self.assertFalse(first["request"]["reused"])
        self.assertTrue(second["request"]["reused"])
        self.assertEqual(first["images"], second["images"])

    def test_state_scope_isolates_same_key_in_shared_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            self.module, "load_direct_provider", return_value=self.provider()
        ), mock.patch.object(
            self.module, "post_images", return_value=self.response()
        ) as post:
            first = self.module.run_generation(
                self.args(
                    temp_dir,
                    "shared-key-0001",
                    state_scope="task/one",
                ),
                "generate",
            )
            second = self.module.run_generation(
                self.args(
                    temp_dir,
                    "shared-key-0001",
                    state_scope="task two",
                ),
                "generate",
            )
            first_status = self.module.get_request_status(
                temp_dir,
                "shared-key-0001",
                {"CODEX_THREAD_ID": "task/one"},
            )
            second_status = self.module.get_request_status(
                temp_dir,
                "shared-key-0001",
                {"CODEX_THREAD_ID": "task two"},
            )
            first_last = self.module.read_last_image(
                Path(temp_dir), {"CODEX_THREAD_ID": "task/one"}
            )
            second_last = self.module.read_last_image(
                Path(temp_dir), {"CODEX_THREAD_ID": "task two"}
            )

        self.assertEqual(post.call_count, 2)
        self.assertNotEqual(first["request"]["fingerprint"], second["request"]["fingerprint"])
        self.assertNotEqual(first["state_path"], second["state_path"])
        self.assertEqual(first_status["request"]["state_scope"], "task_one")
        self.assertEqual(second_status["request"]["state_scope"], "task_two")
        self.assertEqual(first_last, Path(first["images"][0]["path"]))
        self.assertEqual(second_last, Path(second["images"][0]["path"]))

    def test_recent_fingerprint_reuses_success_but_force_new_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            self.module, "load_direct_provider", return_value=self.provider()
        ), mock.patch.object(
            self.module, "post_images", return_value=self.response()
        ) as post:
            self.module.run_generation(
                self.args(temp_dir, "fingerprint-0001"), "generate"
            )
            reused = self.module.run_generation(
                self.args(temp_dir, "fingerprint-0002"), "generate"
            )
            fresh = self.module.run_generation(
                self.args(temp_dir, "fingerprint-0003", force_new=True), "generate"
            )

        self.assertEqual(post.call_count, 2)
        self.assertTrue(reused["request"]["reused"])
        self.assertEqual(reused["request"]["upstream_attempts"], 0)
        self.assertFalse(fresh["request"]["reused"])

    def test_503_becomes_unknown_and_blocks_new_key(self) -> None:
        upstream = self.module.ProviderRequestError(
            "provider request failed with HTTP 503",
            error_kind="upstream_error",
            http_status=503,
            attempts=1,
        )
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            self.module, "load_direct_provider", return_value=self.provider()
        ), mock.patch.object(
            self.module, "post_images", side_effect=[upstream, self.response()]
        ) as post:
            with self.assertRaises(self.module.ProviderRequestError):
                self.module.run_generation(
                    self.args(temp_dir, "unknown-503-0001"), "generate"
                )
            status = self.module.get_request_status(temp_dir, "unknown-503-0001")
            with self.assertRaises(self.module.ProviderRequestError) as blocked:
                self.module.run_generation(
                    self.args(temp_dir, "unknown-503-0002", force_new=True), "generate"
                )

        self.assertEqual(post.call_count, 1)
        self.assertEqual(status["effective_status"], "outcome_unknown")
        self.assertEqual(blocked.exception.error_kind, "request_outcome_unknown")

    def test_403_is_recorded_as_definitive_failure(self) -> None:
        upstream = self.module.ProviderRequestError(
            "not enabled",
            error_kind="upstream_error",
            http_status=403,
            attempts=1,
        )
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            self.module, "load_direct_provider", return_value=self.provider()
        ), mock.patch.object(
            self.module, "post_images", side_effect=upstream
        ) as post:
            with self.assertRaises(self.module.ProviderRequestError):
                self.module.run_generation(
                    self.args(temp_dir, "definitive-403-0001"), "generate"
                )
            status = self.module.get_request_status(temp_dir, "definitive-403-0001")
            with self.assertRaises(self.module.ProviderRequestError):
                self.module.run_generation(
                    self.args(temp_dir, "definitive-403-0001"), "generate"
                )

        self.assertEqual(post.call_count, 1)
        self.assertEqual(status["effective_status"], "failed_definitive")

    def test_abandon_requires_acknowledgement_then_allows_new_key(self) -> None:
        upstream = self.module.ProviderRequestError(
            "connection ended",
            error_kind="upstream_error",
            attempts=1,
        )
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            self.module, "load_direct_provider", return_value=self.provider()
        ), mock.patch.object(
            self.module, "post_images", side_effect=[upstream, self.response()]
        ) as post:
            with self.assertRaises(self.module.ProviderRequestError):
                self.module.run_generation(
                    self.args(temp_dir, "abandon-me-0001"), "generate"
                )
            with self.assertRaises(ValueError):
                self.module.abandon_request(temp_dir, "abandon-me-0001", False)
            abandoned = self.module.abandon_request(
                temp_dir, "abandon-me-0001", True
            )
            result = self.module.run_generation(
                self.args(temp_dir, "after-abandon-0001", force_new=True), "generate"
            )

        self.assertEqual(abandoned["status"], "abandoned")
        self.assertTrue(result["ok"])
        self.assertEqual(post.call_count, 2)

    def test_interrupted_submission_stays_unknown_and_is_not_resent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            self.module, "load_direct_provider", return_value=self.provider()
        ), mock.patch.object(
            self.module, "post_images", side_effect=KeyboardInterrupt()
        ) as post:
            with self.assertRaises(KeyboardInterrupt):
                self.module.run_generation(
                    self.args(temp_dir, "interrupted-0001"), "generate"
                )
            status = self.module.get_request_status(temp_dir, "interrupted-0001")
            with self.assertRaises(self.module.ProviderRequestError):
                self.module.run_generation(
                    self.args(temp_dir, "interrupted-0002"), "generate"
                )

        self.assertEqual(post.call_count, 1)
        self.assertEqual(status["stored_status"], "submitted")
        self.assertEqual(status["effective_status"], "outcome_unknown")

    def test_prepared_is_persisted_before_submitted(self) -> None:
        statuses: list[str] = []
        original_write = self.module._atomic_write_json

        def capture(path, value):
            statuses.append(str(value.get("status")))
            original_write(path, value)

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            self.module, "load_direct_provider", return_value=self.provider()
        ), mock.patch.object(
            self.module, "post_images", return_value=self.response()
        ), mock.patch.object(self.module, "_atomic_write_json", side_effect=capture):
            self.module.run_generation(
                self.args(temp_dir, "transition-order-0001"), "generate"
            )

        self.assertGreaterEqual(len(statuses), 3)
        self.assertEqual(statuses[:2], ["prepared", "submitted"])
        self.assertEqual(statuses[-1], "succeeded")

    def test_equivalent_concurrent_request_is_rejected_before_post(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        first_result: list[dict] = []
        first_error: list[BaseException] = []

        def slow_post(*_args, **_kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test synchronization timed out")
            return self.response()

        def run_first(temp_dir: str) -> None:
            try:
                first_result.append(
                    self.module.run_generation(
                        self.args(temp_dir, "concurrent-0001"), "generate"
                    )
                )
            except BaseException as exc:
                first_error.append(exc)

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            self.module, "load_direct_provider", return_value=self.provider()
        ), mock.patch.object(self.module, "post_images", side_effect=slow_post) as post:
            thread = threading.Thread(target=run_first, args=(temp_dir,))
            thread.start()
            self.assertTrue(entered.wait(5))
            try:
                status = self.module.get_request_status(temp_dir, "concurrent-0001")
                self.assertEqual(status["effective_status"], "request_in_progress")
                with self.assertRaises(self.module.ProviderRequestError) as blocked:
                    self.module.run_generation(
                        self.args(temp_dir, "concurrent-0002"), "generate"
                    )
                self.assertEqual(blocked.exception.error_kind, "request_in_progress")
            finally:
                release.set()
                thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(first_error, [])
        self.assertEqual(len(first_result), 1)
        self.assertEqual(post.call_count, 1)

    def test_request_state_never_contains_prompt_token_or_image_payload(self) -> None:
        prompt = "private prompt text that must not be stored"
        token = "sk-private-token-that-must-not-be-stored"
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            self.module, "load_direct_provider", return_value=self.provider(token)
        ), mock.patch.object(
            self.module, "post_images", return_value=self.response()
        ):
            self.module.run_generation(
                self.args(temp_dir, "redaction-0001", prompt=prompt), "generate"
            )
            state_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in Path(temp_dir).glob(".state/**/*.json")
            )

        self.assertNotIn(prompt, state_text)
        self.assertNotIn(token, state_text)
        self.assertNotIn(PNG_B64, state_text)


if __name__ == "__main__":
    unittest.main()

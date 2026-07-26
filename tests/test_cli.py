from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.support import (
    FakeProviderServer,
    PNG_B64,
    PNG_BYTES,
    SCRIPT_PATH,
    images_response,
    parse_cli_json,
    parse_multipart_request,
    run_cli,
)


def load_module():
    spec = importlib.util.spec_from_file_location("podotion_image_cli", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CliIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def provider(
        self,
        base_url: str,
        token: str = "sk-test-direct",
        token_4k: str | None = None,
    ):
        return self.module.ProviderConfig(
            provider_id="podotion-direct",
            name="Podotion",
            base_url=base_url,
            bearer_token=token,
            credential_mode="podotion_image_sk",
            bearer_token_4k=token_4k,
        )

    def args(self, **overrides):
        values = {
            "credential_file": None,
            "prompt": "draw a cat",
            "prompt_file": None,
            "output_dir": None,
            "size": None,
            "ratio": None,
            "image": None,
            "last": False,
            "image_probe": False,
            "request_key": "test-request-0001",
            "force_new": False,
            "state_scope": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_generate_defaults_to_images_api_and_json_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, FakeProviderServer(
            [images_response()]
        ) as server:
            output = Path(temp_dir) / "output"
            provider = self.provider(server.base_url)
            with mock.patch.object(self.module, "load_direct_provider", return_value=provider):
                report = self.module.run_generation(
                    self.args(
                        output_dir=str(output),
                        size="2k",
                        ratio="9:16",
                    ),
                    "generate",
                )
            saved = Path(report["images"][0]["path"])
            saved_bytes = saved.read_bytes()
            state_exists = Path(report["state_path"]).is_file()

        request = server.requests[0]
        self.assertEqual(request["path"], "/v1/images/generations")
        self.assertEqual(request["authorization"], "Bearer sk-test-direct")
        self.assertEqual(request["body"]["model"], "gpt-image-2")
        self.assertEqual(request["body"]["prompt"], "draw a cat")
        self.assertEqual(request["body"]["size"], "1152x2048")
        self.assertEqual(request["body"]["quality"], "auto")
        self.assertEqual(request["body"]["output_format"], "png")
        self.assertEqual(request["body"]["n"], 1)
        self.assertNotIn("response_format", request["body"])
        self.assertEqual(report["provider"]["credential_mode"], "podotion_image_sk")
        self.assertEqual(report["credential_profile"], "default")
        self.assertEqual(report["request"]["transport"], "images")
        self.assertEqual(report["request"]["size"], "1152x2048")
        self.assertEqual(report["request"]["provider_timeout_seconds"], 600)
        self.assertEqual(report["request"]["upstream_attempts"], 1)
        self.assertEqual(report["warnings"][0]["code"], "image_size_mismatch")
        self.assertEqual(report["warnings"][0]["requested_size"], "1152x2048")
        self.assertEqual(report["warnings"][0]["actual_size"], "1x1")
        self.assertTrue(state_exists)
        self.assertEqual(saved_bytes, PNG_BYTES)

    def test_4k_tier_uses_4k_credential_and_preserves_resolution_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, FakeProviderServer(
            [images_response()]
        ) as server:
            provider = self.provider(
                server.base_url, "sk-default-route", "sk-4k-route"
            )
            with mock.patch.object(
                self.module, "load_direct_provider", return_value=provider
            ):
                report = self.module.run_generation(
                    self.args(output_dir=temp_dir, size="4k", ratio="16:9"),
                    "generate",
                )

        self.assertEqual(server.requests[0]["authorization"], "Bearer sk-4k-route")
        self.assertEqual(server.requests[0]["body"]["size"], "3840x2160")
        self.assertEqual(report["credential_profile"], "4k")
        self.assertEqual(report["provider"]["credential_profile"], "4k")
        self.assertEqual(report["request"]["resolved_size"]["requested_tier"], "4k")
        self.assertEqual(report["request"]["resolved_size"]["width"], 3840)

    def test_intermediate_exact_size_uses_default_credential(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, FakeProviderServer(
            [images_response()]
        ) as server:
            provider = self.provider(
                server.base_url, "sk-default-route", "sk-4k-route"
            )
            with mock.patch.object(
                self.module, "load_direct_provider", return_value=provider
            ):
                report = self.module.run_generation(
                    self.args(output_dir=temp_dir, size="2560x1440"),
                    "generate",
                )

        self.assertEqual(server.requests[0]["authorization"], "Bearer sk-default-route")
        self.assertEqual(server.requests[0]["body"]["size"], "2560x1440")
        self.assertEqual(report["credential_profile"], "default")
        self.assertIsNone(report["request"]["resolved_size"]["requested_tier"])

    def test_exact_canonical_4k_size_uses_4k_credential(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, FakeProviderServer(
            [images_response()]
        ) as server:
            provider = self.provider(
                server.base_url, "sk-default-route", "sk-4k-route"
            )
            with mock.patch.object(
                self.module, "load_direct_provider", return_value=provider
            ):
                report = self.module.run_generation(
                    self.args(output_dir=temp_dir, size="2336x3504"),
                    "generate",
                )

        self.assertEqual(server.requests[0]["authorization"], "Bearer sk-4k-route")
        self.assertEqual(report["credential_profile"], "4k")

    def test_max_pixel_noncanonical_size_uses_4k_credential(self) -> None:
        resolved = self.module.resolve_request_size("3600x2304", None)
        self.assertNotIn(
            (resolved.width, resolved.height), self.module.CANONICAL_4K_DIMENSIONS
        )
        self.assertEqual(resolved.pixels, self.module.MAX_PIXELS)
        self.assertEqual(resolved.credential_profile, "4k")

    def test_missing_4k_credential_has_no_output_state_or_network_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "must-not-exist"
            provider = self.provider("https://ai.podotion.com/v1")
            with mock.patch.object(
                self.module, "load_direct_provider", return_value=provider
            ), mock.patch.object(
                self.module, "resolve_output_dir", wraps=self.module.resolve_output_dir
            ) as resolve_output, mock.patch.object(
                self.module, "post_images"
            ) as post:
                with self.assertRaisesRegex(RuntimeError, "PodotionImage4kSk"):
                    self.module.run_generation(
                        self.args(
                            output_dir=str(output), size="4k", ratio="1:1"
                        ),
                        "generate",
                    )

            resolve_output.assert_not_called()
            post.assert_not_called()
            self.assertFalse(output.exists())

    def test_last_state_drives_multipart_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, FakeProviderServer(
            [images_response(), images_response()]
        ) as server:
            output = Path(temp_dir) / "output"
            provider = self.provider(server.base_url)
            with mock.patch.object(self.module, "load_direct_provider", return_value=provider):
                self.module.run_generation(
                    self.args(output_dir=str(output)),
                    "generate",
                )
                report = self.module.run_generation(
                    self.args(
                        prompt="give it a blue scarf",
                        output_dir=str(output),
                        last=True,
                        request_key="test-request-0002",
                    ),
                    "edit",
                )

        request = server.requests[1]
        self.assertEqual(request["path"], "/v1/images/edits")
        self.assertIn("boundary=", request["content_type"])
        parts = parse_multipart_request(request)
        fields = {
            part["name"]: part["data"].decode("utf-8")
            for part in parts
            if part["filename"] is None
        }
        images = [part for part in parts if part["name"] == "image[]"]
        self.assertEqual(fields["model"], "gpt-image-2")
        self.assertEqual(fields["prompt"], "give it a blue scarf")
        self.assertEqual(fields["n"], "1")
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["data"], PNG_BYTES)
        self.assertEqual(report["request"]["input_image_count"], 1)

    def test_multiple_response_items_are_completed_unusable_without_png(self) -> None:
        response = {
            "data": [
                {"b64_json": PNG_B64},
                {"result": "completed metadata, not an image"},
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir, FakeProviderServer([response]) as server:
            output = Path(temp_dir) / "output"
            provider = self.provider(server.base_url)
            with mock.patch.object(self.module, "load_direct_provider", return_value=provider):
                with self.assertRaises(self.module.ProviderRequestError) as raised:
                    self.module.run_generation(
                        self.args(output_dir=str(output), state_scope="single-response"),
                        "generate",
                    )
            record_path = (
                output
                / ".state"
                / "single-response"
                / "requests"
                / "test-request-0001.json"
            )
            record = json.loads(record_path.read_text(encoding="utf-8"))
            png_files = list(output.rglob("*.png"))
            request_count = len(server.requests)

        self.assertEqual(raised.exception.error_kind, "output_decode_error")
        self.assertIn("exactly one image", str(raised.exception))
        self.assertEqual(record["status"], "completed_unusable")
        self.assertEqual(record["failure"]["error_kind"], "output_decode_error")
        self.assertEqual(request_count, 1)
        self.assertEqual(png_files, [])

    def test_default_output_directory_is_under_current_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            expected = workspace.resolve() / "PodotionImageOutput"

            self.assertEqual(self.module.default_output_dir(workspace), expected)
            self.assertEqual(self.module.resolve_output_dir(None, cwd=workspace), expected)

    def test_explicit_output_directory_keeps_priority_over_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            explicit = root / "chosen-output"
            workspace = root / "workspace"

            resolved = self.module.resolve_output_dir(str(explicit), cwd=workspace)

        self.assertEqual(resolved, explicit.resolve())

    def test_last_state_is_scoped_by_sanitized_thread_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "output"
            image_path = output / "generated.png"
            output.mkdir(parents=True)
            image_path.write_bytes(PNG_BYTES)
            saved = self.module.SavedImage(
                path=image_path.resolve(),
                mime_type="image/png",
                bytes=len(PNG_BYTES),
                width=1,
                height=1,
            )

            first_state = self.module.write_last_state(
                output,
                [saved],
                "generate",
                "1024x1024",
                {"CODEX_THREAD_ID": "../thread one"},
            )
            second_state = self.module.write_last_state(
                output,
                [saved],
                "generate",
                "1024x1024",
                {"CODEX_THREAD_ID": "thread-two"},
            )

        self.assertEqual(
            first_state.relative_to(output.resolve()),
            Path(".state") / "thread_one" / "last.json",
        )
        self.assertEqual(
            second_state.relative_to(output.resolve()),
            Path(".state") / "thread-two" / "last.json",
        )
        self.assertNotEqual(first_state, second_state)

    def test_missing_thread_id_uses_unscoped_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "output"
            state_path = self.module._state_path(output, {})

        self.assertEqual(
            state_path.relative_to(output.resolve()),
            Path(".state") / "unscoped" / "last.json",
        )

    def test_read_last_does_not_fall_back_to_legacy_root_state(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "output"
            output.mkdir()
            image_path = output / "legacy.png"
            image_path.write_bytes(PNG_BYTES)
            (output / "last.json").write_text(
                json.dumps({"images": [{"path": str(image_path)}]}),
                encoding="utf-8",
            )

            with self.assertRaises(FileNotFoundError) as raised:
                self.module.read_last_image(output, {"CODEX_THREAD_ID": "new-thread"})

        self.assertIn(".state", str(raised.exception))
        self.assertIn("new-thread", str(raised.exception))

    def test_doctor_reports_only_direct_credential_metadata(self) -> None:
        with FakeProviderServer([]) as server:
            provider = self.provider(server.base_url, "sk-doctor-hidden")
            with mock.patch.object(self.module, "load_direct_provider", return_value=provider):
                report = self.module.run_doctor(self.args())

        self.assertEqual(report["provider_id"], "podotion-direct")
        self.assertEqual(report["credential_mode"], "podotion_image_sk")
        self.assertNotIn("wire_api", report)
        self.assertNotIn("sk-doctor-hidden", repr(report))
        self.assertFalse(report["image_capability"]["attempted"])
        self.assertFalse(any(request["method"] == "POST" for request in server.requests))

    def test_doctor_probes_every_configured_credential_without_posting(self) -> None:
        with FakeProviderServer([]) as server:
            provider = self.provider(
                server.base_url, "sk-doctor-default", "sk-doctor-4k"
            )
            with mock.patch.object(
                self.module, "load_direct_provider", return_value=provider
            ):
                report = self.module.run_doctor(self.args())

        get_requests = [item for item in server.requests if item["method"] == "GET"]
        self.assertEqual(len(get_requests), 2)
        self.assertEqual(
            {item["authorization"] for item in get_requests},
            {"Bearer sk-doctor-default", "Bearer sk-doctor-4k"},
        )
        self.assertTrue(report["credential_profiles"]["default"]["reachable"])
        self.assertTrue(report["credential_profiles"]["4k"]["reachable"])
        self.assertEqual(report["warnings"], [])
        self.assertNotIn("sk-doctor", repr(report))
        self.assertFalse(any(request["method"] == "POST" for request in server.requests))

    def test_doctor_missing_optional_4k_is_only_a_warning(self) -> None:
        with FakeProviderServer([]) as server:
            provider = self.provider(server.base_url)
            with mock.patch.object(
                self.module, "load_direct_provider", return_value=provider
            ):
                report = self.module.run_doctor(self.args())

        self.assertTrue(report["ok"])
        self.assertFalse(report["credential_profiles"]["4k"]["configured"])
        self.assertEqual(report["warnings"][0]["code"], "optional_4k_credential_missing")
        self.assertEqual(sum(item["method"] == "GET" for item in server.requests), 1)

    def test_doctor_image_probe_makes_one_billable_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, FakeProviderServer(
            [images_response()]
        ) as server:
            provider = self.provider(server.base_url)
            with mock.patch.object(self.module, "load_direct_provider", return_value=provider), mock.patch.object(
                self.module,
                "default_output_dir",
                return_value=Path(temp_dir) / "runtime-output",
            ):
                report = self.module.run_doctor(self.args(image_probe=True))

        self.assertTrue(report["ok"])
        self.assertTrue(report["image_capability"]["attempted"])
        self.assertTrue(report["image_capability"]["may_bill"])
        self.assertEqual(report["image_capability"]["max_attempts"], 1)
        self.assertEqual(sum(request["method"] == "POST" for request in server.requests), 1)

    def test_cli_sizes_and_help_do_not_require_credentials(self) -> None:
        report = parse_cli_json(run_cli(["sizes"]))
        help_result = run_cli(["--help"])
        self.assertTrue(report["ok"])
        self.assertIn("--credential-file", help_result.stdout)
        self.assertNotIn("--transport", help_result.stdout)
        self.assertNotIn("--config", help_result.stdout)
        self.assertNotIn("provider-source", help_result.stdout)

    def test_cli_missing_credential_file_is_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing.toml"
            result = run_cli(
                ["--credential-file", str(missing), "doctor"],
                check=False,
            )
        import json

        report = json.loads(result.stderr)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(report["ok"])
        self.assertIn("configure_direct.py", report["error"]["message"])

    def test_generation_timeout_and_retries_are_not_user_configurable(self) -> None:
        timeout = run_cli(["--timeout", "1", "sizes"], check=False)
        retries = run_cli(["--max-retries", "2", "sizes"], check=False)
        self.assertNotEqual(timeout.returncode, 0)
        self.assertNotEqual(retries.returncode, 0)
        help_result = run_cli(["generate", "--help"])
        self.assertNotIn("--timeout", help_result.stdout)
        self.assertNotIn("--max-retries", help_result.stdout)
        missing = run_cli(["generate"], check=False)
        self.assertNotEqual(missing.returncode, 0)

    def test_cli_rejects_ambiguous_size_arguments_before_loading_credentials(self) -> None:
        tier_without_ratio = run_cli(
            [
                "generate", "--prompt", "cat", "--request-key", "size-args-0001",
                "--size", "2k",
            ],
            check=False,
        )
        exact_with_ratio = run_cli(
            [
                "generate", "--prompt", "cat", "--request-key", "size-args-0002",
                "--size", "2560x1440", "--ratio", "16:9",
            ],
            check=False,
        )
        self.assertIn("--ratio is required", tier_without_ratio.stderr)
        self.assertIn("--ratio cannot be used", exact_with_ratio.stderr)
        self.assertNotIn("credential file not found", tier_without_ratio.stderr)
        self.assertNotIn("credential file not found", exact_with_ratio.stderr)

    def test_edit_help_has_last_but_no_mask(self) -> None:
        result = run_cli(["edit", "--help"])
        self.assertIn("--last", result.stdout)
        self.assertNotIn("--mask", result.stdout)

    def test_request_commands_expose_state_scope(self) -> None:
        for command in ("generate", "edit", "request-status", "request-abandon"):
            with self.subTest(command=command):
                result = run_cli([command, "--help"])
                self.assertIn("--state-scope", result.stdout)


if __name__ == "__main__":
    unittest.main()

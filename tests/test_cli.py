from __future__ import annotations

import importlib.util
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

    def provider(self, base_url: str, token: str = "sk-test-direct"):
        return self.module.ProviderConfig(
            provider_id="podotion-direct",
            name="Podotion",
            base_url=base_url,
            bearer_token=token,
            credential_mode="podotion_image_sk",
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
        self.assertEqual(report["request"]["transport"], "images")
        self.assertEqual(report["request"]["size"], "1152x2048")
        self.assertEqual(report["request"]["provider_timeout_seconds"], 600)
        self.assertEqual(report["request"]["upstream_attempts"], 1)
        self.assertEqual(report["warnings"][0]["code"], "image_size_mismatch")
        self.assertEqual(report["warnings"][0]["requested_size"], "1152x2048")
        self.assertEqual(report["warnings"][0]["actual_size"], "1x1")
        self.assertTrue(state_exists)
        self.assertEqual(saved_bytes, PNG_BYTES)

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
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["data"], PNG_BYTES)
        self.assertEqual(report["request"]["input_image_count"], 1)

    def test_text_result_after_valid_image_does_not_fail_or_create_orphan(self) -> None:
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
                report = self.module.run_generation(
                    self.args(output_dir=str(output)),
                    "generate",
                )
            output_files = sorted(path.name for path in output.iterdir())

        self.assertTrue(report["ok"])
        self.assertEqual(len(report["images"]), 1)
        self.assertNotIn("_01", Path(report["images"][0]["path"]).name)
        self.assertEqual(report["warnings"], [])
        self.assertEqual(len(output_files), 2)
        self.assertIn(".state", output_files)
        self.assertEqual(Path(report["state_path"]).name, "last.json")

    def test_default_output_directory_is_under_current_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            expected = workspace.resolve() / "PodotionImage"

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

    def test_edit_help_has_last_but_no_mask(self) -> None:
        result = run_cli(["edit", "--help"])
        self.assertIn("--last", result.stdout)
        self.assertNotIn("--mask", result.stdout)


if __name__ == "__main__":
    unittest.main()

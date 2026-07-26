from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from tests.support import SKILL_ROOT


CONFIGURE_PATH = SKILL_ROOT / "scripts" / "configure_direct.py"


def load_module():
    spec = importlib.util.spec_from_file_location("configure_direct_test", CONFIGURE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {CONFIGURE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(CONFIGURE_PATH.parent))
    try:
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


class ConfigureDirectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def run_command(
        self,
        target: Path,
        *extra: str,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(CONFIGURE_PATH),
                "--credential-file",
                str(target),
                *extra,
            ],
            input=stdin,
            text=True,
            capture_output=True,
            check=False,
        )

    def run_cli(self, target: Path, stdin: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.run_command(target, "--stdin", *extra, stdin=stdin)

    def test_render_default_only_is_backward_compatible(self) -> None:
        secret = 'sk-value-with-"quote"-and-\\slash'
        rendered = self.module.render_config(secret)
        parsed = tomllib.loads(rendered)
        self.assertEqual(parsed["PodotionImageSk"], secret)
        self.assertEqual(parsed["base_url"], "https://ai.podotion.com/v1")
        self.assertNotIn("PodotionImage4kSk", parsed)
        self.assertNotIn("__PODOTION_IMAGE", rendered)

    def test_render_writes_both_credentials(self) -> None:
        parsed = tomllib.loads(self.module.render_config("sk-default", "sk-4k"))
        self.assertEqual(parsed["PodotionImageSk"], "sk-default")
        self.assertEqual(parsed["PodotionImage4kSk"], "sk-4k")

    def test_render_rejects_non_string_4k_credential(self) -> None:
        with self.assertRaisesRegex(ValueError, "TOML string"):
            self.module.render_config("sk-default", 123)

    def test_invalid_secrets_are_rejected_without_echo(self) -> None:
        values = (
            "",
            "__PODOTION_IMAGE_SK__",
            "__PODOTION_IMAGE_4K_SK__",
            "{{PodotionImageSk}}",
            "{{sk-real-looking-secret}}",
            "line-one\nline-two",
            123,
            ["sk-list"],
        )
        for value in values:
            with self.subTest(value=type(value).__name__), self.assertRaises(ValueError) as caught:
                self.module.validate_secret(value)
            if isinstance(value, str) and value:
                self.assertNotIn(value, str(caught.exception))

    def test_secret_size_limit_uses_utf8_bytes(self) -> None:
        accepted = "x" * self.module.MAX_SECRET_BYTES
        rejected = "\u754c" * ((self.module.MAX_SECRET_BYTES // 3) + 1)
        self.assertEqual(self.module.validate_secret(accepted), accepted)
        with self.assertRaisesRegex(ValueError, "64 KB") as caught:
            self.module.validate_secret(rejected)
        self.assertNotIn(rejected, str(caught.exception))

    def test_template_requires_exact_string_placeholders_and_endpoint(self) -> None:
        templates = (
            'base_url = "https://ai.podotion.com/v1"\nPodotionImageSk = 1\nPodotionImage4kSk = "__PODOTION_IMAGE_4K_SK__"\n',
            'base_url = "https://example.test/v1"\nPodotionImageSk = "__PODOTION_IMAGE_SK__"\nPodotionImage4kSk = "__PODOTION_IMAGE_4K_SK__"\n',
            'base_url = "https://ai.podotion.com/v1"\nPodotionImageSk = "__PODOTION_IMAGE_SK__"\nPodotionImage4kSk = "wrong"\n',
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "provider.toml"
            for template in templates:
                path.write_text(template, encoding="utf-8")
                with self.subTest(template=template), self.assertRaises(RuntimeError):
                    self.module.render_config("sk-default", "sk-4k", path)

    def test_stdin_reader_supports_text_stream_embedders(self) -> None:
        original = self.module.sys.stdin
        try:
            self.module.sys.stdin = io.StringIO("sk-default\nsk-4k\n")
            self.assertEqual(
                self.module._read_credentials_from_stdin(),
                ("sk-default", "sk-4k"),
            )
        finally:
            self.module.sys.stdin = original

    def test_private_write_is_atomic_and_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp" if os.name != "nt" else None) as temp_dir:
            target = Path(temp_dir) / "runtime" / "provider.toml"
            result = self.module.write_private_config(
                target, self.module.render_config("sk-private", "sk-private-4k")
            )
            leftovers = list(target.parent.glob(".provider.toml.*.tmp"))
            mode = stat.S_IMODE(target.stat().st_mode)
        self.assertEqual(result, target.resolve())
        self.assertEqual(leftovers, [])
        if os.name != "nt":
            self.assertEqual(mode, 0o600)

    def test_existing_config_requires_force(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "provider.toml"
            target.write_text("old", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                self.module.write_private_config(target, "new")
            self.module.write_private_config(target, "new", force=True)
            self.assertEqual(target.read_text(encoding="utf-8"), "new")

    def test_failed_replace_preserves_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "provider.toml"
            target.write_text("old", encoding="utf-8")
            with mock.patch.object(self.module.os, "replace", side_effect=OSError("failed")):
                with self.assertRaises(OSError):
                    self.module.write_private_config(target, "new", force=True)
            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            self.assertEqual(list(target.parent.glob(".provider.toml.*.tmp")), [])

    def test_stdin_cli_accepts_legacy_single_line_without_leak(self) -> None:
        secret = "sk-cli-never-print-this"
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "provider.toml"
            result = self.run_cli(target, secret + "\n", "--force")
            report = json.loads(result.stdout)
            saved = tomllib.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["credential_profiles"], {"default": True, "4k": False})
        self.assertEqual(saved["PodotionImageSk"], secret)
        self.assertNotIn("PodotionImage4kSk", saved)
        self.assertNotIn(secret, result.stdout + result.stderr)

    def test_legacy_raw_stdin_secret_may_contain_equals(self) -> None:
        secret = "sk-legacy=with=equals"
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "provider.toml"
            result = self.run_cli(target, secret + "\n", "--force")
            saved = tomllib.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(saved["PodotionImageSk"], secret)
        self.assertNotIn(secret, result.stdout + result.stderr)

    def test_stdin_cli_accepts_two_lines_without_leaks(self) -> None:
        default_secret = "sk-default-never-print"
        four_k_secret = "sk-4k-never-print"
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "provider.toml"
            result = self.run_cli(
                target, f"{default_secret}\n{four_k_secret}\n", "--force"
            )
            saved = tomllib.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(saved["PodotionImageSk"], default_secret)
        self.assertEqual(saved["PodotionImage4kSk"], four_k_secret)
        self.assertNotIn(default_secret, result.stdout + result.stderr)
        self.assertNotIn(four_k_secret, result.stdout + result.stderr)

    def test_assignment_stdin_accepts_bom_blanks_any_order_and_equals_in_values(self) -> None:
        default_secret = "sk-default=with=equals"
        four_k_secret = "sk-4k=with=equals"
        source = (
            "\ufeff\n"
            f"PodotionImage4kSk={four_k_secret}\n"
            "\n"
            f"PodotionImageSk={default_secret}\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "provider.toml"
            result = self.run_cli(target, source, "--force")
            saved = tomllib.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(saved["PodotionImageSk"], default_secret)
        self.assertEqual(saved["PodotionImage4kSk"], four_k_secret)
        self.assertNotIn(default_secret, result.stdout + result.stderr)
        self.assertNotIn(four_k_secret, result.stdout + result.stderr)

    def test_input_file_is_read_only_and_accepts_assignment_format(self) -> None:
        default_secret = "sk-file-default"
        four_k_secret = "sk-file-4k"
        original = (
            b"\xef\xbb\xbf\r\n"
            + f"PodotionImage4kSk={four_k_secret}\r\n".encode()
            + b"\r\n"
            + f"PodotionImageSk={default_secret}\r\n".encode()
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "credentials.txt"
            target = Path(temp_dir) / "provider.toml"
            source.write_bytes(original)
            before = source.stat()
            result = self.run_command(
                target,
                "--input-file",
                str(source),
                "--force",
            )
            after = source.stat()
            saved = tomllib.loads(target.read_text(encoding="utf-8"))

            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
            self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(before.st_mode))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(saved["PodotionImageSk"], default_secret)
        self.assertEqual(saved["PodotionImage4kSk"], four_k_secret)
        self.assertNotIn(default_secret, result.stdout + result.stderr)
        self.assertNotIn(four_k_secret, result.stdout + result.stderr)

    def test_assignment_input_rejects_unknown_duplicate_missing_and_placeholder_keys(self) -> None:
        secret = "sk-invalid-input-must-not-leak"
        inputs = (
            f"UnknownCredential={secret}\n",
            f"PodotionImageSk={secret}\nPodotionImageSk=sk-second\n",
            f"PodotionImage4kSk={secret}\n",
            "PodotionImageSk=__PODOTION_IMAGE_SK__\n",
            f"PodotionImageSk ={secret}\n",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "provider.toml"
            for assignment in inputs:
                with self.subTest(assignment=assignment.split("=", 1)[0]):
                    result = self.run_cli(target, assignment, "--force")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(target.exists())
                    self.assertNotIn(secret, result.stdout + result.stderr)

    def test_input_file_rejects_legacy_raw_lines_and_is_mutually_exclusive_with_stdin(self) -> None:
        secret = "sk-source-never-print"
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "credentials.txt"
            target = Path(temp_dir) / "provider.toml"
            source.write_text(secret + "\n", encoding="utf-8")
            original = source.read_bytes()
            raw_result = self.run_command(target, "--input-file", str(source), "--force")
            exclusive_result = self.run_command(
                target,
                "--stdin",
                "--input-file",
                str(source),
                "--force",
                stdin=secret,
            )

            self.assertNotEqual(raw_result.returncode, 0)
            self.assertNotEqual(exclusive_result.returncode, 0)
            self.assertFalse(target.exists())
            self.assertEqual(source.read_bytes(), original)
            self.assertNotIn(secret, raw_result.stdout + raw_result.stderr)
            self.assertNotIn(secret, exclusive_result.stdout + exclusive_result.stderr)

    def test_input_file_cannot_alias_destination(self) -> None:
        secret = "sk-same-file-never-print"
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "provider.toml"
            original = f"PodotionImageSk={secret}\n".encode()
            target.write_bytes(original)
            result = self.run_command(
                target,
                "--input-file",
                str(target),
                "--force",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_bytes(), original)
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_set_4k_preserves_default_and_replaces_4k(self) -> None:
        default_secret = "sk-default-preserved"
        new_four_k = "sk-new-4k"
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "provider.toml"
            target.write_text(
                self.module.render_config(default_secret, "sk-old-4k"), encoding="utf-8"
            )
            result = self.run_cli(target, new_four_k + "\n", "--set-4k", "--force")
            saved = tomllib.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(saved["PodotionImageSk"], default_secret)
        self.assertEqual(saved["PodotionImage4kSk"], new_four_k)
        self.assertNotIn(default_secret, result.stdout + result.stderr)
        self.assertNotIn(new_four_k, result.stdout + result.stderr)

    def test_set_4k_accepts_assignment_stdin_and_input_file(self) -> None:
        default_secret = "sk-default-preserved"
        values = ("sk-assignment-stdin", "sk-assignment-file")
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "provider.toml"
            target.write_text(self.module.render_config(default_secret), encoding="utf-8")
            stdin_result = self.run_cli(
                target,
                f"PodotionImage4kSk={values[0]}\n",
                "--set-4k",
                "--force",
            )
            source = Path(temp_dir) / "credentials.txt"
            source.write_text(
                f"\ufeff\nPodotionImage4kSk={values[1]}\n",
                encoding="utf-8",
            )
            original = source.read_bytes()
            file_result = self.run_command(
                target,
                "--input-file",
                str(source),
                "--set-4k",
                "--force",
            )
            saved = tomllib.loads(target.read_text(encoding="utf-8"))

            self.assertEqual(source.read_bytes(), original)

        self.assertEqual(stdin_result.returncode, 0, stdin_result.stderr)
        self.assertEqual(file_result.returncode, 0, file_result.stderr)
        self.assertEqual(saved["PodotionImageSk"], default_secret)
        self.assertEqual(saved["PodotionImage4kSk"], values[1])
        combined = stdin_result.stdout + stdin_result.stderr + file_result.stdout + file_result.stderr
        for secret in (default_secret, *values):
            self.assertNotIn(secret, combined)

    def test_set_4k_requires_flags_and_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "missing.toml"
            missing = self.run_cli(target, "sk-new-4k\n", "--set-4k", "--force")
            no_force = self.run_cli(target, "sk-new-4k\n", "--set-4k")
            self.assertNotEqual(missing.returncode, 0)
            self.assertNotEqual(no_force.returncode, 0)
            self.assertFalse(target.exists())

    def test_set_4k_invalid_existing_config_is_zero_write_and_no_leak(self) -> None:
        new_four_k = "sk-new-must-not-leak"
        configs = (
            b'base_url = "https://ai.podotion.com/v1"\nPodotionImageSk = 123\n',
            b'base_url = "https://ai.podotion.com/v1"\nPodotionImageSk = "{{placeholder}}"\n',
            b'base_url = "https://ai.podotion.com/v1"\nPodotionImageSk = "sk-old"\nextra = true\n',
            b'base_url = "https://example.test/v1"\nPodotionImageSk = "sk-old"\n',
            b'not valid toml = "sk-old"\n',
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "provider.toml"
            for original in configs:
                target.write_bytes(original)
                with self.subTest(original=original):
                    result = self.run_cli(
                        target, new_four_k + "\n", "--set-4k", "--force"
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(target.read_bytes(), original)
                    self.assertEqual(list(target.parent.glob(".provider.toml.*.tmp")), [])
                    self.assertNotIn(new_four_k, result.stdout + result.stderr)

    def test_set_4k_rejects_oversized_existing_config_without_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "provider.toml"
            original = b"#" * (self.module.MAX_CONFIG_BYTES + 1)
            target.write_bytes(original)
            result = self.run_cli(target, "sk-new-4k\n", "--set-4k", "--force")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_bytes(), original)

    def test_stdin_rejects_extra_lines_and_oversize_without_write(self) -> None:
        inputs = (
            "sk-one\nsk-two\nsk-three",
            "z" * (self.module.MAX_SECRET_BYTES + 1),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "provider.toml"
            for stdin in inputs:
                with self.subTest(length=len(stdin)):
                    result = self.run_cli(target, stdin, "--force")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(target.exists())
                    self.assertNotIn(stdin, result.stdout + result.stderr)

    def test_check_is_local_read_only_and_reports_profiles_without_secrets(self) -> None:
        default_secret = "sk-check-default"
        four_k_secret = "sk-check-4k"
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "provider.toml"
            target.write_text(
                self.module.render_config(default_secret, four_k_secret), encoding="utf-8"
            )
            original = target.read_bytes()
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    self.module.sys,
                    "argv",
                    [
                        str(CONFIGURE_PATH),
                        "--check",
                        "--credential-file",
                        str(target),
                    ],
                ),
                mock.patch.object(
                    self.module.runtime,
                    "_open_provider_request",
                    side_effect=AssertionError("network was attempted"),
                ),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                result = self.module.main()
            report = json.loads(stdout.getvalue())

            self.assertEqual(target.read_bytes(), original)

        self.assertEqual(result, 0, stderr.getvalue())
        self.assertEqual(report["operation"], "check")
        self.assertEqual(report["credential_profiles"], {"default": True, "4k": True})
        self.assertNotIn(default_secret, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(four_k_secret, stdout.getvalue() + stderr.getvalue())

    def test_check_rejects_mutating_options_and_missing_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "provider.toml"
            missing = self.run_command(target, "--check")
            forced = self.run_command(target, "--check", "--force")
            sourced = self.run_command(target, "--check", "--stdin", stdin="sk-secret")

            self.assertNotEqual(missing.returncode, 0)
            self.assertNotEqual(forced.returncode, 0)
            self.assertNotEqual(sourced.returncode, 0)
            self.assertFalse(target.exists())
            self.assertNotIn("sk-secret", sourced.stdout + sourced.stderr)

    def test_check_reports_optional_4k_absent_and_redacts_invalid_config(self) -> None:
        default_secret = "sk-check-default-only"
        malformed_secret = "sk-malformed-must-not-leak"
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "provider.toml"
            target.write_text(self.module.render_config(default_secret), encoding="utf-8")
            valid = self.run_command(target, "--check")
            report = json.loads(valid.stdout)

            original = f'PodotionImageSk = "{malformed_secret}"\nthis is not toml\n'.encode()
            target.write_bytes(original)
            invalid = self.run_command(target, "--check")

            self.assertEqual(target.read_bytes(), original)

        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertEqual(report["credential_profiles"], {"default": True, "4k": False})
        self.assertNotIn(default_secret, valid.stdout + valid.stderr)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertNotIn(malformed_secret, invalid.stdout + invalid.stderr)


if __name__ == "__main__":
    unittest.main()

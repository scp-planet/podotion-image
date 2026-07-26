from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import SCRIPT_PATH


def load_module():
    spec = importlib.util.spec_from_file_location("podotion_image_provider", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ProviderConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def write_direct_config(
        self,
        path: Path,
        secret: str = "sk-image-secret",
        secret_4k: str | None = None,
    ) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            'base_url = "https://ai.podotion.com/v1"\n'
            f'PodotionImageSk = "{secret}"\n'
        )
        if secret_4k is not None:
            content += f'PodotionImage4kSk = "{secret_4k}"\n'
        path.write_text(content, encoding="utf-8")
        return path

    def test_default_path_uses_codex_home_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.module.direct_provider_config_path(
                {"CODEX_HOME": str(Path(temp_dir) / "codex-home")}
            )
            root_matches = path.parents[2].samefile(Path(temp_dir))
            relative_path = path.relative_to(path.parents[2])
        self.assertTrue(root_matches)
        self.assertEqual(
            relative_path,
            Path("codex-home") / "podotion-image" / "provider.toml",
        )

    def test_loads_direct_secret_and_fixed_remote_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.write_direct_config(Path(temp_dir) / "provider.toml")
            provider = self.module.load_direct_provider(config, environ={})

        self.assertEqual(provider.provider_id, "podotion-direct")
        self.assertEqual(provider.base_url, "https://ai.podotion.com/v1")
        self.assertEqual(provider.bearer_token, "sk-image-secret")
        self.assertEqual(provider.credential_mode, "podotion_image_sk")
        self.assertNotIn("sk-image-secret", repr(provider))
        self.assertEqual(
            self.module._request_headers(provider)["Authorization"],
            "Bearer sk-image-secret",
        )

    def test_loads_and_selects_two_credentials_without_repr_leaks(self) -> None:
        default_secret = "sk-default-private"
        secret_4k = "sk-4k-private"
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.write_direct_config(
                Path(temp_dir) / "provider.toml", default_secret, secret_4k
            )
            provider = self.module.load_direct_provider(config, environ={})

        selected_default = provider.select_credential("default")
        selected_4k = provider.select_credential("4k")
        self.assertEqual(selected_default.bearer_token, default_secret)
        self.assertEqual(selected_default.credential_profile, "default")
        self.assertEqual(selected_4k.bearer_token, secret_4k)
        self.assertEqual(selected_4k.credential_profile, "4k")
        self.assertEqual(selected_4k.credential_mode, "podotion_image_4k_sk")
        for value in (provider, selected_default, selected_4k):
            self.assertNotIn(default_secret, repr(value))
            self.assertNotIn(secret_4k, repr(value))

    def test_missing_optional_4k_credential_fails_only_when_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.write_direct_config(Path(temp_dir) / "provider.toml")
            provider = self.module.load_direct_provider(config, environ={})

        self.assertEqual(provider.select_credential("default").credential_profile, "default")
        with self.assertRaisesRegex(RuntimeError, "PodotionImage4kSk"):
            provider.select_credential("4k")

    def test_config_fields_require_canonical_toml_string_types(self) -> None:
        invalid = (
            'base_url = 123\nPodotionImageSk = "sk-default"\n',
            'base_url = "https://ai.podotion.com/v1"\nPodotionImageSk = 123\n',
            'base_url = "https://ai.podotion.com/v1"\nPodotionImageSk = "sk-default"\nPodotionImage4kSk = 123\n',
            'base_url = "https://ai.podotion.com/v1"\nPodotionImageSk = "sk-default"\nextra = true\n',
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "provider.toml"
            for content in invalid:
                with self.subTest(content=content.splitlines()[-1]):
                    config.write_text(content, encoding="utf-8")
                    with self.assertRaises(RuntimeError):
                        self.module.load_direct_provider(config, environ={})

    def test_braced_and_4k_placeholders_are_rejected(self) -> None:
        placeholders = (
            ("{{PodotionImageSk}}", None),
            ("sk-default", "{{PodotionImage4kSk}}"),
            ("sk-default", self.module.DIRECT_4K_SECRET_PLACEHOLDER),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "provider.toml"
            for default_secret, secret_4k in placeholders:
                with self.subTest(default=default_secret, four_k=secret_4k):
                    self.write_direct_config(config, default_secret, secret_4k)
                    with self.assertRaisesRegex(RuntimeError, "placeholder"):
                        self.module.load_direct_provider(config, environ={})

    def test_native_codex_home_defaults_cover_supported_platforms(self) -> None:
        cases = [
            ("windows", {"USERPROFILE": r"C:\\Users\\Ada"}, r"C:\Users\Ada\.codex"),
            ("macos", {"HOME": "/Users/ada"}, "/Users/ada/.codex"),
            ("linux", {"HOME": "/home/ada"}, "/home/ada/.codex"),
            (
                "linux",
                {"HOME": "/home/ada", "WSL_DISTRO_NAME": "Ubuntu"},
                "/home/ada/.codex",
            ),
        ]
        for platform, environ, expected in cases:
            with self.subTest(platform=platform, environ=environ):
                self.assertEqual(
                    self.module._native_codex_home_string(
                        environ, platform=platform, os_release=""
                    ),
                    expected,
                )

    def test_windows_codex_home_lookup_is_case_insensitive(self) -> None:
        self.assertEqual(
            self.module._native_codex_home_string(
                {"codex_home": r"D:\\Codex Data"}, platform="windows"
            ),
            r"D:\Codex Data",
        )

    def test_missing_direct_config_has_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "provider.toml"
            with self.assertRaisesRegex(FileNotFoundError, "configure_direct.py"):
                self.module.load_direct_provider(missing, environ={})

    def test_unchanged_placeholder_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.write_direct_config(
                Path(temp_dir) / "provider.toml",
                self.module.DIRECT_SECRET_PLACEHOLDER,
            )
            with self.assertRaisesRegex(RuntimeError, "placeholder"):
                self.module.load_direct_provider(config, environ={})

    def test_other_base_url_is_rejected_without_exposing_secret(self) -> None:
        secret = "sk-must-stay-hidden"
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "provider.toml"
            config.write_text(
                'base_url = "https://collector.example/v1"\n'
                f'PodotionImageSk = "{secret}"\n',
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError) as raised:
                self.module.load_direct_provider(config, environ={})

        self.assertIn("exactly https://ai.podotion.com/v1", str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))

    def test_malformed_config_error_does_not_include_secret_line(self) -> None:
        secret = "sk-malformed-and-hidden"
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "provider.toml"
            config.write_text(f'PodotionImageSk = "{secret}\n', encoding="utf-8")
            with self.assertRaises(RuntimeError) as raised:
                self.module.load_direct_provider(config, environ={})
        self.assertNotIn(secret, str(raised.exception))

    def test_url_builders_use_v1_once(self) -> None:
        self.assertEqual(
            self.module.build_images_url("https://ai.podotion.com/v1", "generate"),
            "https://ai.podotion.com/v1/images/generations",
        )
        self.assertEqual(
            self.module.build_images_url("https://ai.podotion.com/v1", "edit"),
            "https://ai.podotion.com/v1/images/edits",
        )

    def test_redaction_removes_direct_config_and_bearer_secrets(self) -> None:
        secret = "sk-super-secret-value"
        message = (
            f"Authorization: Bearer {secret}; "
            f'PodotionImageSk = "{secret}"'
        )
        redacted = self.module.redact_secrets(message, secrets=[secret])
        self.assertNotIn(secret, redacted)
        self.assertNotIn("Bearer sk-", redacted)

    def test_redaction_removes_4k_config_secret_without_explicit_secret_list(self) -> None:
        secret = "sk-4k-super-secret-value"
        redacted = self.module.redact_secrets(
            f'PodotionImage4kSk = "{secret}"'
        )
        self.assertNotIn(secret, redacted)


if __name__ == "__main__":
    unittest.main()

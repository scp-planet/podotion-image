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

    def write_direct_config(self, path: Path, secret: str = "sk-image-secret") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'base_url = "https://ai.podotion.com/v1"\n'
            f'PodotionImageSk = "{secret}"\n',
            encoding="utf-8",
        )
        return path

    def test_default_path_uses_codex_home_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.module.direct_provider_config_path(
                {"CODEX_HOME": str(Path(temp_dir) / "codex-home")}
            )
        self.assertEqual(
            path,
            Path(temp_dir).resolve() / "codex-home" / "podotion-image" / "provider.toml",
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


if __name__ == "__main__":
    unittest.main()

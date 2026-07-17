from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

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

    def test_render_replaces_placeholder_and_escapes_toml(self) -> None:
        secret = 'sk-value-with-"quote"-and-\\slash'
        rendered = self.module.render_config(secret)
        parsed = tomllib.loads(rendered)
        self.assertEqual(parsed["PodotionImageSk"], secret)
        self.assertEqual(parsed["base_url"], "https://ai.podotion.com/v1")
        self.assertNotIn("__PODOTION_IMAGE_SK__", rendered)

    def test_placeholder_and_empty_secret_are_rejected(self) -> None:
        for value in ("", "__PODOTION_IMAGE_SK__"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.module.render_config(value)

    def test_private_write_is_atomic_and_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp" if os.name != "nt" else None) as temp_dir:
            target = Path(temp_dir) / "runtime" / "provider.toml"
            result = self.module.write_private_config(
                target,
                self.module.render_config("sk-private"),
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

    def test_stdin_cli_never_prints_secret(self) -> None:
        secret = "sk-cli-never-print-this"
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "provider.toml"
            result = subprocess.run(
                [
                    sys.executable,
                    str(CONFIGURE_PATH),
                    "--stdin",
                    "--credential-file",
                    str(target),
                ],
                input=secret,
                text=True,
                capture_output=True,
                check=False,
            )
            report = json.loads(result.stdout)
            saved = tomllib.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(report["ok"])
        self.assertEqual(saved["PodotionImageSk"], secret)
        self.assertNotIn(secret, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

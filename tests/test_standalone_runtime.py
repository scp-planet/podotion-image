from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import SCRIPT_PATH


def load_module():
    spec = importlib.util.spec_from_file_location(
        "podotion_image_standalone_runtime", SCRIPT_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class StandaloneRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def test_copied_script_runs_in_isolated_python_without_repository_imports(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("from podotion_image", source)
        self.assertNotIn("sys.path.insert", source)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            copied = root / "standalone" / "podotion_image.py"
            copied.parent.mkdir()
            shutil.copy2(SCRIPT_PATH, copied)
            result = subprocess.run(
                [sys.executable, "-I", str(copied), "sizes"],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"3840x2160"', result.stdout)

    def test_resolution_modes_preserve_request_metadata(self) -> None:
        tier = self.module.resolve_request_size("2k", "9:16")
        exact = self.module.resolve_request_size("2560x1440", None)
        ratio_only = self.module.resolve_request_size(None, "16:9")
        automatic = self.module.resolve_request_size(None, None)

        self.assertEqual(tier.value, "1152x2048")
        self.assertEqual(tier.requested_tier, "2k")
        self.assertEqual((tier.width, tier.height), (1152, 2048))
        self.assertEqual(exact.value, "2560x1440")
        self.assertIsNone(exact.requested_tier)
        self.assertEqual((exact.width, exact.height), (2560, 1440))
        self.assertEqual(ratio_only.requested_tier, "1k")
        self.assertEqual(ratio_only.value, "1280x720")
        self.assertEqual(automatic.value, "auto")
        self.assertIsNone(automatic.width)
        self.assertIsNone(automatic.height)

    def test_tier_and_exact_size_argument_combinations_are_unambiguous(self) -> None:
        with self.assertRaisesRegex(ValueError, "--ratio is required"):
            self.module.resolve_request_size("2k", None)
        with self.assertRaisesRegex(ValueError, "cannot be used"):
            self.module.resolve_request_size("2560x1440", "16:9")

    def test_exact_dimension_constraints(self) -> None:
        valid = ("1024x640", "3840x1280", "3600x2304")
        invalid = (
            "3856x1280",  # Long edge over 3840.
            "1025x1024",  # Edges must be multiples of 16.
            "3840x1264",  # Aspect ratio exceeds 3:1.
            "1008x640",   # Fewer than 655,360 pixels.
            "3840x2176",  # More than 8,294,400 pixels.
        )
        for value in valid:
            with self.subTest(value=value):
                self.assertEqual(
                    self.module.resolve_request_size(value, None).value, value
                )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(Exception):
                    self.module.resolve_request_size(value, None)

    def test_credential_routing_covers_all_rules(self) -> None:
        for ratio in self.module.SUPPORTED_RATIOS:
            with self.subTest(tier="4k", ratio=ratio):
                resolved = self.module.resolve_size("4k", ratio)
                self.assertIn(
                    (resolved.width, resolved.height),
                    self.module.CANONICAL_4K_DIMENSIONS,
                )
                self.assertEqual(resolved.credential_profile, "4k")
            for tier in ("1k", "2k"):
                with self.subTest(tier=tier, ratio=ratio):
                    self.assertEqual(
                        self.module.resolve_size(tier, ratio).credential_profile,
                        "default",
                    )

        self.assertEqual(
            self.module.resolve_request_size("2336x3504", None).credential_profile,
            "4k",
        )
        self.assertEqual(
            self.module.resolve_request_size("3600x2304", None).credential_profile,
            "4k",
        )
        self.assertEqual(
            self.module.resolve_request_size("2560x1440", None).credential_profile,
            "default",
        )
        self.assertEqual(
            self.module.resolve_request_size(None, None).credential_profile,
            "default",
        )


if __name__ == "__main__":
    unittest.main()

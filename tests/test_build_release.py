from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.build_release import ARCHIVE_ROOT, build_release


class BuildReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        required = (
            ".codex-plugin/plugin.json",
            ".mcp.json",
            "README.md",
            "mcp/server.py",
            "scripts/install.py",
            "skills/podotion-image/SKILL.md",
            "skills/podotion-image/scripts/podotion_image.py",
        )
        for relative in required:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"content for {relative}\n", encoding="utf-8")

    def test_build_uses_one_top_level_directory_and_excludes_local_files(self) -> None:
        (self.root / "tests").mkdir()
        (self.root / "tests/test_example.py").write_text("pass\n", encoding="utf-8")
        excluded = (
            ".git/config",
            "dist/old.zip",
            "mcp/__pycache__/server.cpython-314.pyc",
            ".pytest_cache/state",
            "PodotionImage/generated.png",
            "podotion-image-workspace/note.txt",
        )
        for relative in excluded:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"not for release")

        output = Path(self.temporary.name) / "release.zip"
        result = build_release(self.root, output)

        self.assertTrue(result["ok"])
        with zipfile.ZipFile(output) as archive:
            names = archive.namelist()
        self.assertTrue(names)
        self.assertEqual({name.split("/", 1)[0] for name in names}, {ARCHIVE_ROOT})
        self.assertIn(f"{ARCHIVE_ROOT}/README.md", names)
        self.assertIn(f"{ARCHIVE_ROOT}/tests/test_example.py", names)
        for relative in excluded:
            self.assertNotIn(f"{ARCHIVE_ROOT}/{relative}", names)

    def test_build_is_deterministic(self) -> None:
        first = Path(self.temporary.name) / "first.zip"
        second = Path(self.temporary.name) / "second.zip"

        build_release(self.root, first)
        build_release(self.root, second)

        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_missing_plugin_manifest_is_rejected(self) -> None:
        (self.root / ".codex-plugin/plugin.json").unlink()

        with self.assertRaisesRegex(ValueError, "plugin.json"):
            build_release(self.root, Path(self.temporary.name) / "invalid.zip")


if __name__ == "__main__":
    unittest.main()

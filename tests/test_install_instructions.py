from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
SKILL_URL = "https://github.com/scp-planet/podotion-image/tree/main/skills/podotion-image"
REPOSITORY = "https://github.com/scp-planet/podotion-image.git"


class InstallInstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.readme = README.read_text(encoding="utf-8")
        section = self.readme.index("## 使用 Skills Install 首次安装")
        prompt_start = self.readme.index("```text", section)
        prompt_end = self.readme.index("```", prompt_start + len("```text"))
        self.prompt = self.readme[prompt_start + len("```text") : prompt_end]

    def test_first_install_uses_standard_skill_subdirectory_url(self) -> None:
        self.assertIn("内置 skill-installer", self.prompt)
        self.assertIn(SKILL_URL, self.prompt)
        self.assertIn("裸仓库 URL", self.readme)
        self.assertNotIn("scripts/install.py", self.prompt)
        self.assertNotIn("codex plugin add", self.prompt)

    def test_install_prompt_keeps_both_secrets_on_stdin(self) -> None:
        self.assertEqual(self.readme.count("{{PodotionImageSk}}"), 1)
        self.assertEqual(self.readme.count("{{PodotionImage4kSk}}"), 1)
        self.assertIn("configure_direct.py --stdin --force", self.prompt)
        self.assertIn("第一行是默认 SK，第二行是 4K SK", self.prompt)
        self.assertIn("不得把 SK 放入命令行参数", self.prompt)
        self.assertIn("不得运行 --image-probe", self.prompt)

    def test_native_python_and_runtime_requirement_are_clear(self) -> None:
        for platform in ("Windows", "macOS", "Linux", "WSL"):
            with self.subTest(platform=platform):
                self.assertIn(platform, self.prompt)
        self.assertIn("Python 3.11", self.readme)
        self.assertIn("不代表系统一定提供", self.readme)
        self.assertIn("重启 Codex、新建任务", self.prompt)

    def test_update_and_uninstall_contracts_are_documented(self) -> None:
        self.assertIn("manage.py update --dry-run", self.readme)
        self.assertIn("manage.py update", self.readme)
        self.assertIn("manage.py uninstall-legacy-plugin --yes", self.readme)
        self.assertIn("manage.py uninstall --yes", self.readme)
        self.assertIn(REPOSITORY, self.readme)
        self.assertIn("Schannel", self.readme)
        self.assertIn("凭据、工作区图片和请求状态一律保留", self.readme)

    def test_plugin_runtime_surface_is_removed_from_repository(self) -> None:
        for relative in (
            ".codex-plugin/plugin.json",
            ".mcp.json",
            "mcp/protocol.py",
            "mcp/server.py",
            "podotion_image/__init__.py",
            "podotion_image/paths.py",
            "scripts/install.py",
            "scripts/build_release.py",
        ):
            with self.subTest(path=relative):
                self.assertFalse((ROOT / relative).exists())

    def test_tradeoff_and_legacy_migration_are_explicit(self) -> None:
        self.assertIn("不会执行安装钩子", self.readme)
        self.assertIn("不再提供 MCP `image`", self.readme)
        self.assertIn("从旧 Plugin 迁移", self.readme)
        self.assertIn("uninstall-legacy-plugin --yes", self.prompt)
        self.assertIn("不要要求我手工编辑 Marketplace 或删除目录", self.prompt)
        self.assertIn("不需要手工运行 `codex plugin remove`", self.readme)

    def test_default_output_directory_is_documented(self) -> None:
        self.assertIn("<workspace>/PodotionImageOutput", self.readme)


if __name__ == "__main__":
    unittest.main()

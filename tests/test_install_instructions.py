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
        lines = [line for line in self.prompt.splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], "$skill-installer")
        self.assertEqual(lines[1], SKILL_URL)
        self.assertIn(SKILL_URL, self.prompt)
        self.assertIn("裸仓库 URL", self.readme)
        self.assertNotIn("scripts/install.py", self.prompt)
        self.assertNotIn("codex plugin add", self.prompt)

    def test_install_prompt_contains_no_credentials_or_post_install_actions(self) -> None:
        self.assertNotIn("{{PodotionImageSk}}", self.readme)
        self.assertNotIn("{{PodotionImage4kSk}}", self.readme)
        for marker in (
            "PodotionImageSk",
            "PodotionImage4kSk",
            "configure_direct.py",
            "doctor",
            "uninstall-legacy-plugin",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self.prompt)
        self.assertIn("安装阶段不提交或配置任何 SK", self.readme)

    def test_native_python_and_runtime_requirement_are_clear(self) -> None:
        for platform in ("Windows", "macOS", "Linux", "WSL"):
            with self.subTest(platform=platform):
                self.assertIn(platform, self.readme)
        self.assertIn("Python 3.11", self.readme)
        self.assertIn("不代表系统一定提供", self.readme)
        self.assertIn("安装完成后重启 Codex 并新建任务", self.readme)

    def test_lazy_configuration_preflight_and_inputs_are_documented(self) -> None:
        self.assertIn("每个新任务的第一次生成或编辑之前", self.readme)
        self.assertIn("configure_direct.py --check", self.readme)
        self.assertIn("manage.py status", self.readme)
        self.assertIn("本地、只读", self.readme)
        self.assertIn("PodotionImageSk=<value>", self.readme)
        self.assertIn("PodotionImage4kSk=<value>", self.readme)
        self.assertIn("UTF-8 文本文件", self.readme)
        self.assertIn("非 4K 请求不要求 4K SK", self.readme)

    def test_attachment_security_boundary_and_restart_are_explicit(self) -> None:
        self.assertIn("避免把 SK 字面量放进聊天提示正文或进程命令行参数", self.readme)
        self.assertIn("仍是当前 Codex 会话的输入", self.readme)
        self.assertIn("并不是会话外的秘密通道", self.readme)
        self.assertIn("--input-file", self.readme)
        self.assertIn("任何成功的配置写入后", self.readme)
        self.assertIn("运行不带 `--image-probe` 的非计费 `doctor`", self.readme)
        self.assertIn("完成配置和可选迁移后停止当前任务", self.readme)

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
        self.assertIn("每个新任务的首次图片预检", self.readme)
        self.assertIn("拒绝只对当前任务有效", self.readme)
        doctor = self.readme.index("必须先在本任务运行不计费的 `podotion_image.py doctor`")
        cleanup = self.readme.index("只有 doctor 成功才运行清理", doctor)
        self.assertLess(doctor, cleanup)
        self.assertIn("不需要手工运行 `codex plugin remove`", self.readme)
        self.assertIn("迁移成功后立即停止当前任务", self.readme)

    def test_default_output_directory_is_documented(self) -> None:
        self.assertIn("<workspace>/PodotionImageOutput", self.readme)

    def test_single_request_and_serial_multi_image_behavior_is_documented(self) -> None:
        self.assertIn("每次上游请求固定 `n=1`", self.readme)
        self.assertIn("只接受一张标准 `data[]` 结果", self.readme)
        self.assertIn("只使用最基础的同步 Images API", self.readme)
        self.assertIn("不调用 `/async`", self.readme)
        self.assertIn("不发送 `stream=true`", self.readme)
        self.assertIn("最多串行执行 10 个独立图片操作", self.readme)
        self.assertIn("绝不并行", self.readme)
        self.assertIn("每生成并保存一张就立即返回该张结果", self.readme)
        self.assertIn("相同提示词的第二张及以后使用 `--force-new`", self.readme)
        self.assertIn("后续图片不再执行", self.readme)

    def test_user_facing_errors_are_documented_as_concise(self) -> None:
        self.assertIn("一条简洁、脱敏的错误原因", self.readme)
        self.assertIn("不展示上游原始响应、堆栈、密钥或完整诊断对象", self.readme)


if __name__ == "__main__":
    unittest.main()

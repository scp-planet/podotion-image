from __future__ import annotations

import unittest
from pathlib import Path


README = Path(__file__).resolve().parents[1] / "README.md"
REPOSITORY = "https://github.com/scp-planet/podotion-image.git"


class InstallInstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.readme = README.read_text(encoding="utf-8")
        prompt_start = self.readme.index("```text", self.readme.index("## 从 GitHub 安装或更新"))
        prompt_end = self.readme.index("```", prompt_start + len("```text"))
        self.prompt = self.readme[prompt_start + len("```text") : prompt_end]

    def test_install_prompt_is_cross_platform_and_concise(self) -> None:
        self.assertIn("当前原生运行时", self.prompt)
        for platform in ("Windows", "macOS", "Linux", "WSL"):
            with self.subTest(platform=platform):
                self.assertIn(platform, self.prompt)
        self.assertIn("Windows 使用 `py -3`", self.prompt)
        self.assertIn("macOS、Linux 和 WSL 使用 `python3`", self.prompt)
        self.assertNotIn("~/plugins/podotion-image", self.prompt)
        self.assertNotIn("~/.agents/plugins/marketplace.json", self.prompt)
        self.assertNotIn("创建权限受限的随机临时目录", self.prompt)
        self.assertNotIn("1.", self.prompt)

    def test_windows_schannel_details_remain_outside_prompt(self) -> None:
        self.assertIn("仅在原生 Windows 遇到 Schannel 错误时", self.prompt)
        self.assertIn("SEC_E_NO_CREDENTIALS", self.readme)
        self.assertIn(
            "git -c http.sslBackend=openssl clone --depth 1 " + REPOSITORY,
            self.readme,
        )
        self.assertIn("<new-clone-dir>", self.readme)

    def test_retry_does_not_weaken_tls_or_persist_git_configuration(self) -> None:
        self.assertIn("不会修改用户级或系统级 Git 配置", self.readme)
        self.assertIn("不能通过关闭 TLS 校验解决", self.readme)
        self.assertIn("不要永久切换 Git 后端", self.readme)

    def test_install_prompt_keeps_source_and_secret_contracts(self) -> None:
        self.assertIn(REPOSITORY, self.prompt)
        self.assertEqual(self.readme.count("{{PodotionImageSk}}"), 1)
        self.assertIn("包含 Skill 和 MCP 的 Plugin", self.prompt)
        self.assertIn("不使用 skill-installer", self.prompt)
        self.assertIn("CODEX_HOME 未设置时使用当前平台默认值", self.prompt)
        self.assertIn("不做额外写权限探针", self.prompt)
        self.assertIn("`configure_direct.py --stdin --force`", self.prompt)
        self.assertIn("不得运行 `--image-probe`", self.prompt)


if __name__ == "__main__":
    unittest.main()

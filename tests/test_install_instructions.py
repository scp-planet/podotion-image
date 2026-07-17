from __future__ import annotations

import unittest
from pathlib import Path


README = Path(__file__).resolve().parents[1] / "README.md"
REPOSITORY = "https://github.com/scp-planet/podotion-image.git"


class InstallInstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.readme = README.read_text(encoding="utf-8")

    def test_windows_schannel_retry_is_narrow_and_source_preserving(self) -> None:
        self.assertIn("仅在原生 Windows 中", self.readme)
        self.assertIn("schannel", self.readme)
        self.assertIn("AcquireCredentialsHandle failed", self.readme)
        self.assertIn("SEC_E_NO_CREDENTIALS", self.readme)
        self.assertIn(
            "git -c http.sslBackend=openssl clone --depth 1 " + REPOSITORY,
            self.readme,
        )
        self.assertIn("对同一 GitHub URL 只重试一次", self.readme)
        self.assertIn("<new-clone-dir>", self.readme)

    def test_retry_does_not_weaken_tls_or_persist_git_configuration(self) -> None:
        self.assertIn("不得执行 git config --global 或 --system", self.readme)
        self.assertIn("不得设置 http.sslVerify=false", self.readme)
        self.assertIn("不得改用代理、镜像、归档下载、附件或 WSL", self.readme)
        self.assertIn("不要永久切换 Git 后端", self.readme)

    def test_install_prompt_keeps_source_and_secret_contracts(self) -> None:
        self.assertGreaterEqual(self.readme.count(REPOSITORY), 4)
        self.assertIn("origin 严格等于上述 GitHub 地址", self.readme)
        self.assertIn("git rev-parse --is-shallow-repository 返回 true", self.readme)
        self.assertEqual(self.readme.count("{{PodotionImageSk}}"), 1)
        self.assertIn("不得运行 --image-probe", self.readme)


if __name__ == "__main__":
    unittest.main()

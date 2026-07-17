from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from podotion_image.paths import PlatformKind, detect_platform
from scripts.install import (
    InstallError,
    MarketplaceConflictError,
    SharedCodexHomeError,
    apply_manifest_cachebuster,
    build_install_plan,
    detect_codex_home,
    execute_install_plan,
    main,
    merge_marketplace,
    render_mcp_json,
    validate_codex_home_owner,
)


class CodexHomeTests(unittest.TestCase):
    def test_each_platform_uses_its_own_default(self) -> None:
        cases = (
            (
                "windows",
                {"USERPROFILE": r"C:\Users\Ada"},
                r"C:\Users\Ada\.codex",
            ),
            ("wsl", {"HOME": "/home/ada"}, "/home/ada/.codex"),
            ("macos", {"HOME": "/Users/ada"}, "/Users/ada/.codex"),
            ("linux", {"HOME": "/home/ada"}, "/home/ada/.codex"),
        )
        for platform, environ, expected in cases:
            with self.subTest(platform=platform):
                self.assertEqual(
                    detect_codex_home(
                        platform=platform,
                        environ=environ,
                        check_marker=False,
                    ),
                    expected,
                )

    def test_wsl_rejects_a_windows_path_but_allows_codex_app_mount(self) -> None:
        with self.assertRaises(SharedCodexHomeError):
            detect_codex_home(
                platform="wsl",
                environ={"HOME": "/home/ada", "CODEX_HOME": r"C:\Users\Ada\.codex"},
                check_marker=False,
            )
        self.assertEqual(
            detect_codex_home(
                platform="wsl",
                environ={"HOME": "/home/ada", "CODEX_HOME": "/mnt/c/Users/Ada/.codex"},
                check_marker=False,
            ),
            "/mnt/c/Users/Ada/.codex",
        )

    def test_wsl_mount_uses_a_private_runtime_marker(self) -> None:
        from scripts.install import platform_marker_path

        self.assertEqual(
            platform_marker_path("/mnt/c/Users/Ada/.codex", PlatformKind.WSL),
            "/mnt/c/Users/Ada/.codex/.podotion-image-runtimes/wsl/.podotion-image-platform.json",
        )

    def test_wsl_uses_home_when_windows_profile_is_also_present(self) -> None:
        self.assertEqual(
            detect_codex_home(
                platform="wsl",
                environ={
                    "HOME": "/home/ada",
                    "USERPROFILE": r"C:\Users\Ada",
                },
                check_marker=False,
            ),
            "/home/ada/.codex",
        )

    def test_windows_rejects_a_wsl_share(self) -> None:
        with self.assertRaises(SharedCodexHomeError):
            detect_codex_home(
                platform="windows",
                environ={
                    "USERPROFILE": r"C:\Users\Ada",
                    "CODEX_HOME": r"\\wsl.localhost\Ubuntu\home\ada\.codex",
                },
                check_marker=False,
            )

    def test_owner_marker_rejects_cross_platform_reuse(self) -> None:
        with self.assertRaises(SharedCodexHomeError):
            validate_codex_home_owner(
                "/home/ada/.codex",
                PlatformKind.LINUX,
                marker={"schema": 1, "platform": "macos"},
            )


class InstallPlanTests(unittest.TestCase):
    def test_plans_are_native_on_windows_wsl_macos_and_linux(self) -> None:
        cases = (
            {
                "platform": "windows",
                "env": {"USERPROFILE": r"C:\Users\Ada"},
                "source": r"D:\src\podotion-image",
                "python": r"C:\Python314\python.exe",
                "destination": r"C:\Users\Ada\plugins\podotion-image",
                "codex_home": r"C:\Users\Ada\.codex",
            },
            {
                "platform": "wsl",
                "env": {"HOME": "/home/ada"},
                "source": "/src/podotion-image",
                "python": "/usr/bin/python3",
                "destination": "/home/ada/plugins/podotion-image",
                "codex_home": "/home/ada/.codex",
            },
            {
                "platform": "macos",
                "env": {"HOME": "/Users/ada"},
                "source": "/src/podotion-image",
                "python": "/opt/homebrew/bin/python3",
                "destination": "/Users/ada/plugins/podotion-image",
                "codex_home": "/Users/ada/.codex",
            },
            {
                "platform": "linux",
                "env": {"HOME": "/home/ada"},
                "source": "/src/podotion-image",
                "python": "/usr/bin/python3",
                "destination": "/home/ada/plugins/podotion-image",
                "codex_home": "/home/ada/.codex",
            },
        )
        for case in cases:
            with self.subTest(platform=case["platform"]):
                plan = build_install_plan(
                    case["source"],
                    platform=case["platform"],
                    environ=case["env"],
                    python_executable=case["python"],
                    check_marker=False,
                )
                self.assertEqual(plan.plugin_destination, case["destination"])
                self.assertEqual(plan.codex_home, case["codex_home"])
                self.assertEqual(
                    plan.codex_command,
                    ("codex", "plugin", "add", "podotion-image@personal"),
                )
                self.assertEqual(
                    plan.codex_rollback_command,
                    ("codex", "plugin", "remove", "podotion-image@personal"),
                )
                config = json.loads(plan.mcp_json)
                server = config["mcpServers"]["podotion-image"]
                self.assertEqual(server["command"], case["python"])
                self.assertEqual(server["args"][:2], ["-I", "-u"])
                self.assertEqual(server["args"][-1], "--stdio")
                self.assertEqual(
                    server["env"]["CODEX_HOME"], case["codex_home"]
                )
                if case["platform"] == "windows":
                    self.assertIn(
                        r'"CODEX_HOME": "C:\\Users\\Ada\\.codex"',
                        plan.mcp_json,
                    )

    def test_mcp_render_uses_absolute_interpreter_and_server_path(self) -> None:
        payload = json.loads(
            render_mcp_json(
                "/Users/ada/plugins/podotion-image",
                codex_home="/Users/ada/.codex",
                python_executable="/opt/homebrew/bin/python3",
                platform="macos",
            )
        )
        server = payload["mcpServers"]["podotion-image"]
        self.assertEqual(server["command"], "/opt/homebrew/bin/python3")
        self.assertEqual(
            server["args"],
            [
                "-I",
                "-u",
                "/Users/ada/plugins/podotion-image/mcp/server.py",
                "--stdio",
            ],
        )
        self.assertEqual(server["env"], {"CODEX_HOME": "/Users/ada/.codex"})
        self.assertEqual(server["startup_timeout_sec"], 30)
        self.assertEqual(server["tool_timeout_sec"], 3600)

    def test_wsl_codex_app_mount_keeps_standard_plugin_identity(self) -> None:
        plan = build_install_plan(
            "/src/podotion-image",
            platform="wsl",
            environ={
                "HOME": "/home/ada",
                "CODEX_HOME": "/mnt/c/Users/Ada/.codex",
            },
            python_executable="/usr/bin/python3",
            check_marker=False,
        )
        self.assertEqual(plan.plugin_destination, "/home/ada/plugins/podotion-image")
        self.assertEqual(plan.codex_command[-1], "podotion-image@personal")
        self.assertIn("/mnt/c/Users/Ada/.codex/.podotion-image-runtimes/wsl", plan.platform_marker)
        server = json.loads(plan.mcp_json)["mcpServers"]["podotion-image"]
        self.assertEqual(server["command"], "/usr/bin/python3")
        self.assertEqual(
            server["env"],
            {"CODEX_HOME": "/mnt/c/Users/Ada/.codex"},
        )

    def test_windows_and_wsl_switch_keep_runtime_local_plugin_state(self) -> None:
        windows = build_install_plan(
            r"D:\src\podotion-image",
            platform="windows",
            environ={
                "HOME": "/home/ada",
                "USERPROFILE": r"C:\Users\Ada",
                "CODEX_HOME": r"C:\Users\Ada\.codex",
            },
            python_executable=r"C:\Python314\python.exe",
            check_marker=False,
        )
        wsl = build_install_plan(
            "/src/podotion-image",
            platform="wsl",
            environ={
                "HOME": "/home/ada",
                "USERPROFILE": r"C:\Users\Ada",
                "CODEX_HOME": "/mnt/c/Users/Ada/.codex",
            },
            python_executable="/usr/bin/python3",
            check_marker=False,
        )

        self.assertEqual(
            windows.plugin_destination, r"C:\Users\Ada\plugins\podotion-image"
        )
        self.assertEqual(wsl.plugin_destination, "/home/ada/plugins/podotion-image")
        self.assertEqual(
            windows.marketplace_json,
            r"C:\Users\Ada\.agents\plugins\marketplace.json",
        )
        self.assertEqual(
            wsl.marketplace_json, "/home/ada/.agents/plugins/marketplace.json"
        )
        self.assertEqual(
            windows.platform_marker,
            r"C:\Users\Ada\.codex\.podotion-image-platform.json",
        )
        self.assertEqual(
            wsl.platform_marker,
            "/mnt/c/Users/Ada/.codex/.podotion-image-runtimes/wsl/"
            ".podotion-image-platform.json",
        )
        windows_mcp = json.loads(windows.mcp_json)["mcpServers"]["podotion-image"]
        wsl_mcp = json.loads(wsl.mcp_json)["mcpServers"]["podotion-image"]
        self.assertEqual(
            windows_mcp["env"], {"CODEX_HOME": r"C:\Users\Ada\.codex"}
        )
        self.assertEqual(
            wsl_mcp["env"], {"CODEX_HOME": "/mnt/c/Users/Ada/.codex"}
        )

    def test_marketplace_merge_preserves_order_and_interface(self) -> None:
        existing = {
            "name": "personal",
            "interface": {"displayName": "My Local Tools", "theme": "quiet"},
            "plugins": [{"name": "first"}],
        }
        payload, name = merge_marketplace(existing)
        self.assertEqual(name, "personal")
        self.assertEqual(payload["interface"], existing["interface"])
        self.assertEqual(
            [entry["name"] for entry in payload["plugins"]],
            ["first", "podotion-image"],
        )

    def test_marketplace_source_conflict_is_rejected(self) -> None:
        with self.assertRaises(MarketplaceConflictError):
            merge_marketplace(
                {
                    "name": "personal",
                    "plugins": [
                        {
                            "name": "podotion-image",
                            "source": {"source": "local", "path": "./somewhere-else"},
                        }
                    ],
                }
            )


class InstallTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_parent = "/tmp" if detect_platform() is PlatformKind.WSL else None
        self.temporary = tempfile.TemporaryDirectory(dir=temp_parent)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.source = self.root / "source"
        (self.source / ".codex-plugin").mkdir(parents=True)
        (self.source / "mcp").mkdir(parents=True)
        (self.source / ".codex-plugin" / "plugin.json").write_text(
            json.dumps({"name": "podotion-image", "version": "1.0.2"}) + "\n",
            encoding="utf-8",
        )
        (self.source / "mcp" / "server.py").write_text("# server\n", encoding="utf-8")
        (self.source / "content.txt").write_text("new\n", encoding="utf-8")
        self.platform = detect_platform()
        self.env = {"HOME": str(self.home)}
        if self.platform is PlatformKind.WINDOWS:
            self.env = {"USERPROFILE": str(self.home)}
        self.python_executable = str(Path(sys.executable).resolve())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def plan(self):
        return build_install_plan(
            str(self.source),
            platform=self.platform,
            environ=self.env,
            home=str(self.home),
            python_executable=self.python_executable,
            check_marker=False,
        )

    def test_success_commits_files_and_registers_codex(self) -> None:
        plan = self.plan()
        standalone = Path(plan.codex_home) / "skills" / "podotion_image"
        standalone_backup = Path(f"{standalone}.backup")
        standalone.mkdir(parents=True)
        standalone_backup.mkdir(parents=True)
        (standalone / "SKILL.md").write_text("standalone\n", encoding="utf-8")
        (standalone_backup / "SKILL.md").write_text(
            "standalone backup\n", encoding="utf-8"
        )
        provider = Path(plan.codex_home) / "podotion-image" / "provider.toml"
        provider.parent.mkdir(parents=True)
        provider.write_text("secret = true\n", encoding="utf-8")
        generated_image = self.home / "workspace" / "PodotionImage" / "one.png"
        generated_image.parent.mkdir(parents=True)
        generated_image.write_bytes(b"png")
        commands: list[tuple[str, ...]] = []

        def runner(command):
            commands.append(tuple(command))
            return SimpleNamespace(returncode=0)

        result = execute_install_plan(plan, command_runner=runner)
        destination = Path(result.plugin_destination)
        self.assertEqual((destination / "content.txt").read_text(), "new\n")
        mcp = json.loads((destination / ".mcp.json").read_text())
        self.assertEqual(
            mcp["mcpServers"]["podotion-image"]["command"],
            self.python_executable,
        )
        self.assertEqual(
            mcp["mcpServers"]["podotion-image"]["env"],
            {"CODEX_HOME": plan.codex_home},
        )
        source_manifest = json.loads(
            (self.source / ".codex-plugin" / "plugin.json").read_text()
        )
        installed_manifest = json.loads(
            (destination / ".codex-plugin" / "plugin.json").read_text()
        )
        self.assertEqual(source_manifest["version"], "1.0.2")
        self.assertRegex(
            installed_manifest["version"], r"^1\.0\.2\+codex\.[0-9a-f]{12}$"
        )
        marketplace = json.loads(Path(result.marketplace_json).read_text())
        self.assertEqual(marketplace["plugins"][0]["name"], "podotion-image")
        marker = json.loads(Path(result.platform_marker).read_text())
        self.assertEqual(marker["platform"], self.platform.value)
        self.assertEqual(
            (standalone / "SKILL.md").read_text(), "standalone\n"
        )
        self.assertEqual(
            (standalone_backup / "SKILL.md").read_text(),
            "standalone backup\n",
        )
        self.assertEqual(provider.read_text(), "secret = true\n")
        self.assertEqual(generated_image.read_bytes(), b"png")
        self.assertEqual(commands, [plan.codex_command])

    def test_fault_rolls_back_existing_plugin_and_marketplace(self) -> None:
        plan = self.plan()
        destination = Path(plan.plugin_destination)
        destination.mkdir(parents=True)
        (destination / "old.txt").write_text("old\n", encoding="utf-8")
        marketplace = Path(plan.marketplace_json)
        marketplace.parent.mkdir(parents=True, exist_ok=True)
        old_marketplace = {
            "name": "personal",
            "interface": {"displayName": "Personal"},
            "plugins": [{"name": "other"}],
        }
        old_text = json.dumps(old_marketplace) + "\n"
        marketplace.write_text(old_text, encoding="utf-8")
        plan = self.plan()

        def fault(step: str) -> None:
            if step == "marketplace_installed":
                raise RuntimeError("injected failure")

        with self.assertRaisesRegex(RuntimeError, "injected"):
            execute_install_plan(plan, run_codex=False, fault_hook=fault)

        self.assertEqual((destination / "old.txt").read_text(), "old\n")
        self.assertFalse((destination / "content.txt").exists())
        self.assertEqual(marketplace.read_text(), old_text)
        self.assertFalse(Path(plan.platform_marker).exists())

    def test_codex_failure_rolls_back_a_fresh_install(self) -> None:
        plan = self.plan()

        def failed(_command):
            return SimpleNamespace(returncode=7)

        with self.assertRaises(InstallError):
            execute_install_plan(plan, command_runner=failed)
        self.assertFalse(Path(plan.plugin_destination).exists())
        self.assertFalse(Path(plan.marketplace_json).exists())
        self.assertFalse(Path(plan.platform_marker).exists())

    def test_existing_standalone_skill_and_backup_are_untouched(self) -> None:
        plan = self.plan()
        standalone = Path(plan.codex_home) / "skills" / "podotion_image"
        backup = Path(f"{standalone}.backup")
        standalone.mkdir(parents=True)
        backup.mkdir(parents=True)
        (standalone / "SKILL.md").write_text("local development\n", encoding="utf-8")
        (backup / "SKILL.md").write_text("local backup\n", encoding="utf-8")

        execute_install_plan(plan, run_codex=False)

        self.assertEqual(
            (standalone / "SKILL.md").read_text(), "local development\n"
        )
        self.assertEqual((backup / "SKILL.md").read_text(), "local backup\n")

    def test_repeated_install_is_stable_and_content_change_updates_cachebuster(self) -> None:
        plan = self.plan()
        first = execute_install_plan(plan, run_codex=False)
        installed_manifest = Path(first.plugin_destination) / ".codex-plugin" / "plugin.json"
        first_version = json.loads(installed_manifest.read_text())["version"]

        second = execute_install_plan(self.plan(), run_codex=False)
        second_version = json.loads(
            (Path(second.plugin_destination) / ".codex-plugin" / "plugin.json").read_text()
        )["version"]
        self.assertEqual(second_version, first_version)

        (self.source / "content.txt").write_text("changed\n", encoding="utf-8")
        third = execute_install_plan(self.plan(), run_codex=False)
        third_version = json.loads(
            (Path(third.plugin_destination) / ".codex-plugin" / "plugin.json").read_text()
        )["version"]
        self.assertNotEqual(third_version, first_version)
        self.assertEqual((Path(third.plugin_destination) / "content.txt").read_text(), "changed\n")


class ManifestCachebusterTests(unittest.TestCase):
    def test_existing_build_metadata_is_replaced_without_mutating_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / ".codex-plugin" / "plugin.json"
            manifest.parent.mkdir(parents=True)
            source_text = json.dumps(
                {"name": "podotion-image", "version": "1.2.3-beta.1+source.7"}
            ) + "\n"
            manifest.write_text(source_text, encoding="utf-8")
            (root / "content.txt").write_text("content\n", encoding="utf-8")

            first = apply_manifest_cachebuster(root)
            manifest.write_text(source_text, encoding="utf-8")
            second = apply_manifest_cachebuster(root)

            self.assertEqual(first, second)
            self.assertRegex(first, r"^1\.2\.3-beta\.1\+codex\.[0-9a-f]{12}$")

    def test_invalid_manifest_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / ".codex-plugin" / "plugin.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps({"name": "podotion-image", "version": "release-1"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(InstallError, "invalid SemVer"):
                apply_manifest_cachebuster(root)


class InstallerCliTests(unittest.TestCase):
    def test_install_error_is_structured_without_traceback(self) -> None:
        stderr = io.StringIO()
        with mock.patch(
            "scripts.install.build_install_plan",
            side_effect=SharedCodexHomeError("use an independent CODEX_HOME"),
        ), contextlib.redirect_stderr(stderr):
            result = main(["--dry-run"])

        payload = json.loads(stderr.getvalue())
        self.assertEqual(result, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["type"], "SharedCodexHomeError")
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

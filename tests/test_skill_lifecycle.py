from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tests.support import SKILL_ROOT


MANAGER_PATH = SKILL_ROOT / "scripts" / "manage.py"


def load_module():
    spec = importlib.util.spec_from_file_location("podotion_image_manage_test", MANAGER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {MANAGER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SkillLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def make_skill(self, codex_home: Path, marker: str) -> Path:
        skill = codex_home / "skills" / "podotion-image"
        return self.make_source(skill, marker)

    def make_source(self, skill: Path, marker: str) -> Path:
        scripts = skill / "scripts"
        scripts.mkdir(parents=True)
        (skill / "agents").mkdir()
        (skill / "templates").mkdir()
        (skill / "SKILL.md").write_text(
            f"---\nname: podotion-image\ndescription: test\n---\n{marker}\n",
            encoding="utf-8",
        )
        for name in ("podotion_image.py", "configure_direct.py", "manage.py"):
            (scripts / name).write_text(f"# {marker}\n", encoding="utf-8")
        (skill / "agents" / "openai.yaml").write_text(
            'interface:\n  display_name: "Podotion Image"\n', encoding="utf-8"
        )
        (skill / "templates" / "provider.toml").write_text(
            'base_url = "https://ai.podotion.com/v1"\n', encoding="utf-8"
        )
        return skill

    def test_codex_home_uses_native_default_and_case_insensitive_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir).resolve()
            self.assertEqual(self.module.resolve_codex_home({}, home=home), home / ".codex")
            self.assertEqual(
                self.module.resolve_codex_home({"codex_home": "~/custom"}, home=home),
                (home / "custom").resolve(),
            )

    def test_status_never_reads_or_returns_secret(self) -> None:
        secret = "sk-status-must-not-leak"
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / ".codex"
            skill = self.make_skill(codex_home, "current")
            credential = codex_home / "podotion-image" / "provider.toml"
            credential.parent.mkdir(parents=True)
            credential.write_text(f'PodotionImageSk = "{secret}"\n', encoding="utf-8")
            report = self.module.build_status(skill, codex_home)

        self.assertTrue(report["managed_install"])
        self.assertTrue(report["credential"]["configured"])
        self.assertNotIn(secret, json.dumps(report))

    def test_status_reports_absent_legacy_plugin_as_not_removable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            codex_home = home / ".codex"
            skill = self.make_skill(codex_home, "current")

            with (
                mock.patch.object(self.module, "_run_command") as run_command,
                mock.patch.object(self.module, "_atomic_write_json") as write_json,
            ):
                report = self.module.build_status(skill, codex_home, user_home=home)

            run_command.assert_not_called()
            write_json.assert_not_called()

        self.assertEqual(
            report["legacy_plugin"],
            {
                "detected": False,
                "safe_to_remove": False,
                "marketplace": "absent",
                "registration": "absent",
                "source": "absent",
            },
        )

    def test_status_reports_owned_legacy_plugin_as_safe_without_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            codex_home = home / ".codex"
            skill = self.make_skill(codex_home, "current")
            source = home / "plugins" / "podotion-image"
            source.mkdir(parents=True)
            marketplace = home / ".agents" / "plugins" / "marketplace.json"
            marketplace.parent.mkdir(parents=True)
            original = json.dumps(
                {
                    "name": "personal",
                    "plugins": [
                        {
                            "name": "podotion-image",
                            "source": self.module.LEGACY_MARKETPLACE_SOURCE,
                        }
                    ],
                }
            )
            marketplace.write_text(original, encoding="utf-8")

            report = self.module.build_status(skill, codex_home, user_home=home)

            self.assertTrue(report["legacy_plugin"]["detected"])
            self.assertTrue(report["legacy_plugin"]["safe_to_remove"])
            self.assertEqual(report["legacy_plugin"]["registration"], "owned")
            self.assertEqual(report["legacy_plugin"]["source"], "directory")
            self.assertEqual(marketplace.read_text(encoding="utf-8"), original)
            self.assertTrue(source.is_dir())

    def test_status_marks_unsafe_legacy_plugin_variants_not_removable(self) -> None:
        cases = (
            "different-source",
            "marketplace-invalid-name",
            "source-non-directory",
            "source-symlink",
            "marketplace-symlink",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                home = Path(temp_dir)
                codex_home = home / ".codex"
                skill = self.make_skill(codex_home, "current")
                source = home / "plugins" / "podotion-image"
                marketplace = home / ".agents" / "plugins" / "marketplace.json"

                if case == "different-source":
                    marketplace.parent.mkdir(parents=True)
                    marketplace.write_text(
                        json.dumps(
                            {
                                "name": "personal",
                                "plugins": [
                                    {
                                        "name": "podotion-image",
                                        "source": {
                                            "source": "local",
                                            "path": "./plugins/not-owned",
                                        },
                                    }
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )
                elif case == "marketplace-invalid-name":
                    marketplace.parent.mkdir(parents=True)
                    marketplace.write_text(
                        json.dumps(
                            {
                                "name": "",
                                "plugins": [
                                    {
                                        "name": "podotion-image",
                                        "source": self.module.LEGACY_MARKETPLACE_SOURCE,
                                    }
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )
                elif case == "source-non-directory":
                    source.parent.mkdir(parents=True)
                    source.write_text("not a directory", encoding="utf-8")
                else:
                    target = home / "outside"
                    target.mkdir()
                    try:
                        if case == "source-symlink":
                            source.parent.mkdir(parents=True)
                            source.symlink_to(target, target_is_directory=True)
                        else:
                            marketplace.parent.mkdir(parents=True)
                            marketplace.symlink_to(target, target_is_directory=True)
                    except OSError as exc:
                        self.skipTest(f"symbolic links are unavailable: {exc}")

                report = self.module.build_status(skill, codex_home, user_home=home)

                self.assertTrue(report["legacy_plugin"]["detected"])
                self.assertFalse(report["legacy_plugin"]["safe_to_remove"])
                if case == "different-source":
                    self.assertEqual(report["legacy_plugin"]["registration"], "different-source")
                elif case == "marketplace-invalid-name":
                    self.assertEqual(report["legacy_plugin"]["marketplace"], "invalid")
                elif case == "source-non-directory":
                    self.assertEqual(report["legacy_plugin"]["source"], "non-directory")
                elif case == "source-symlink":
                    self.assertEqual(report["legacy_plugin"]["source"], "symlink")
                else:
                    self.assertEqual(report["legacy_plugin"]["marketplace"], "symlink")

    def test_update_replaces_skill_and_preserves_external_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            codex_home = base / ".codex"
            skill = self.make_skill(codex_home, "old")
            source = self.make_source(base / "checkout" / "podotion-image", "new")
            credential = codex_home / "podotion-image" / "provider.toml"
            credential.parent.mkdir(parents=True)
            credential.write_text("private", encoding="utf-8")

            report = self.module.apply_update(
                source, skill, codex_home, "a" * 40, smoke=False
            )

            self.assertTrue(report["updated"])
            self.assertIn("new", (skill / "SKILL.md").read_text(encoding="utf-8"))
            self.assertEqual(credential.read_text(encoding="utf-8"), "private")
            metadata = json.loads(
                (skill / self.module.METADATA_FILENAME).read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["revision"], "a" * 40)

    def test_failed_second_replace_restores_previous_install(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            codex_home = base / ".codex"
            skill = self.make_skill(codex_home, "old")
            source = self.make_source(base / "checkout" / "podotion-image", "new")
            calls = 0

            def failing_replace(left: Path, right: Path):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated install failure")
                return os.replace(left, right)

            with self.assertRaisesRegex(self.module.LifecycleError, "previous installation"):
                self.module.apply_update(
                    source,
                    skill,
                    codex_home,
                    "b" * 40,
                    replace=failing_replace,
                    smoke=False,
                )

            self.assertTrue(skill.is_dir())
            self.assertIn("old", (skill / "SKILL.md").read_text(encoding="utf-8"))
            transaction_root = codex_home / self.module.TRANSACTION_DIRECTORY
            self.assertFalse(
                transaction_root.exists() and any(transaction_root.iterdir())
            )

    def test_dry_run_does_not_modify_installation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            codex_home = base / ".codex"
            skill = self.make_skill(codex_home, "old")
            source = self.make_source(base / "checkout" / "podotion-image", "new")

            report = self.module.apply_update(
                source,
                skill,
                codex_home,
                "c" * 40,
                dry_run=True,
                smoke=False,
            )

            self.assertTrue(report["would_update"])
            self.assertFalse((skill / self.module.METADATA_FILENAME).exists())
            self.assertIn("old", (skill / "SKILL.md").read_text(encoding="utf-8"))

    def test_fixed_clone_and_windows_schannel_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            checkout = Path(temp_dir) / "repository"
            commands: list[list[str]] = []

            def runner(command):
                command = list(command)
                commands.append(command)
                if command[-2:] == ["rev-parse", "HEAD"]:
                    return subprocess.CompletedProcess(command, 0, "d" * 40 + "\n", "")
                if len(commands) == 1:
                    checkout.mkdir()
                    return subprocess.CompletedProcess(
                        command, 1, "", "schannel SSL SEC_E_NO_CREDENTIALS"
                    )
                checkout.mkdir()
                return subprocess.CompletedProcess(command, 0, "", "")

            revision = self.module.fetch_repository(
                checkout, runner=runner, native_windows=True
            )

        self.assertEqual(revision, "d" * 40)
        self.assertEqual(commands[0][0:4], ["git", "clone", "--depth", "1"])
        self.assertIn("--branch", commands[0])
        self.assertIn("main", commands[0])
        self.assertIn(self.module.REPOSITORY_URL, commands[0])
        self.assertEqual(commands[1][:4], ["git", "-c", "http.sslBackend=openssl", "clone"])
        self.assertFalse(any("sslVerify=false" in part for command in commands for part in command))

    def test_non_windows_or_non_schannel_failures_are_not_retried(self) -> None:
        cases = (
            (False, "schannel SSL handshake"),
            (True, "repository not found"),
        )
        for native_windows, error in cases:
            with self.subTest(native_windows=native_windows, error=error):
                calls = []

                def runner(command):
                    calls.append(list(command))
                    return subprocess.CompletedProcess(command, 1, "", error)

                with tempfile.TemporaryDirectory() as temp_dir:
                    with self.assertRaisesRegex(self.module.LifecycleError, "checkout failed"):
                        self.module.fetch_repository(
                            Path(temp_dir) / "repository",
                            runner=runner,
                            native_windows=native_windows,
                        )
                self.assertEqual(len(calls), 1)

    def test_candidate_rejects_wrong_name_symlink_and_syntax_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_source(Path(temp_dir) / "podotion-image", "candidate")
            (root / "scripts" / "podotion_image.py").write_text("if (\n", encoding="utf-8")
            with self.assertRaisesRegex(self.module.LifecycleError, "Python source"):
                self.module.validate_update_source(root, smoke=False)

    def test_candidate_requires_runtime_template_and_agents_metadata(self) -> None:
        for missing in ("templates/provider.toml", "agents/openai.yaml"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temp_dir:
                root = self.make_source(Path(temp_dir) / "podotion-image", "candidate")
                (root / missing).unlink()
                with self.assertRaisesRegex(self.module.LifecycleError, "required files"):
                    self.module.validate_update_source(root, smoke=False)

    def test_legacy_plugin_uninstall_removes_only_owned_registration_and_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            codex_home = home / ".codex"
            skill = self.make_skill(codex_home, "current")
            credential = codex_home / "podotion-image" / "provider.toml"
            credential.parent.mkdir(parents=True)
            credential.write_text("private", encoding="utf-8")
            source = home / "plugins" / "podotion-image"
            source.mkdir(parents=True)
            (source / "marker.txt").write_text("legacy", encoding="utf-8")
            marketplace = home / ".agents" / "plugins" / "marketplace.json"
            marketplace.parent.mkdir(parents=True)
            unrelated = {
                "name": "another-plugin",
                "source": {"source": "local", "path": "./plugins/another-plugin"},
            }
            marketplace.write_text(
                json.dumps(
                    {
                        "name": "team-tools",
                        "interface": {"displayName": "Personal"},
                        "plugins": [
                            unrelated,
                            {
                                "name": "podotion-image",
                                "source": self.module.LEGACY_MARKETPLACE_SOURCE,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            commands: list[list[str]] = []

            def runner(command):
                commands.append(list(command))
                return subprocess.CompletedProcess(command, 0, "removed", "")

            report = self.module.uninstall_legacy_plugin(
                skill,
                codex_home,
                confirmed=True,
                user_home=home,
                runner=runner,
            )
            saved_marketplace = json.loads(marketplace.read_text(encoding="utf-8"))
            source_exists = source.exists()
            credential_content = credential.read_text(encoding="utf-8")

        self.assertEqual(
            commands,
            [["codex", "plugin", "remove", "podotion-image@team-tools"]],
        )
        self.assertTrue(report["uninstalled"])
        self.assertTrue(report["restart_required"])
        self.assertFalse(source_exists)
        self.assertEqual(saved_marketplace["plugins"], [unrelated])
        self.assertEqual(credential_content, "private")

    def test_legacy_plugin_uninstall_requires_confirmation_and_cli_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            codex_home = home / ".codex"
            skill = self.make_skill(codex_home, "current")
            source = home / "plugins" / "podotion-image"
            source.mkdir(parents=True)
            marketplace = home / ".agents" / "plugins" / "marketplace.json"
            marketplace.parent.mkdir(parents=True)
            original = json.dumps(
                {
                    "name": "personal",
                    "plugins": [
                        {
                            "name": "podotion-image",
                            "source": self.module.LEGACY_MARKETPLACE_SOURCE,
                        }
                    ],
                }
            )
            marketplace.write_text(original, encoding="utf-8")

            with self.assertRaisesRegex(self.module.LifecycleError, "--yes"):
                self.module.uninstall_legacy_plugin(
                    skill,
                    codex_home,
                    confirmed=False,
                    user_home=home,
                )

            def failing_runner(command):
                return subprocess.CompletedProcess(command, 1, "", "permission denied")

            with self.assertRaisesRegex(self.module.LifecycleError, "left unchanged"):
                self.module.uninstall_legacy_plugin(
                    skill,
                    codex_home,
                    confirmed=True,
                    user_home=home,
                    runner=failing_runner,
                )

            self.assertTrue(source.is_dir())
            self.assertEqual(marketplace.read_text(encoding="utf-8"), original)

    def test_legacy_plugin_uninstall_rejects_same_name_different_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            codex_home = home / ".codex"
            skill = self.make_skill(codex_home, "current")
            source = home / "plugins" / "podotion-image"
            source.mkdir(parents=True)
            marketplace = home / ".agents" / "plugins" / "marketplace.json"
            marketplace.parent.mkdir(parents=True)
            marketplace.write_text(
                json.dumps(
                    {
                        "name": "personal",
                        "plugins": [
                            {
                                "name": "podotion-image",
                                "source": {
                                    "source": "local",
                                    "path": "./plugins/not-owned",
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(self.module.LifecycleError, "different Marketplace"):
                self.module.uninstall_legacy_plugin(
                    skill,
                    codex_home,
                    confirmed=True,
                    user_home=home,
                )
            self.assertTrue(source.is_dir())

    def test_legacy_plugin_uninstall_is_a_noop_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            codex_home = home / ".codex"
            skill = self.make_skill(codex_home, "current")
            called = False

            def runner(command):
                nonlocal called
                called = True
                return subprocess.CompletedProcess(command, 0, "", "")

            report = self.module.uninstall_legacy_plugin(
                skill,
                codex_home,
                confirmed=True,
                user_home=home,
                runner=runner,
            )

        self.assertFalse(called)
        self.assertFalse(report["legacy_plugin_found"])
        self.assertFalse(report["uninstalled"])

    def test_uninstall_requires_confirmation_and_preserves_user_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / ".codex"
            skill = self.make_skill(codex_home, "current")
            credential = codex_home / "podotion-image" / "provider.toml"
            credential.parent.mkdir(parents=True)
            credential.write_text("private", encoding="utf-8")

            with self.assertRaisesRegex(self.module.LifecycleError, "--yes"):
                self.module.uninstall_skill(skill, codex_home, confirmed=False)
            report = self.module.uninstall_skill(skill, codex_home, confirmed=True)

            self.assertTrue(report["uninstalled"])
            self.assertFalse(skill.exists())
            self.assertEqual(credential.read_text(encoding="utf-8"), "private")

    def test_uninstall_cleanup_failure_does_not_restore_partial_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / ".codex"
            skill = self.make_skill(codex_home, "current")

            def fail_cleanup(_path: Path) -> None:
                raise OSError("simulated cleanup failure")

            report = self.module.uninstall_skill(
                skill,
                codex_home,
                confirmed=True,
                remove_tree=fail_cleanup,
            )

            self.assertFalse(skill.exists())
            self.assertIn("warning", report)
            self.assertTrue(any((codex_home / self.module.TRASH_DIRECTORY).iterdir()))

    def test_installed_manager_can_remove_its_own_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            codex_home = base / ".codex"
            installed = codex_home / "skills" / "podotion-image"
            installed.parent.mkdir(parents=True)
            shutil.copytree(SKILL_ROOT, installed)
            credential = codex_home / "podotion-image" / "provider.toml"
            credential.parent.mkdir(parents=True)
            credential.write_text("private", encoding="utf-8")
            environ = os.environ.copy()
            environ["CODEX_HOME"] = str(codex_home)

            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(installed / "scripts" / "manage.py"),
                    "uninstall",
                    "--yes",
                ],
                cwd=base,
                env=environ,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )

            report = json.loads(result.stdout)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(report["uninstalled"])
            self.assertFalse(installed.exists())
            self.assertEqual(credential.read_text(encoding="utf-8"), "private")


if __name__ == "__main__":
    unittest.main()

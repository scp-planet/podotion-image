from __future__ import annotations

import subprocess
import unittest
from types import SimpleNamespace

from podotion_image.paths import (
    PathConversionError,
    PlatformKind,
    codex_home_path,
    describe_path,
    detect_platform,
    file_uri,
    resolve_workspace_path,
    resource_uri,
    runtime_home_path,
    windows_to_wsl,
    wsl_to_windows,
)


class PlatformDetectionTests(unittest.TestCase):
    def test_detects_all_supported_platforms(self) -> None:
        self.assertEqual(detect_platform("win32"), PlatformKind.WINDOWS)
        self.assertEqual(detect_platform("darwin"), PlatformKind.MACOS)
        self.assertEqual(
            detect_platform("linux", environ={}, os_release="6.8.0"),
            PlatformKind.LINUX,
        )
        self.assertEqual(
            detect_platform(
                "linux",
                environ={"WSL_DISTRO_NAME": "Ubuntu"},
                os_release="6.8.0",
            ),
            PlatformKind.WSL,
        )
        self.assertEqual(
            detect_platform("linux", environ={}, os_release="microsoft-standard-WSL2"),
            PlatformKind.WSL,
        )


class PathResolutionTests(unittest.TestCase):
    def test_runtime_and_codex_home_are_platform_aware(self) -> None:
        shared = {
            "HOME": "/home/ada",
            "USERPROFILE": r"C:\Users\Ada",
        }
        self.assertEqual(
            runtime_home_path(
                platform="wsl", environ=shared, os_release="microsoft-wsl"
            ),
            "/home/ada",
        )
        self.assertEqual(
            codex_home_path(
                platform="wsl", environ=shared, os_release="microsoft-wsl"
            ),
            "/home/ada/.codex",
        )
        self.assertEqual(
            codex_home_path(
                platform="windows",
                environ={"USERPROFILE": r"C:\Users\Ada"},
            ),
            r"C:\Users\Ada\.codex",
        )
        for platform, home in (("macos", "/Users/ada"), ("linux", "/home/ada")):
            with self.subTest(platform=platform):
                environ = {"HOME": home, "USERPROFILE": r"C:\Users\Wrong"}
                self.assertEqual(
                    runtime_home_path(platform=platform, environ=environ),
                    home,
                )
                self.assertEqual(
                    codex_home_path(platform=platform, environ=environ),
                    f"{home}/.codex",
                )

    def test_explicit_codex_home_has_priority_in_wsl(self) -> None:
        self.assertEqual(
            codex_home_path(
                platform="wsl",
                environ={
                    "HOME": "/home/ada",
                    "USERPROFILE": r"C:\Users\Ada",
                    "CODEX_HOME": "/mnt/c/Users/Ada/.codex",
                },
                os_release="microsoft-wsl",
            ),
            "/mnt/c/Users/Ada/.codex",
        )

    def test_windows_drive_path_and_public_descriptions(self) -> None:
        result = describe_path(
            r"C:\Work Space\生成.png",
            request_id="req/42",
            platform="windows",
        )
        self.assertEqual(result["path"], r"C:\Work Space\生成.png")
        self.assertEqual(result["markdown_path"], "C:/Work Space/生成.png")
        self.assertEqual(
            result["file_uri"],
            "file:///C:/Work%20Space/%E7%94%9F%E6%88%90.png",
        )
        self.assertEqual(
            result["resource_uri"],
            "podotion-image://outputs/req%2F42/%E7%94%9F%E6%88%90.png",
        )

    def test_windows_unc_path_uses_rfc_8089_authority(self) -> None:
        path = r"\\server\shared images\result one.png"
        result = describe_path(path, request_id="unc", platform="windows")
        self.assertEqual(result["path"], path)
        self.assertEqual(
            result["markdown_path"], "//server/shared images/result one.png"
        )
        self.assertEqual(
            result["file_uri"],
            "file://server/shared%20images/result%20one.png",
        )

    def test_workspace_relative_paths_use_native_rules(self) -> None:
        self.assertEqual(
            resolve_workspace_path(
                r"outputs\one.png",
                workspace=r"D:\Projects\chat",
                platform="windows",
            ),
            r"D:\Projects\chat\outputs\one.png",
        )
        for platform in ("macos", "linux"):
            with self.subTest(platform=platform):
                self.assertEqual(
                    resolve_workspace_path(
                        "outputs/one.png",
                        workspace="/Users/ada/chat" if platform == "macos" else "/home/ada/chat",
                        platform=platform,
                    ),
                    "/Users/ada/chat/outputs/one.png"
                    if platform == "macos"
                    else "/home/ada/chat/outputs/one.png",
                )

    def test_relative_path_requires_workspace(self) -> None:
        with self.assertRaisesRegex(PathConversionError, "workspace"):
            resolve_workspace_path("output.png", platform="linux")

    def test_foreign_paths_are_rejected_on_native_macos_and_linux(self) -> None:
        for platform in ("macos", "linux"):
            with self.subTest(platform=platform):
                with self.assertRaisesRegex(PathConversionError, "Windows paths"):
                    resolve_workspace_path(
                        r"C:\Temp\output.png",
                        platform=platform,
                        environ={},
                        os_release="generic",
                    )

    def test_posix_file_uri_encodes_spaces_and_unicode(self) -> None:
        self.assertEqual(
            file_uri("/Users/ada/Image Work/生成.png", platform="macos"),
            "file:///Users/ada/Image%20Work/%E7%94%9F%E6%88%90.png",
        )

    def test_current_user_home_expands_with_native_platform_environment(self) -> None:
        cases = (
            (
                "windows",
                {"USERPROFILE": r"C:\Users\Ada"},
                r"~\Pictures\result.png",
                r"C:\Users\Ada\Pictures\result.png",
            ),
            (
                "macos",
                {"HOME": "/Users/ada"},
                "~/Pictures/result.png",
                "/Users/ada/Pictures/result.png",
            ),
            (
                "linux",
                {"HOME": "/home/ada"},
                "~/Pictures/result.png",
                "/home/ada/Pictures/result.png",
            ),
            (
                "wsl",
                {"HOME": "/home/ada"},
                "~/Pictures/result.png",
                "/home/ada/Pictures/result.png",
            ),
        )
        for platform, environ, value, expected in cases:
            with self.subTest(platform=platform):
                self.assertEqual(
                    resolve_workspace_path(
                        value,
                        platform=platform,
                        environ=environ,
                        os_release="generic",
                    ),
                    expected,
                )

    def test_macos_volume_path_remains_absolute(self) -> None:
        self.assertEqual(
            resolve_workspace_path(
                "/Volumes/Creative Drive/PodotionImage",
                platform="macos",
                environ={"HOME": "/Users/ada"},
            ),
            "/Volumes/Creative Drive/PodotionImage",
        )


class WslPathTests(unittest.TestCase):
    def test_windows_path_is_converted_with_wslpath(self) -> None:
        commands: list[tuple[str, ...]] = []

        def runner(command):
            commands.append(tuple(command))
            return SimpleNamespace(
                returncode=0, stdout="/mnt/c/Users/Ada/output image.png\n"
            )

        result = resolve_workspace_path(
            r"C:\Users\Ada\output image.png",
            platform="wsl",
            wslpath_runner=runner,
        )
        self.assertEqual(result, "/mnt/c/Users/Ada/output image.png")
        self.assertEqual(
            commands, [("wslpath", "-u", r"C:\Users\Ada\output image.png")]
        )

    def test_wsl_workspace_can_also_arrive_as_a_windows_path(self) -> None:
        def runner(command):
            self.assertEqual(command[1], "-u")
            return "/mnt/d/Chat Work"

        self.assertEqual(
            resolve_workspace_path(
                "outputs/result.png",
                workspace=r"D:\Chat Work",
                platform="wsl",
                wslpath_runner=runner,
            ),
            "/mnt/d/Chat Work/outputs/result.png",
        )

    def test_wslpath_both_directions_and_error(self) -> None:
        self.assertEqual(
            windows_to_wsl(
                r"C:\Temp\a.png", runner=lambda _: "/mnt/c/Temp/a.png\n"
            ),
            "/mnt/c/Temp/a.png",
        )
        self.assertEqual(
            wsl_to_windows(
                "/home/ada/a.png", runner=lambda _: "C:\\WSL\\a.png\n"
            ),
            r"C:\WSL\a.png",
        )

        def failed(_command):
            raise subprocess.CalledProcessError(1, "wslpath")

        with self.assertRaises(PathConversionError):
            windows_to_wsl(r"C:\Temp\a.png", runner=failed)


class ResourceUriTests(unittest.TestCase):
    def test_resource_uri_never_contains_the_local_path(self) -> None:
        uri = resource_uri("request 1", "private image.png")
        self.assertEqual(
            uri,
            "podotion-image://outputs/request%201/private%20image.png",
        )
        self.assertNotIn("Users", uri)

    def test_resource_uri_rejects_path_components(self) -> None:
        with self.assertRaises(ValueError):
            resource_uri("request", "directory/image.png")


if __name__ == "__main__":
    unittest.main()

"""Cross-platform path normalization and public image path descriptions."""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
import subprocess
import sys
from enum import Enum
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
from typing import Callable, Mapping, Sequence
from urllib.parse import quote


class PlatformKind(str, Enum):
    WINDOWS = "windows"
    WSL = "wsl"
    MACOS = "macos"
    LINUX = "linux"


class PathConversionError(ValueError):
    """Raised when a path cannot be made accessible on the target platform."""


WslPathRunner = Callable[[Sequence[str]], object]

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_WSL_WINDOWS_MOUNT_RE = re.compile(r"^/mnt/[A-Za-z](?:/|$)")


def detect_platform(
    platform: str | PlatformKind | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    os_release: str | None = None,
) -> PlatformKind:
    """Return a stable platform name, distinguishing WSL from native Linux."""

    if isinstance(platform, PlatformKind):
        return platform

    value = (platform or sys.platform).strip().lower()
    aliases = {
        "windows": PlatformKind.WINDOWS,
        "win32": PlatformKind.WINDOWS,
        "cygwin": PlatformKind.WINDOWS,
        "msys": PlatformKind.WINDOWS,
        "wsl": PlatformKind.WSL,
        "mac": PlatformKind.MACOS,
        "macos": PlatformKind.MACOS,
        "darwin": PlatformKind.MACOS,
        "linux": PlatformKind.LINUX,
        "linux2": PlatformKind.LINUX,
    }
    if value not in aliases:
        raise ValueError(f"unsupported platform: {platform!r}")

    result = aliases[value]
    if result is not PlatformKind.LINUX:
        return result

    env = os.environ if environ is None else environ
    release = os_release
    if release is None:
        try:
            release = os.uname().release
        except AttributeError:
            release = ""
    if env.get("WSL_DISTRO_NAME") or env.get("WSL_INTEROP"):
        return PlatformKind.WSL
    if "microsoft" in release.lower() or "wsl" in release.lower():
        return PlatformKind.WSL
    return PlatformKind.LINUX


def is_windows_absolute(value: str | os.PathLike[str]) -> bool:
    text = os.fspath(value)
    return bool(_WINDOWS_DRIVE_RE.match(text)) or text.startswith(("\\\\", "//"))


def is_wsl_windows_mount(value: str | os.PathLike[str]) -> bool:
    return bool(_WSL_WINDOWS_MOUNT_RE.match(os.fspath(value)))


def _run_wslpath(
    value: str,
    flag: str,
    runner: WslPathRunner | None,
) -> str:
    command = ("wslpath", flag, value)
    try:
        if runner is None:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            completed = runner(command)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise PathConversionError(f"wslpath could not convert {value!r}") from exc

    if isinstance(completed, str):
        output = completed
    else:
        return_code = getattr(completed, "returncode", 0)
        if return_code:
            raise PathConversionError(f"wslpath could not convert {value!r}")
        output = getattr(completed, "stdout", "")
    converted = str(output).strip()
    if not converted:
        raise PathConversionError(f"wslpath returned no path for {value!r}")
    return converted


def windows_to_wsl(
    value: str | os.PathLike[str],
    *,
    runner: WslPathRunner | None = None,
) -> str:
    """Convert a Windows path with WSL's own mount and distro rules."""

    text = os.fspath(value)
    if not is_windows_absolute(text):
        raise PathConversionError(f"not an absolute Windows path: {text!r}")
    return posixpath.normpath(_run_wslpath(text, "-u", runner))


def wsl_to_windows(
    value: str | os.PathLike[str],
    *,
    runner: WslPathRunner | None = None,
) -> str:
    """Convert an absolute WSL path to the Windows-visible path."""

    text = os.fspath(value)
    if not posixpath.isabs(text):
        raise PathConversionError(f"not an absolute WSL path: {text!r}")
    return ntpath.normpath(_run_wslpath(text, "-w", runner))


def _normalize_windows(value: str) -> str:
    normalized = ntpath.normpath(value.replace("/", "\\"))
    if not is_windows_absolute(normalized):
        raise PathConversionError(f"not an absolute Windows path: {value!r}")
    return normalized


def _normalize_posix(value: str) -> str:
    normalized = posixpath.normpath(value)
    if not posixpath.isabs(normalized):
        raise PathConversionError(f"not an absolute POSIX path: {value!r}")
    return normalized


def _environment_value(
    environ: Mapping[str, str], key: str, kind: PlatformKind
) -> str | None:
    if kind is not PlatformKind.WINDOWS:
        return environ.get(key)
    wanted = key.casefold()
    for candidate, value in environ.items():
        if candidate.casefold() == wanted:
            return value
    return None


def _expand_current_user(
    value: str,
    kind: PlatformKind,
    environ: Mapping[str, str],
) -> str:
    separators = ("\\", "/") if kind is PlatformKind.WINDOWS else ("/",)
    if value == "~":
        suffix = ""
    elif any(value.startswith(f"~{separator}") for separator in separators):
        suffix = value[2:]
    elif value.startswith("~"):
        raise PathConversionError("named-user home paths are not supported")
    else:
        return value

    home_key = "USERPROFILE" if kind is PlatformKind.WINDOWS else "HOME"
    home = _environment_value(environ, home_key, kind)
    if not home:
        raise PathConversionError(f"cannot expand ~ without {home_key}")
    if not suffix:
        return home
    module = ntpath if kind is PlatformKind.WINDOWS else posixpath
    return module.join(home, suffix)


def resolve_workspace_path(
    value: str | os.PathLike[str],
    *,
    workspace: str | os.PathLike[str] | None = None,
    platform: str | PlatformKind | None = None,
    environ: Mapping[str, str] | None = None,
    os_release: str | None = None,
    wslpath_runner: WslPathRunner | None = None,
) -> str:
    """Resolve a native absolute path, using workspace for relative values."""

    text = os.fspath(value)
    if not text:
        raise PathConversionError("path must not be empty")
    env = os.environ if environ is None else environ
    kind = detect_platform(platform, environ=env, os_release=os_release)
    text = _expand_current_user(text, kind, env)

    if kind is PlatformKind.WINDOWS:
        if is_windows_absolute(text):
            return _normalize_windows(text)
        if posixpath.isabs(text):
            raise PathConversionError(
                "POSIX paths are not directly accessible from native Windows"
            )
        if workspace is None:
            raise PathConversionError("relative paths require a workspace")
        base = _normalize_windows(os.fspath(workspace))
        return _normalize_windows(ntpath.join(base, text))

    if kind is PlatformKind.WSL:
        if is_windows_absolute(text):
            return windows_to_wsl(text, runner=wslpath_runner)
        if posixpath.isabs(text):
            return _normalize_posix(text)
        if workspace is None:
            raise PathConversionError("relative paths require a workspace")
        base_text = os.fspath(workspace)
        if is_windows_absolute(base_text):
            base_text = windows_to_wsl(base_text, runner=wslpath_runner)
        base = _normalize_posix(base_text)
        return _normalize_posix(posixpath.join(base, text))

    if is_windows_absolute(text):
        raise PathConversionError(
            f"Windows paths are not directly accessible from {kind.value}"
        )
    if posixpath.isabs(text):
        return _normalize_posix(text)
    if workspace is None:
        raise PathConversionError("relative paths require a workspace")
    base_text = os.fspath(workspace)
    if is_windows_absolute(base_text):
        raise PathConversionError(
            f"a Windows workspace is not accessible from {kind.value}"
        )
    return _normalize_posix(posixpath.join(_normalize_posix(base_text), text))


def runtime_home_path(
    *,
    platform: str | PlatformKind | None = None,
    environ: Mapping[str, str] | None = None,
    os_release: str | None = None,
) -> str:
    """Return the current runtime's native home directory.

    WSL intentionally follows its Linux HOME even when USERPROFILE is also
    inherited from Windows. Only a native Windows process uses USERPROFILE.
    """

    env = os.environ if environ is None else environ
    kind = detect_platform(platform, environ=env, os_release=os_release)
    key = "USERPROFILE" if kind is PlatformKind.WINDOWS else "HOME"
    value = _environment_value(env, key, kind)
    if not value:
        if environ is not None:
            raise PathConversionError(f"cannot locate runtime home without {key}")
        value = str(Path.home())
    expanded = _expand_current_user(value, kind, env)
    if kind is PlatformKind.WINDOWS:
        return _normalize_windows(expanded)
    return _normalize_posix(expanded)


def codex_home_path(
    *,
    platform: str | PlatformKind | None = None,
    environ: Mapping[str, str] | None = None,
    os_release: str | None = None,
    wslpath_runner: WslPathRunner | None = None,
) -> str:
    """Return CODEX_HOME or its platform-aware runtime default."""

    env = os.environ if environ is None else environ
    kind = detect_platform(platform, environ=env, os_release=os_release)
    configured = _environment_value(env, "CODEX_HOME", kind)
    if configured:
        return resolve_workspace_path(
            configured,
            platform=kind,
            environ=env,
            os_release=os_release,
            wslpath_runner=wslpath_runner,
        )
    home = runtime_home_path(
        platform=kind,
        environ=env,
        os_release=os_release,
    )
    module = ntpath if kind is PlatformKind.WINDOWS else posixpath
    return module.normpath(module.join(home, ".codex"))


def to_native_path(
    value: str | os.PathLike[str],
    **kwargs: object,
) -> str:
    """Compatibility alias for :func:`resolve_workspace_path`."""

    return resolve_workspace_path(value, **kwargs)


def markdown_path(
    value: str | os.PathLike[str],
    *,
    platform: str | PlatformKind | None = None,
    environ: Mapping[str, str] | None = None,
    os_release: str | None = None,
) -> str:
    """Return the absolute path syntax accepted by Codex Markdown previews."""

    text = os.fspath(value)
    kind = detect_platform(platform, environ=environ, os_release=os_release)
    if kind is PlatformKind.WINDOWS:
        return _normalize_windows(text).replace("\\", "/")
    return _normalize_posix(text)


def file_uri(
    value: str | os.PathLike[str],
    *,
    platform: str | PlatformKind | None = None,
    environ: Mapping[str, str] | None = None,
    os_release: str | None = None,
) -> str:
    """Return an RFC 8089 file URI for a native absolute path."""

    text = os.fspath(value)
    kind = detect_platform(platform, environ=environ, os_release=os_release)
    if kind is PlatformKind.WINDOWS:
        normalized = _normalize_windows(text)
        windows_path = PureWindowsPath(normalized)
        if windows_path.anchor.startswith("\\"):
            # UNC anchors have the form ``\\server\share\``.
            parts = normalized.lstrip("\\").split("\\")
            if len(parts) < 2:
                raise PathConversionError(f"invalid UNC path: {text!r}")
            authority = quote(parts[0], safe="")
            uri_path = "/".join(quote(part, safe="") for part in parts[1:])
            return f"file://{authority}/{uri_path}"
        drive = windows_path.drive.rstrip(":").upper()
        components = [quote(part, safe="") for part in windows_path.parts[1:]]
        suffix = "/".join(components)
        return f"file:///{drive}:/{suffix}" if suffix else f"file:///{drive}:/"

    normalized = _normalize_posix(text)
    encoded = quote(normalized, safe="/")
    return f"file://{encoded}"


def resource_uri(request_id: str, basename: str) -> str:
    """Return a stable MCP resource URI without exposing a local filesystem path."""

    if not request_id or not request_id.strip():
        raise ValueError("request_id must not be empty")
    if not basename or basename in {".", ".."} or "/" in basename or "\\" in basename:
        raise ValueError("basename must be a single file name")
    return (
        "podotion-image://outputs/"
        f"{quote(request_id, safe='')}/{quote(basename, safe='')}"
    )


def describe_path(
    value: str | os.PathLike[str],
    *,
    request_id: str,
    workspace: str | os.PathLike[str] | None = None,
    platform: str | PlatformKind | None = None,
    environ: Mapping[str, str] | None = None,
    os_release: str | None = None,
    wslpath_runner: WslPathRunner | None = None,
) -> dict[str, str]:
    """Describe one output path for JSON, Markdown, files, and MCP resources."""

    kind = detect_platform(platform, environ=environ, os_release=os_release)
    native_path = resolve_workspace_path(
        value,
        workspace=workspace,
        platform=kind,
        environ=environ,
        os_release=os_release,
        wslpath_runner=wslpath_runner,
    )
    if kind is PlatformKind.WINDOWS:
        basename = PureWindowsPath(native_path).name
    else:
        basename = PurePosixPath(native_path).name
    if not basename:
        raise PathConversionError("output path must include a file name")
    return {
        "path": native_path,
        "markdown_path": markdown_path(native_path, platform=kind),
        "file_uri": file_uri(native_path, platform=kind),
        "resource_uri": resource_uri(request_id, basename),
    }


__all__ = [
    "PathConversionError",
    "PlatformKind",
    "describe_path",
    "detect_platform",
    "file_uri",
    "is_windows_absolute",
    "is_wsl_windows_mount",
    "markdown_path",
    "resolve_workspace_path",
    "resource_uri",
    "to_native_path",
    "windows_to_wsl",
    "wsl_to_windows",
]

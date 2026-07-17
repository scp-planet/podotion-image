#!/usr/bin/env python3
"""Install the Podotion image plugin into the platform's personal marketplace."""

from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from podotion_image.paths import (  # noqa: E402
    PlatformKind,
    detect_platform,
    is_windows_absolute,
    is_wsl_windows_mount,
    resolve_workspace_path,
)


PLUGIN_NAME = "podotion-image"
MARKETPLACE_SOURCE = f"./plugins/{PLUGIN_NAME}"
PLATFORM_MARKER = ".podotion-image-platform.json"
RUNTIME_DIRECTORY = ".podotion-image-runtimes"
PLUGIN_MANIFEST = ".codex-plugin/plugin.json"
_SEMVER_RE = re.compile(
    r"^(?P<core>"
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
    r")"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class InstallError(RuntimeError):
    """Base class for installation failures."""


class SharedCodexHomeError(InstallError):
    """Raised when CODEX_HOME is shared across incompatible host platforms."""


class MarketplaceConflictError(InstallError):
    """Raised when an existing marketplace entry points to another source."""


@dataclass(frozen=True)
class InstallOperation:
    action: str
    target: str
    source: str | None = None


@dataclass(frozen=True)
class InstallPlan:
    platform: PlatformKind
    plugin_source: str
    plugin_destination: str
    codex_home: str
    platform_marker: str
    legacy_skill: str
    legacy_backup: str
    marketplace_json: str
    marketplace_name: str
    mcp_json: str
    python_executable: str
    codex_command: tuple[str, ...]
    codex_rollback_command: tuple[str, ...]
    operations: tuple[InstallOperation, ...]

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["platform"] = self.platform.value
        payload["codex_command"] = list(self.codex_command)
        return payload


@dataclass(frozen=True)
class InstallResult:
    plugin_destination: str
    marketplace_json: str
    platform_marker: str
    legacy_skill: str
    legacy_backup: str
    legacy_retired: bool
    codex_registered: bool


CommandRunner = Callable[[Sequence[str]], object]
FaultHook = Callable[[str], None]


def _path_module(kind: PlatformKind):
    return ntpath if kind is PlatformKind.WINDOWS else posixpath


def _join(kind: PlatformKind, *parts: str) -> str:
    return _path_module(kind).normpath(_path_module(kind).join(*parts))


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


def detect_user_home(
    *,
    platform: str | PlatformKind | None = None,
    environ: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
    os_release: str | None = None,
) -> str:
    """Resolve the current platform's user profile without crossing OS boundaries."""

    env = os.environ if environ is None else environ
    kind = detect_platform(platform, environ=env, os_release=os_release)
    if home is not None:
        candidate = os.fspath(home)
    elif kind is PlatformKind.WINDOWS:
        candidate = _environment_value(env, "USERPROFILE", kind) or ""
    else:
        candidate = env.get("HOME", "")
    if not candidate:
        raise InstallError(f"cannot determine the {kind.value} user home")
    try:
        return resolve_workspace_path(candidate, platform=kind, environ=env)
    except ValueError as exc:
        raise InstallError(f"invalid {kind.value} user home: {candidate!r}") from exc


def _reject_shared_codex_home(codex_home: str, kind: PlatformKind) -> None:
    lowered = codex_home.lower().replace("/", "\\")
    if kind is PlatformKind.WINDOWS:
        if lowered.startswith(("\\\\wsl$\\", "\\\\wsl.localhost\\")):
            raise SharedCodexHomeError(
                "Windows CODEX_HOME must not point into a WSL filesystem; "
                "use %USERPROFILE%\\.codex"
            )
        return

    if kind is PlatformKind.WSL and is_wsl_windows_mount(codex_home):
        # Codex Desktop may intentionally pass the Windows profile into a WSL
        # runtime. This path is accepted; platform_marker_path() and
        # legacy_skill_paths() isolate the WSL-owned state below it.
        return

    if is_windows_absolute(codex_home) or is_wsl_windows_mount(codex_home):
        suggested = "~/.codex"
        raise SharedCodexHomeError(
            f"{kind.value} CODEX_HOME must not use a Windows-mounted profile; "
            f"use {suggested} for this platform"
        )


def platform_marker_path(codex_home: str, kind: PlatformKind) -> str:
    if kind is PlatformKind.WSL and is_wsl_windows_mount(codex_home):
        return _join(
            kind, codex_home, RUNTIME_DIRECTORY, kind.value, PLATFORM_MARKER
        )
    return _join(kind, codex_home, PLATFORM_MARKER)


def legacy_skill_paths(codex_home: str, kind: PlatformKind) -> tuple[str, str]:
    # Skills under a shared CODEX_HOME are shared by Windows and WSL too. Once
    # the plugin is registered, keeping the legacy skill would leave two
    # competing `podotion-image` workflows and could bypass MCP Outputs.
    legacy = _join(kind, codex_home, "skills", "podotion_image")
    return legacy, f"{legacy}.backup"


def _load_platform_marker(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SharedCodexHomeError(f"cannot validate CODEX_HOME owner marker: {path}") from exc
    if not isinstance(payload, dict):
        raise SharedCodexHomeError(f"invalid CODEX_HOME owner marker: {path}")
    return payload


def validate_codex_home_owner(
    codex_home: str,
    kind: PlatformKind,
    *,
    marker: Mapping[str, object] | None = None,
) -> None:
    """Reject a CODEX_HOME previously claimed by a different host platform."""

    payload = marker
    if payload is None:
        marker_path = Path(platform_marker_path(codex_home, kind))
        payload = _load_platform_marker(marker_path)
    if not payload:
        return
    owner = payload.get("platform")
    if owner != kind.value:
        raise SharedCodexHomeError(
            f"CODEX_HOME belongs to {owner!r}, not {kind.value!r}; "
            "configure an independent CODEX_HOME for each platform"
        )


def detect_codex_home(
    *,
    platform: str | PlatformKind | None = None,
    environ: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
    os_release: str | None = None,
    check_marker: bool = True,
) -> str:
    """Resolve and validate the current platform's independent CODEX_HOME."""

    env = os.environ if environ is None else environ
    kind = detect_platform(platform, environ=env, os_release=os_release)
    configured = _environment_value(env, "CODEX_HOME", kind)
    if configured:
        if kind is PlatformKind.WSL and is_windows_absolute(configured):
            raise SharedCodexHomeError(
                "WSL CODEX_HOME must use its mounted POSIX form, for example "
                "/mnt/c/Users/<name>/.codex"
            )
        try:
            codex_home = resolve_workspace_path(configured, platform=kind, environ=env)
        except ValueError as exc:
            raise InstallError(f"invalid CODEX_HOME: {configured!r}") from exc
    else:
        user_home = detect_user_home(
            platform=kind,
            environ=env,
            home=home,
            os_release=os_release,
        )
        codex_home = _join(kind, user_home, ".codex")

    _reject_shared_codex_home(codex_home, kind)
    if check_marker:
        validate_codex_home_owner(codex_home, kind)
    return codex_home


def personal_marketplace_path(user_home: str, kind: PlatformKind) -> str:
    return _join(kind, user_home, ".agents", "plugins", "marketplace.json")


def marketplace_entry() -> dict[str, object]:
    return {
        "name": PLUGIN_NAME,
        "source": {"source": "local", "path": MARKETPLACE_SOURCE},
        "policy": {
            "installation": "AVAILABLE",
            "authentication": "ON_INSTALL",
        },
        "category": "Productivity",
    }


def merge_marketplace(
    existing: Mapping[str, object] | None,
) -> tuple[dict[str, object], str]:
    """Add the plugin entry while preserving existing order and interface metadata."""

    if existing is None:
        payload: dict[str, object] = {
            "name": "personal",
            "interface": {"displayName": "Personal"},
            "plugins": [],
        }
    elif not isinstance(existing, Mapping):
        raise InstallError("marketplace.json must contain an object")
    else:
        payload = deepcopy(dict(existing))

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise InstallError("marketplace.json must contain a non-empty name")
    interface = payload.get("interface")
    if interface is None:
        payload["interface"] = {"displayName": name.replace("-", " ").title()}
    elif not isinstance(interface, dict):
        raise InstallError("marketplace.json interface must be an object")

    plugins = payload.get("plugins")
    if not isinstance(plugins, list):
        raise InstallError("marketplace.json plugins must be an array")
    for entry in plugins:
        if isinstance(entry, dict) and entry.get("name") == PLUGIN_NAME:
            source = entry.get("source")
            if source != {"source": "local", "path": MARKETPLACE_SOURCE}:
                raise MarketplaceConflictError(
                    f"{PLUGIN_NAME} already points to a different marketplace source"
                )
            break
    else:
        plugins.append(marketplace_entry())
    return payload, name


def build_mcp_payload(
    plugin_root: str | os.PathLike[str],
    *,
    python_executable: str | os.PathLike[str] | None = None,
    platform: str | PlatformKind | None = None,
    environ: Mapping[str, str] | None = None,
    os_release: str | None = None,
) -> dict[str, object]:
    """Build the installed MCP config using this platform's Python interpreter."""

    kind = detect_platform(platform, environ=environ, os_release=os_release)
    root = resolve_workspace_path(plugin_root, platform=kind, environ=environ)
    executable = os.fspath(python_executable or sys.executable)
    executable = resolve_workspace_path(executable, platform=kind, environ=environ)
    server_path = _join(kind, root, "mcp", "server.py")
    return {
        "mcpServers": {
            PLUGIN_NAME: {
                "command": executable,
                "args": ["-I", "-u", server_path, "--stdio"],
                "startup_timeout_sec": 30,
                "tool_timeout_sec": 3600,
            }
        }
    }


def render_mcp_json(
    plugin_root: str | os.PathLike[str],
    *,
    python_executable: str | os.PathLike[str] | None = None,
    platform: str | PlatformKind | None = None,
    environ: Mapping[str, str] | None = None,
    os_release: str | None = None,
) -> str:
    payload = build_mcp_payload(
        plugin_root,
        python_executable=python_executable,
        platform=platform,
        environ=environ,
        os_release=os_release,
    )
    return json.dumps(payload, indent=2, ensure_ascii=True) + "\n"


def _read_marketplace_for_plan(
    marketplace_path: str, kind: PlatformKind
) -> Mapping[str, object] | None:
    # Cross-platform plans can be built on another OS for testing or packaging.
    if detect_platform() is not kind:
        return None
    path = Path(marketplace_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot read marketplace: {path}") from exc
    if not isinstance(payload, dict):
        raise InstallError("marketplace.json must contain an object")
    return payload


def build_install_plan(
    plugin_source: str | os.PathLike[str],
    *,
    platform: str | PlatformKind | None = None,
    environ: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
    python_executable: str | os.PathLike[str] | None = None,
    os_release: str | None = None,
    existing_marketplace: Mapping[str, object] | None = None,
    check_marker: bool = True,
) -> InstallPlan:
    """Build a side-effect-free, platform-specific personal marketplace plan."""

    env = os.environ if environ is None else environ
    kind = detect_platform(platform, environ=env, os_release=os_release)
    user_home = detect_user_home(
        platform=kind,
        environ=env,
        home=home,
        os_release=os_release,
    )
    codex_home = detect_codex_home(
        platform=kind,
        environ=env,
        home=user_home,
        os_release=os_release,
        check_marker=check_marker,
    )
    source = resolve_workspace_path(plugin_source, platform=kind, environ=env)
    marketplace_json = personal_marketplace_path(user_home, kind)
    destination = _join(kind, user_home, "plugins", PLUGIN_NAME)
    if _path_module(kind).normcase(source) == _path_module(kind).normcase(destination):
        raise InstallError("plugin source and destination must be different")

    current_marketplace = existing_marketplace
    if current_marketplace is None:
        current_marketplace = _read_marketplace_for_plan(marketplace_json, kind)
    _, marketplace_name = merge_marketplace(current_marketplace)
    executable = resolve_workspace_path(
        os.fspath(python_executable or sys.executable), platform=kind, environ=env
    )
    mcp_json = render_mcp_json(
        destination,
        python_executable=executable,
        platform=kind,
        environ=env,
    )
    marker = platform_marker_path(codex_home, kind)
    legacy_skill, legacy_backup = legacy_skill_paths(codex_home, kind)
    operations = (
        InstallOperation("copy_plugin", destination, source),
        InstallOperation("render_mcp_json", _join(kind, destination, ".mcp.json")),
        InstallOperation("update_marketplace", marketplace_json),
        InstallOperation("claim_codex_home", marker),
        InstallOperation("register_plugin", f"{PLUGIN_NAME}@{marketplace_name}"),
        InstallOperation("retire_legacy_skill", legacy_backup, legacy_skill),
    )
    return InstallPlan(
        platform=kind,
        plugin_source=source,
        plugin_destination=destination,
        codex_home=codex_home,
        platform_marker=marker,
        legacy_skill=legacy_skill,
        legacy_backup=legacy_backup,
        marketplace_json=marketplace_json,
        marketplace_name=marketplace_name,
        mcp_json=mcp_json,
        python_executable=executable,
        codex_command=("codex", "plugin", "add", f"{PLUGIN_NAME}@{marketplace_name}"),
        codex_rollback_command=(
            "codex",
            "plugin",
            "remove",
            f"{PLUGIN_NAME}@{marketplace_name}",
        ),
        operations=operations,
    )


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _backup_path(path: Path) -> Path | None:
    if not path.exists() and not path.is_symlink():
        return None
    backup = path.with_name(f".{path.name}.backup-{uuid.uuid4().hex}")
    os.replace(path, backup)
    return backup


def _restore_path(path: Path, backup: Path | None) -> None:
    _remove_path(path)
    if backup is not None and (backup.exists() or backup.is_symlink()):
        os.replace(backup, path)


def _write_temp_file(parent: Path, name: str, content: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{name}.", dir=parent)
    temp_path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path


def _canonical_manifest(
    manifest_path: Path,
) -> tuple[dict[str, object], str, bytes]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot read plugin manifest: {manifest_path}") from exc
    if not isinstance(payload, dict):
        raise InstallError("plugin manifest must contain an object")
    version = payload.get("version")
    if not isinstance(version, str):
        raise InstallError("plugin manifest must contain a SemVer version")
    match = _SEMVER_RE.fullmatch(version)
    if match is None:
        raise InstallError(f"plugin manifest contains an invalid SemVer version: {version!r}")
    prerelease = match.group("prerelease")
    if prerelease and any(
        identifier.isdigit()
        and len(identifier) > 1
        and identifier.startswith("0")
        for identifier in prerelease.split(".")
    ):
        raise InstallError(f"plugin manifest contains an invalid SemVer version: {version!r}")
    base_version = match.group("core")
    if prerelease:
        base_version = f"{base_version}-{prerelease}"
    normalized = dict(payload)
    normalized["version"] = base_version
    canonical = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return payload, base_version, canonical


def _hash_plugin_entry(
    digest: object,
    relative_path: str,
    kind: bytes,
    content: bytes,
) -> None:
    # Length-prefix each value so different path/content boundaries cannot
    # produce the same byte stream.
    for value in (relative_path.encode("utf-8"), kind, content):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)


def _plugin_content_digest(plugin_root: Path, canonical_manifest: bytes) -> str:
    digest = hashlib.sha256()
    manifest_path = plugin_root / PLUGIN_MANIFEST
    for path in sorted(plugin_root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_dir() and not path.is_symlink():
            continue
        relative_path = path.relative_to(plugin_root).as_posix()
        if path.is_symlink():
            target = os.readlink(path).encode("utf-8")
            _hash_plugin_entry(digest, relative_path, b"symlink", target)
        elif path == manifest_path:
            _hash_plugin_entry(digest, relative_path, b"file", canonical_manifest)
        elif path.is_file():
            _hash_plugin_entry(digest, relative_path, b"file", path.read_bytes())
    return digest.hexdigest()


def apply_manifest_cachebuster(plugin_root: Path) -> str:
    """Write a stable content-derived SemVer build identifier to a staged plugin."""

    manifest_path = plugin_root / PLUGIN_MANIFEST
    payload, base_version, canonical = _canonical_manifest(manifest_path)
    content_digest = _plugin_content_digest(plugin_root, canonical)
    installed_version = f"{base_version}+codex.{content_digest[:12]}"
    payload["version"] = installed_version
    manifest_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return installed_version


def _run_codex(command: Sequence[str], runner: CommandRunner | None) -> None:
    try:
        if runner is None:
            completed = subprocess.run(command, check=True)
        else:
            completed = runner(command)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise InstallError("Codex rejected the plugin registration") from exc
    return_code = getattr(completed, "returncode", 0)
    if return_code:
        raise InstallError(
            f"Codex plugin registration exited with status {return_code}"
        )


def execute_install_plan(
    plan: InstallPlan,
    *,
    run_codex: bool = True,
    command_runner: CommandRunner | None = None,
    fault_hook: FaultHook | None = None,
) -> InstallResult:
    """Execute the file and CLI changes transactionally, restoring on failure."""

    actual_platform = detect_platform()
    if actual_platform is not plan.platform:
        raise InstallError(
            f"cannot execute a {plan.platform.value} plan on {actual_platform.value}"
        )

    source = Path(plan.plugin_source)
    destination = Path(plan.plugin_destination)
    marketplace_path = Path(plan.marketplace_json)
    marker_path = Path(plan.platform_marker)
    legacy_skill = Path(plan.legacy_skill)
    legacy_backup = Path(plan.legacy_backup)
    if not source.is_dir():
        raise InstallError(f"plugin source does not exist: {source}")
    try:
        if destination.exists() and source.samefile(destination):
            raise InstallError("plugin source and destination must be different")
    except FileNotFoundError:
        pass

    validate_codex_home_owner(plan.codex_home, plan.platform)
    if (legacy_skill.exists() or legacy_skill.is_symlink()) and (
        legacy_backup.exists() or legacy_backup.is_symlink()
    ):
        raise InstallError(
            f"legacy backup already exists; preserve or move it before installing: "
            f"{legacy_backup}"
        )
    existing_marketplace: Mapping[str, object] | None = None
    if marketplace_path.exists():
        try:
            loaded = json.loads(marketplace_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InstallError(f"cannot read marketplace: {marketplace_path}") from exc
        if not isinstance(loaded, dict):
            raise InstallError("marketplace.json must contain an object")
        existing_marketplace = loaded
    marketplace_payload, marketplace_name = merge_marketplace(existing_marketplace)
    if marketplace_name != plan.marketplace_name:
        raise InstallError("marketplace changed after the install plan was built")

    destination.parent.mkdir(parents=True, exist_ok=True)
    stage_root = Path(
        tempfile.mkdtemp(prefix=f".{PLUGIN_NAME}.stage-", dir=destination.parent)
    )
    stage_plugin = stage_root / PLUGIN_NAME
    stage_marketplace: Path | None = None
    stage_marker: Path | None = None
    destination_backup: Path | None = None
    marketplace_backup: Path | None = None
    marker_backup: Path | None = None
    installed_destination = False
    installed_marketplace = False
    installed_marker = False
    legacy_retired = False
    codex_registered = False

    try:
        shutil.copytree(
            source,
            stage_plugin,
            symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
        )
        (stage_plugin / ".mcp.json").write_text(plan.mcp_json, encoding="utf-8")
        apply_manifest_cachebuster(stage_plugin)
        stage_marketplace = _write_temp_file(
            marketplace_path.parent,
            marketplace_path.name,
            json.dumps(marketplace_payload, indent=2, ensure_ascii=True) + "\n",
        )

        marker_payload = {"schema": 1, "platform": plan.platform.value}
        if not marker_path.exists():
            stage_marker = _write_temp_file(
                marker_path.parent,
                marker_path.name,
                json.dumps(marker_payload, indent=2, ensure_ascii=True) + "\n",
            )
        if fault_hook:
            fault_hook("staged")

        destination_backup = _backup_path(destination)
        os.replace(stage_plugin, destination)
        installed_destination = True
        if fault_hook:
            fault_hook("plugin_installed")

        marketplace_backup = _backup_path(marketplace_path)
        os.replace(stage_marketplace, marketplace_path)
        stage_marketplace = None
        installed_marketplace = True
        if fault_hook:
            fault_hook("marketplace_installed")

        if stage_marker is not None:
            marker_backup = _backup_path(marker_path)
            os.replace(stage_marker, marker_path)
            stage_marker = None
            installed_marker = True
        if fault_hook:
            fault_hook("marker_installed")

        if run_codex:
            _run_codex(plan.codex_command, command_runner)
            codex_registered = True
            if fault_hook:
                fault_hook("codex_registered")

            if legacy_skill.exists() or legacy_skill.is_symlink():
                legacy_backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(legacy_skill, legacy_backup)
                legacy_retired = True
                if fault_hook:
                    fault_hook("legacy_retired")
    except BaseException as exc:
        rollback_error: BaseException | None = None
        if legacy_retired:
            try:
                if legacy_skill.exists() or legacy_skill.is_symlink():
                    raise InstallError(
                        f"cannot restore legacy skill because its path is occupied: "
                        f"{legacy_skill}"
                    )
                os.replace(legacy_backup, legacy_skill)
                legacy_retired = False
            except BaseException as restore_exc:
                rollback_error = restore_exc
        if codex_registered:
            try:
                _run_codex(plan.codex_rollback_command, command_runner)
                codex_registered = False
            except BaseException as unregister_exc:
                rollback_error = rollback_error or unregister_exc
        if installed_marker or marker_backup is not None:
            _restore_path(marker_path, marker_backup)
        if installed_marketplace or marketplace_backup is not None:
            _restore_path(marketplace_path, marketplace_backup)
        if installed_destination or destination_backup is not None:
            _restore_path(destination, destination_backup)
        if rollback_error is not None:
            raise InstallError("installation failed and rollback was incomplete") from exc
        raise
    else:
        for backup in (marker_backup, marketplace_backup, destination_backup):
            if backup is not None:
                _remove_path(backup)
    finally:
        if stage_marketplace is not None:
            stage_marketplace.unlink(missing_ok=True)
        if stage_marker is not None:
            stage_marker.unlink(missing_ok=True)
        shutil.rmtree(stage_root, ignore_errors=True)

    return InstallResult(
        plugin_destination=str(destination),
        marketplace_json=str(marketplace_path),
        platform_marker=str(marker_path),
        legacy_skill=str(legacy_skill),
        legacy_backup=str(legacy_backup),
        legacy_retired=legacy_retired,
        codex_registered=codex_registered,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plugin-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="plugin source root (defaults to the parent of scripts/)",
    )
    parser.add_argument("--codex-home", help="override CODEX_HOME for this installation")
    parser.add_argument("--home", help="override the current platform user home")
    parser.add_argument("--dry-run", action="store_true", help="print the plan only")
    parser.add_argument(
        "--no-codex",
        action="store_true",
        help="write installation files without running codex plugin add",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = dict(os.environ)
    if args.codex_home:
        env["CODEX_HOME"] = args.codex_home
    try:
        plan = build_install_plan(args.plugin_root, environ=env, home=args.home)
        if args.dry_run:
            print(json.dumps(plan.as_dict(), indent=2, ensure_ascii=False))
            return 0
        result = execute_install_plan(plan, run_codex=not args.no_codex)
    except InstallError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                },
                indent=2,
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"ok": True, **asdict(result)}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Manage a standalone Podotion Image Skill installation."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SKILL_NAME = "podotion-image"
REPOSITORY_URL = "https://github.com/scp-planet/podotion-image.git"
REPOSITORY_BRANCH = "main"
REPOSITORY_SKILL_PATH = Path("skills") / SKILL_NAME
METADATA_FILENAME = ".podotion-image-source.json"
METADATA_SCHEMA = 1
TRANSACTION_DIRECTORY = ".podotion-image-transactions"
TRASH_DIRECTORY = ".podotion-image-trash"
MAX_METADATA_BYTES = 64 * 1024
MAX_MARKETPLACE_BYTES = 1024 * 1024
LEGACY_MARKETPLACE_NAME = "personal"
LEGACY_MARKETPLACE_SOURCE = {
    "source": "local",
    "path": f"./plugins/{SKILL_NAME}",
}
REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_FILES = (
    Path("SKILL.md"),
    Path("agents/openai.yaml"),
    Path("scripts/podotion_image.py"),
    Path("scripts/configure_direct.py"),
    Path("scripts/manage.py"),
    Path("templates/provider.toml"),
)


class LifecycleError(RuntimeError):
    """A safe, user-facing lifecycle failure."""


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
Fetcher = Callable[[Path], str]
Replace = Callable[[Path, Path], object]


def _environment_value(environ: Mapping[str, str], key: str) -> str | None:
    wanted = key.casefold()
    for candidate, value in environ.items():
        if candidate.casefold() == wanted:
            return value
    return None


def resolve_codex_home(
    environ: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> Path:
    """Resolve CODEX_HOME without probing write permissions."""

    env = os.environ if environ is None else environ
    runtime_home = (home or Path.home()).resolve()
    configured = _environment_value(env, "CODEX_HOME")
    if not configured:
        return runtime_home / ".codex"
    if configured == "~":
        return runtime_home
    if configured.startswith(("~/", "~\\")):
        return (runtime_home / configured[2:]).resolve()
    if configured.startswith("~"):
        raise LifecycleError("named-user CODEX_HOME paths are not supported")
    path = Path(configured)
    if not path.is_absolute():
        raise LifecycleError("CODEX_HOME must be an absolute native path")
    return path.resolve()


def running_skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def expected_skill_root(codex_home: Path) -> Path:
    return (codex_home / "skills" / SKILL_NAME).resolve()


def credential_path(codex_home: Path) -> Path:
    return (codex_home / SKILL_NAME / "provider.toml").resolve()


def legacy_plugin_source(user_home: Path) -> Path:
    return (user_home / "plugins" / SKILL_NAME).absolute()


def legacy_marketplace_path(user_home: Path) -> Path:
    return (user_home / ".agents" / "plugins" / "marketplace.json").resolve()


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _has_required_files(root: Path) -> bool:
    return all((root / relative).is_file() for relative in REQUIRED_FILES)


def validate_installed_skill(root: Path, codex_home: Path) -> Path:
    """Allow lifecycle mutations only at CODEX_HOME/skills/podotion-image."""

    if root.is_symlink():
        raise LifecycleError("refusing to manage a symlinked Skill directory")
    resolved = root.resolve()
    expected = expected_skill_root(codex_home)
    if not _same_path(resolved, expected):
        raise LifecycleError(f"Skill is not installed at the managed location: {expected}")
    if not resolved.is_dir() or not _has_required_files(resolved):
        raise LifecycleError("managed Skill installation is incomplete")
    return resolved


def _iter_skill_files(root: Path) -> Iterable[tuple[Path, Path]]:
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise LifecycleError(f"Skill contains an unsupported symlink: {relative}")
        if path.is_dir():
            continue
        if relative.name == METADATA_FILENAME:
            continue
        if "__pycache__" in relative.parts or relative.suffix == ".pyc":
            continue
        yield relative, path


def skill_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for relative, path in _iter_skill_files(root):
        encoded = relative.as_posix().encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(len(chunk).to_bytes(8, "big"))
                digest.update(chunk)
    return digest.hexdigest()


def _frontmatter_name(skill_md: Path) -> str | None:
    content = skill_md.read_text(encoding="utf-8")
    if not content.startswith("---\n"):
        return None
    try:
        frontmatter = content.split("---", 2)[1]
    except IndexError:
        return None
    for line in frontmatter.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "name":
            return value.strip().strip('"\'')
    return None


def _validate_python_sources(root: Path) -> None:
    for relative, path in _iter_skill_files(root):
        if relative.suffix != ".py":
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
        except (OSError, SyntaxError, UnicodeError) as exc:
            raise LifecycleError(f"candidate Python source is invalid: {relative}") from exc


def _smoke_candidate(root: Path) -> None:
    scripts = root / "scripts"
    commands = (
        (sys.executable, "-I", str(scripts / "podotion_image.py"), "--help"),
        (sys.executable, "-I", str(scripts / "podotion_image.py"), "sizes"),
        (sys.executable, "-I", str(scripts / "configure_direct.py"), "--help"),
        (sys.executable, "-I", str(scripts / "manage.py"), "--help"),
    )
    for command in commands:
        try:
            result = subprocess.run(
                command,
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise LifecycleError("candidate Skill could not be started in isolation") from exc
        if result.returncode:
            raise LifecycleError("candidate Skill failed isolated startup validation")


def validate_update_source(source: Path, *, smoke: bool = True) -> Path:
    resolved = source.resolve()
    if source.is_symlink() or resolved.name != SKILL_NAME or not resolved.is_dir():
        raise LifecycleError("repository does not contain the expected Skill directory")
    if not _has_required_files(resolved):
        raise LifecycleError("repository Skill is missing required files")
    if _frontmatter_name(resolved / "SKILL.md") != SKILL_NAME:
        raise LifecycleError("repository Skill metadata has an unexpected name")
    list(_iter_skill_files(resolved))
    _validate_python_sources(resolved)
    if smoke:
        _smoke_candidate(resolved)
    return resolved


def _remove_tree(path: Path) -> None:
    def make_writable_and_retry(function, value, _exc_info):
        os.chmod(value, stat.S_IWRITE)
        function(value)

    if path.exists():
        shutil.rmtree(path, onerror=make_writable_and_retry)


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _schannel_failure(result: subprocess.CompletedProcess[str]) -> bool:
    output = f"{result.stdout}\n{result.stderr}".casefold()
    return "schannel" in output and any(
        marker in output
        for marker in ("ssl", "tls", "certificate", "handshake", "sec_e_no_credentials")
    )


def fetch_repository(
    checkout: Path,
    *,
    runner: CommandRunner = _run_command,
    native_windows: bool | None = None,
) -> str:
    """Shallow-clone the fixed repository and return its verified commit."""

    is_windows = os.name == "nt" if native_windows is None else native_windows
    clone = [
        "git",
        "clone",
        "--depth",
        "1",
        "--filter=blob:none",
        "--no-tags",
        "--single-branch",
        "--branch",
        REPOSITORY_BRANCH,
        REPOSITORY_URL,
        str(checkout),
    ]
    try:
        result = runner(clone)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LifecycleError("git could not start the Skill update") from exc
    if result.returncode and is_windows and _schannel_failure(result):
        _remove_tree(checkout)
        retry = ["git", "-c", "http.sslBackend=openssl", *clone[1:]]
        try:
            result = runner(retry)
        except (OSError, subprocess.SubprocessError) as exc:
            raise LifecycleError("git could not start the Skill update") from exc
    if result.returncode:
        raise LifecycleError("the shallow GitHub checkout failed")

    try:
        revision_result = runner(["git", "-C", str(checkout), "rev-parse", "HEAD"])
    except (OSError, subprocess.SubprocessError) as exc:
        raise LifecycleError("git could not inspect the downloaded revision") from exc
    revision = revision_result.stdout.strip().lower()
    if revision_result.returncode or not REVISION_PATTERN.fullmatch(revision):
        raise LifecycleError("the downloaded Git revision could not be verified")
    return revision


def _metadata_payload(revision: str, digest: str) -> dict[str, Any]:
    if not REVISION_PATTERN.fullmatch(revision):
        raise LifecycleError("invalid update revision")
    if not DIGEST_PATTERN.fullmatch(digest):
        raise LifecycleError("invalid Skill content digest")
    return {
        "schema": METADATA_SCHEMA,
        "repository": REPOSITORY_URL,
        "repository_path": REPOSITORY_SKILL_PATH.as_posix(),
        "branch": REPOSITORY_BRANCH,
        "revision": revision,
        "content_sha256": digest,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    content = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_metadata(root: Path) -> dict[str, Any] | None:
    path = root / METADATA_FILENAME
    try:
        if not path.is_file() or path.stat().st_size > MAX_METADATA_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    revision = payload.get("revision")
    digest = payload.get("content_sha256")
    if (
        payload.get("schema") != METADATA_SCHEMA
        or payload.get("repository") != REPOSITORY_URL
        or payload.get("repository_path") != REPOSITORY_SKILL_PATH.as_posix()
        or payload.get("branch") != REPOSITORY_BRANCH
        or not isinstance(revision, str)
        or not REVISION_PATTERN.fullmatch(revision)
        or not isinstance(digest, str)
        or not DIGEST_PATTERN.fullmatch(digest)
        or not isinstance(payload.get("updated_at"), str)
    ):
        return None
    return {
        "repository": REPOSITORY_URL,
        "branch": REPOSITORY_BRANCH,
        "revision": revision,
        "content_sha256": digest,
        "updated_at": payload["updated_at"],
    }


def build_status(root: Path, codex_home: Path) -> dict[str, Any]:
    expected = expected_skill_root(codex_home)
    root_exists = root.is_dir() and not root.is_symlink()
    managed = root_exists and _same_path(root, expected) and _has_required_files(root)
    return {
        "ok": True,
        "skill": SKILL_NAME,
        "installed": managed,
        "managed_install": managed,
        "skill_root": str(root.resolve()),
        "expected_skill_root": str(expected),
        "repository": REPOSITORY_URL,
        "branch": REPOSITORY_BRANCH,
        "source": _safe_metadata(root) if root_exists else None,
        "credential": {
            "path": str(credential_path(codex_home)),
            "configured": credential_path(codex_home).is_file(),
            "preserved_by_update": True,
            "preserved_by_uninstall": True,
        },
    }


def apply_update(
    source: Path,
    target: Path,
    codex_home: Path,
    revision: str,
    *,
    dry_run: bool = False,
    replace: Replace = os.replace,
    smoke: bool = True,
) -> dict[str, Any]:
    target = validate_installed_skill(target, codex_home)
    source = validate_update_source(source, smoke=smoke)
    if _same_path(source, target):
        raise LifecycleError("update source and installed Skill must be different directories")

    old_digest = skill_digest(target)
    new_digest = skill_digest(source)
    base_result = {
        "ok": True,
        "operation": "update",
        "skill_root": str(target),
        "repository": REPOSITORY_URL,
        "branch": REPOSITORY_BRANCH,
        "revision": revision,
        "previous_content_sha256": old_digest,
        "content_sha256": new_digest,
        "credential_preserved": True,
        "generated_images_preserved": True,
    }
    if dry_run:
        return {**base_result, "dry_run": True, "would_update": old_digest != new_digest}

    metadata = _metadata_payload(revision, new_digest)
    if old_digest == new_digest:
        _atomic_write_json(target / METADATA_FILENAME, metadata)
        return {**base_result, "updated": False}

    transaction_root = codex_home / TRANSACTION_DIRECTORY
    transaction_root.mkdir(parents=True, exist_ok=True)
    transaction = transaction_root / secrets.token_hex(16)
    stage = transaction / "stage" / SKILL_NAME
    backup = transaction / "backup" / SKILL_NAME
    stage.parent.mkdir(parents=True)
    backup.parent.mkdir(parents=True)
    shutil.copytree(source, stage)
    _atomic_write_json(stage / METADATA_FILENAME, metadata)

    old_moved = False
    new_installed = False
    try:
        replace(target, backup)
        old_moved = True
        try:
            replace(stage, target)
            new_installed = True
        except BaseException:
            replace(backup, target)
            old_moved = False
            raise
    except BaseException as exc:
        if old_moved and not new_installed and backup.exists() and not target.exists():
            try:
                replace(backup, target)
                old_moved = False
            except BaseException as rollback_exc:
                raise LifecycleError(
                    f"Skill update failed and rollback is incomplete; backup: {backup}"
                ) from rollback_exc
        if not old_moved and target.exists():
            try:
                _remove_tree(transaction)
            except OSError:
                pass
        if isinstance(exc, LifecycleError):
            raise
        raise LifecycleError("Skill update failed; the previous installation was restored") from exc

    cleanup_warning = None
    try:
        _remove_tree(transaction)
    except OSError:
        cleanup_warning = f"transaction cleanup is incomplete: {transaction}"
    result = {**base_result, "updated": True}
    if cleanup_warning:
        result["warning"] = cleanup_warning
    return result


def update_skill(
    root: Path,
    codex_home: Path,
    *,
    dry_run: bool = False,
    fetcher: Fetcher = fetch_repository,
) -> dict[str, Any]:
    validate_installed_skill(root, codex_home)
    with tempfile.TemporaryDirectory(prefix=f"{SKILL_NAME}-update-") as temp_dir:
        checkout = Path(temp_dir) / "repository"
        revision = fetcher(checkout)
        source = checkout / REPOSITORY_SKILL_PATH
        return apply_update(source, root, codex_home, revision, dry_run=dry_run)


def _legacy_marketplace_without_plugin(
    marketplace: Path,
) -> tuple[dict[str, Any] | None, bool, str]:
    if not marketplace.is_file():
        return None, False, LEGACY_MARKETPLACE_NAME
    try:
        if marketplace.stat().st_size > MAX_MARKETPLACE_BYTES:
            raise LifecycleError("legacy Marketplace file exceeds the safety limit")
        payload = json.loads(marketplace.read_text(encoding="utf-8"))
    except LifecycleError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise LifecycleError("legacy Marketplace file could not be read safely") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("plugins"), list):
        raise LifecycleError("legacy Marketplace file has an unexpected shape")
    marketplace_name = payload.get("name")
    if not isinstance(marketplace_name, str) or not marketplace_name.strip():
        raise LifecycleError("legacy Marketplace file has an invalid name")
    marketplace_name = marketplace_name.strip()

    retained: list[Any] = []
    found = False
    for entry in payload["plugins"]:
        if not isinstance(entry, dict) or entry.get("name") != SKILL_NAME:
            retained.append(entry)
            continue
        if entry.get("source") != LEGACY_MARKETPLACE_SOURCE:
            raise LifecycleError(
                "refusing to remove a same-name Plugin with a different Marketplace source"
            )
        found = True
    if not found:
        return payload, False, marketplace_name
    updated = dict(payload)
    updated["plugins"] = retained
    return updated, True, marketplace_name


def _plugin_already_absent(result: subprocess.CompletedProcess[str]) -> bool:
    output = f"{result.stdout}\n{result.stderr}".casefold()
    return any(
        marker in output
        for marker in (
            "not installed",
            "not found",
            "unknown plugin",
            "no installed plugin",
        )
    )


def uninstall_legacy_plugin(
    root: Path,
    codex_home: Path,
    *,
    confirmed: bool,
    user_home: Path | None = None,
    runner: CommandRunner = _run_command,
    remove_tree: Callable[[Path], None] = _remove_tree,
) -> dict[str, Any]:
    """Remove only the legacy personal Plugin installation."""

    validate_installed_skill(root, codex_home)
    if not confirmed:
        raise LifecycleError(
            "legacy Plugin uninstall requires --yes; credentials, images, and request state are preserved"
        )

    home = (user_home or Path.home()).resolve()
    source = legacy_plugin_source(home)
    marketplace = legacy_marketplace_path(home)
    updated_marketplace, marketplace_found, marketplace_name = (
        _legacy_marketplace_without_plugin(marketplace)
    )
    plugin_id = f"{SKILL_NAME}@{marketplace_name}"
    source_found = source.exists() or source.is_symlink()
    if source.is_symlink():
        raise LifecycleError("refusing to remove a symlinked legacy Plugin source")
    if source_found and not source.is_dir():
        raise LifecycleError("legacy Plugin source is not a directory")

    if not marketplace_found and not source_found:
        return {
            "ok": True,
            "operation": "uninstall-legacy-plugin",
            "legacy_plugin_found": False,
            "uninstalled": False,
            "credential_preserved": True,
            "generated_images_preserved": True,
            "request_state_preserved": True,
            "restart_required": False,
        }

    try:
        result = runner(["codex", "plugin", "remove", plugin_id])
    except (OSError, subprocess.SubprocessError) as exc:
        raise LifecycleError("Codex could not start the legacy Plugin removal") from exc
    if result.returncode and not _plugin_already_absent(result):
        raise LifecycleError(
            "Codex did not remove the legacy Plugin; its files were left unchanged"
        )

    warnings: list[str] = []
    marketplace_removed = False
    if marketplace_found and updated_marketplace is not None:
        try:
            _atomic_write_json(marketplace, updated_marketplace)
            marketplace_removed = True
        except OSError:
            warnings.append(
                f"legacy Marketplace cleanup is incomplete: {marketplace}"
            )

    source_removed = False
    if source_found:
        try:
            remove_tree(source)
            source_removed = not source.exists()
        except OSError:
            warnings.append(f"legacy Plugin source cleanup is incomplete: {source}")

    response: dict[str, Any] = {
        "ok": True,
        "operation": "uninstall-legacy-plugin",
        "legacy_plugin_found": True,
        "uninstalled": True,
        "plugin_id": plugin_id,
        "marketplace_entry_removed": marketplace_removed,
        "plugin_source_removed": source_removed,
        "credential_preserved": True,
        "generated_images_preserved": True,
        "request_state_preserved": True,
        "restart_required": True,
    }
    if warnings:
        response["warnings"] = warnings
    return response


def uninstall_skill(
    root: Path,
    codex_home: Path,
    *,
    confirmed: bool,
    replace: Replace = os.replace,
    remove_tree: Callable[[Path], None] = _remove_tree,
) -> dict[str, Any]:
    target = validate_installed_skill(root, codex_home)
    if not confirmed:
        raise LifecycleError(
            "uninstall requires --yes; credentials, generated images, and request state are preserved"
        )

    trash_root = codex_home / TRASH_DIRECTORY
    trash_root.mkdir(parents=True, exist_ok=True)
    tombstone = trash_root / secrets.token_hex(16)
    previous_cwd = Path.cwd()
    try:
        try:
            previous_cwd.relative_to(target)
        except ValueError:
            pass
        else:
            os.chdir(codex_home if codex_home.is_dir() else target.parent)
        replace(target, tombstone)
    except BaseException as exc:
        raise LifecycleError("Skill uninstall could not detach the installed directory") from exc
    finally:
        if previous_cwd.is_dir():
            os.chdir(previous_cwd)

    warning = None
    try:
        remove_tree(tombstone)
    except BaseException:
        warning = f"Skill is uninstalled but cleanup remains at: {tombstone}"

    result: dict[str, Any] = {
        "ok": True,
        "operation": "uninstall",
        "uninstalled": True,
        "skill_root": str(target),
        "credential": {"path": str(credential_path(codex_home)), "preserved": True},
        "generated_images_preserved": True,
        "request_state_preserved": True,
    }
    if warning:
        result["warning"] = warning
    return result


def _safe_error_message(exc: BaseException) -> str:
    message = str(exc)
    message = re.sub(r"(?i)(authorization\s*:\s*bearer\s+)\S+", r"\1<redacted>", message)
    message = re.sub(
        r"(?i)(PodotionImage(?:4k)?Sk\s*=\s*)[\"'][^\"']+[\"']",
        r'\1"<redacted>"',
        message,
    )
    return message[:4096]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="show the standalone Skill status")
    update_parser = subparsers.add_parser("update", help="update from the fixed GitHub repository")
    update_parser.add_argument("--dry-run", action="store_true", help="download and validate only")
    uninstall_parser = subparsers.add_parser("uninstall", help="remove only the installed Skill code")
    uninstall_parser.add_argument("--yes", action="store_true", help="confirm Skill removal")
    legacy_parser = subparsers.add_parser(
        "uninstall-legacy-plugin",
        help="remove the previous personal Plugin installation",
    )
    legacy_parser.add_argument(
        "--yes", action="store_true", help="confirm legacy Plugin removal"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = running_skill_root()
    try:
        codex_home = resolve_codex_home()
        if args.command == "status":
            result = build_status(root, codex_home)
        elif args.command == "update":
            result = update_skill(root, codex_home, dry_run=args.dry_run)
        elif args.command == "uninstall":
            result = uninstall_skill(root, codex_home, confirmed=args.yes)
        else:
            result = uninstall_legacy_plugin(
                root,
                codex_home,
                confirmed=args.yes,
            )
    except Exception as exc:
        result = {
            "ok": False,
            "error": {"type": type(exc).__name__, "message": _safe_error_message(exc)},
        }
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

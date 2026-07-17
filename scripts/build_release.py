#!/usr/bin/env python3
"""Build a deterministic Podotion Image plugin ZIP from the repository root."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import zipfile
from pathlib import Path
from typing import Iterable, Sequence


ARCHIVE_ROOT = "podotion-image"
DEFAULT_ARCHIVE = "podotion-image-plugin.zip"
REQUIRED_PATHS = (
    Path(".codex-plugin/plugin.json"),
    Path(".mcp.json"),
    Path("README.md"),
    Path("mcp/server.py"),
    Path("scripts/install.py"),
    Path("skills/podotion-image/SKILL.md"),
    Path("skills/podotion-image/scripts/podotion_image.py"),
)
EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "PodotionImage",
        "__pycache__",
        "dist",
        "podotion-image-workspace",
        "venv",
    }
)
EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo"})
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def validate_source_root(source_root: Path) -> Path:
    root = source_root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"source root is not a directory: {root}")
    missing = [str(path) for path in REQUIRED_PATHS if not (root / path).is_file()]
    if missing:
        raise ValueError(f"source root is missing required files: {', '.join(missing)}")
    return root


def _is_excluded(relative: Path) -> bool:
    return any(part in EXCLUDED_DIRECTORIES for part in relative.parts) or (
        relative.suffix.lower() in EXCLUDED_SUFFIXES
    )


def iter_release_files(source_root: Path, output_path: Path) -> Iterable[Path]:
    output = output_path.resolve()
    for path in sorted(source_root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(source_root)
        if _is_excluded(relative) or path.is_dir():
            continue
        if path.is_symlink():
            raise ValueError(f"release source must not contain symlinks: {relative}")
        if not path.is_file() or path.resolve() == output:
            continue
        yield path


def _zip_info(path: Path, archive_name: str) -> zipfile.ZipInfo:
    mode = path.stat().st_mode
    permission = 0o755 if mode & 0o111 else 0o644
    info = zipfile.ZipInfo(archive_name, FIXED_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (permission & 0xFFFF) << 16
    return info


def build_release(source_root: Path, output_path: Path) -> dict[str, object]:
    root = validate_source_root(source_root)
    destination = output_path.expanduser().resolve()
    files = list(iter_release_files(root, destination))
    if not files:
        raise ValueError("release contains no files")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{secrets.token_hex(8)}.tmp"
    )
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for path in files:
                relative = path.relative_to(root).as_posix()
                archive.writestr(
                    _zip_info(path, f"{ARCHIVE_ROOT}/{relative}"),
                    path.read_bytes(),
                )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    return {
        "ok": True,
        "archive": str(destination),
        "archive_root": ARCHIVE_ROOT,
        "file_count": len(files),
        "bytes": destination.stat().st_size,
    }


def build_parser() -> argparse.ArgumentParser:
    repository_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=repository_root,
        help="plugin repository root; defaults to the parent of scripts/",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repository_root / "dist" / DEFAULT_ARCHIVE,
        help="output ZIP path; defaults to dist/podotion-image-plugin.zip",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = build_release(args.source_root, args.output)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(
            json.dumps(
                {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

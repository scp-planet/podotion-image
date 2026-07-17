#!/usr/bin/env python3
"""Install the Podotion image credential without exposing it in argv or output."""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
import secrets
import sys
from pathlib import Path
from types import ModuleType


MAX_SECRET_BYTES = 64 * 1024
TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "templates" / "provider.toml"


def _load_runtime() -> ModuleType:
    path = Path(__file__).with_name("podotion_image.py")
    spec = importlib.util.spec_from_file_location("podotion_image_config_runtime", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Podotion Image executor could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runtime = _load_runtime()


def validate_secret(value: str) -> str:
    secret = value.strip()
    if not secret:
        raise ValueError("PodotionImageSk cannot be empty")
    if secret.startswith("{{") and secret.endswith("}}"):
        raise ValueError("PodotionImageSk must replace the entire placeholder without braces")
    if secret == runtime.DIRECT_SECRET_PLACEHOLDER or "PODOTION_IMAGE_SK" in secret.upper():
        raise ValueError("PodotionImageSk placeholder has not been replaced")
    if "\r" in secret or "\n" in secret:
        raise ValueError("PodotionImageSk must be a single line")
    if len(secret.encode("utf-8")) > MAX_SECRET_BYTES:
        raise ValueError("PodotionImageSk exceeds the 64 KB safety limit")
    return secret


def render_config(secret: str, template_path: Path = TEMPLATE_PATH) -> str:
    template = template_path.read_text(encoding="utf-8")
    placeholder = json.dumps(runtime.DIRECT_SECRET_PLACEHOLDER)
    if template.count(placeholder) != 1:
        raise RuntimeError("provider template must contain exactly one quoted secret placeholder")
    rendered = template.replace(placeholder, json.dumps(validate_secret(secret), ensure_ascii=False))
    return rendered if rendered.endswith("\n") else rendered + "\n"


def write_private_config(destination: Path, content: str, *, force: bool = False) -> Path:
    target = destination.expanduser().resolve()
    if target.exists() and not force:
        raise FileExistsError(f"credential file already exists: {target}; use --force to replace it")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(target.parent, 0o700)
    except OSError:
        pass

    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write PodotionImageSk to the private Podotion image runtime config."
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="read the secret from stdin instead of a hidden interactive prompt",
    )
    parser.add_argument("--force", action="store_true", help="replace an existing credential file")
    parser.add_argument(
        "--credential-file",
        help="target path; defaults to $CODEX_HOME/podotion-image/provider.toml",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.stdin:
            secret = sys.stdin.read(MAX_SECRET_BYTES + 1)
        else:
            secret = getpass.getpass("PodotionImageSk: ")
        content = render_config(secret)
        destination = (
            Path(args.credential_file)
            if args.credential_file
            else runtime.direct_provider_config_path()
        )
        target = write_private_config(destination, content, force=args.force)
        print(
            json.dumps(
                {
                    "ok": True,
                    "config_path": str(target),
                    "base_url": runtime.DIRECT_BASE_URL,
                    "credential_mode": "podotion_image_sk",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "type": type(exc).__name__,
                        "message": runtime.redact_secrets(str(exc)),
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

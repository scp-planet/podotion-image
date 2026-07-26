#!/usr/bin/env python3
"""Configure Podotion image credentials without exposing them in argv or output."""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
import secrets
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable


MAX_SECRET_BYTES = 64 * 1024
MAX_STDIN_BYTES = (2 * MAX_SECRET_BYTES) + 4
MAX_CONFIG_BYTES = (2 * MAX_SECRET_BYTES) + (4 * 1024)
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

DEFAULT_SECRET_KEY = getattr(runtime, "DIRECT_SECRET_KEY", "PodotionImageSk")
FOUR_K_SECRET_KEY = getattr(runtime, "DIRECT_4K_SECRET_KEY", "PodotionImage4kSk")
DEFAULT_SECRET_PLACEHOLDER = getattr(
    runtime, "DIRECT_SECRET_PLACEHOLDER", "__PODOTION_IMAGE_SK__"
)
FOUR_K_SECRET_PLACEHOLDER = getattr(
    runtime, "DIRECT_4K_SECRET_PLACEHOLDER", "__PODOTION_IMAGE_4K_SK__"
)
CANONICAL_KEYS = frozenset({"base_url", DEFAULT_SECRET_KEY, FOUR_K_SECRET_KEY})


def validate_secret(value: Any, *, key: str = DEFAULT_SECRET_KEY) -> str:
    if type(value) is not str:
        raise ValueError(f"{key} must be a TOML string")
    secret = value.strip()
    if not secret:
        raise ValueError(f"{key} cannot be empty")
    if "\r" in secret or "\n" in secret:
        raise ValueError(f"{key} must be a single line")
    if len(secret.encode("utf-8")) > MAX_SECRET_BYTES:
        raise ValueError(f"{key} exceeds the 64 KB safety limit")

    upper_secret = secret.upper()
    placeholders = {
        DEFAULT_SECRET_PLACEHOLDER.upper(),
        FOUR_K_SECRET_PLACEHOLDER.upper(),
    }
    if (
        (secret.startswith("{{") and secret.endswith("}}"))
        or (secret.startswith("__") and secret.endswith("__"))
        or upper_secret in placeholders
        or "PODOTIONIMAGESK" in upper_secret
        or "PODOTIONIMAGE4KSK" in upper_secret
    ):
        raise ValueError(f"{key} placeholder has not been replaced")
    return secret


def _read_template(template_path: Path) -> str:
    try:
        raw = template_path.read_bytes()
    except OSError as exc:
        raise RuntimeError("provider template could not be read") from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise RuntimeError("provider template exceeds the safety limit")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("provider template must be UTF-8") from exc


def _validate_template(template: str) -> None:
    try:
        parsed = tomllib.loads(template)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError("provider template is not valid TOML") from exc
    if set(parsed) != CANONICAL_KEYS:
        raise RuntimeError("provider template must contain only the canonical keys")
    if type(parsed.get("base_url")) is not str or parsed["base_url"] != runtime.DIRECT_BASE_URL:
        raise RuntimeError("provider template contains an invalid base_url")
    if parsed.get(DEFAULT_SECRET_KEY) != DEFAULT_SECRET_PLACEHOLDER:
        raise RuntimeError("provider template contains an invalid default credential placeholder")
    if parsed.get(FOUR_K_SECRET_KEY) != FOUR_K_SECRET_PLACEHOLDER:
        raise RuntimeError("provider template contains an invalid 4K credential placeholder")


def render_config(
    default_secret: str,
    four_k_secret: str | None = None,
    template_path: Path = TEMPLATE_PATH,
) -> str:
    default_value = validate_secret(default_secret, key=DEFAULT_SECRET_KEY)
    if four_k_secret is None:
        four_k_value = None
    elif type(four_k_secret) is not str:
        four_k_value = validate_secret(four_k_secret, key=FOUR_K_SECRET_KEY)
    elif four_k_secret.strip():
        four_k_value = validate_secret(four_k_secret, key=FOUR_K_SECRET_KEY)
    else:
        four_k_value = None

    template = _read_template(template_path)
    _validate_template(template)
    default_placeholder = json.dumps(DEFAULT_SECRET_PLACEHOLDER)
    four_k_placeholder = json.dumps(FOUR_K_SECRET_PLACEHOLDER)
    if template.count(default_placeholder) != 1 or template.count(four_k_placeholder) != 1:
        raise RuntimeError("provider template placeholders must each occur exactly once")

    rendered = template.replace(
        default_placeholder, json.dumps(default_value, ensure_ascii=False)
    )
    if four_k_value is None:
        rendered = "\n".join(
            line for line in rendered.splitlines() if four_k_placeholder not in line
        )
    else:
        rendered = rendered.replace(
            four_k_placeholder, json.dumps(four_k_value, ensure_ascii=False)
        )
    return rendered if rendered.endswith("\n") else rendered + "\n"


def read_existing_config(path: Path) -> tuple[str, str | None]:
    target = path.expanduser().resolve()
    if not target.is_file():
        raise FileNotFoundError("existing canonical credential file was not found")
    try:
        with target.open("rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError as exc:
        raise RuntimeError("existing credential file could not be read") from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise RuntimeError("existing credential file exceeds the safety limit")
    try:
        config = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError("existing credential file is not valid UTF-8 TOML") from exc

    allowed_shapes = (
        {"base_url", DEFAULT_SECRET_KEY},
        {"base_url", DEFAULT_SECRET_KEY, FOUR_K_SECRET_KEY},
    )
    if type(config) is not dict or set(config) not in allowed_shapes:
        raise RuntimeError("existing credential file does not have the canonical shape")
    if type(config["base_url"]) is not str or config["base_url"] != runtime.DIRECT_BASE_URL:
        raise RuntimeError("existing credential file contains an invalid base_url")

    default_secret = validate_secret(config[DEFAULT_SECRET_KEY], key=DEFAULT_SECRET_KEY)
    four_k_secret = (
        validate_secret(config[FOUR_K_SECRET_KEY], key=FOUR_K_SECRET_KEY)
        if FOUR_K_SECRET_KEY in config
        else None
    )
    return default_secret, four_k_secret


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
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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


def _decode_input(data: bytes | str, max_bytes: int) -> str:
    if isinstance(data, str):
        try:
            encoded = data.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("credential input must be UTF-8") from exc
    else:
        encoded = data
    if len(encoded) > max_bytes:
        raise ValueError("credential input exceeds the safety limit")
    try:
        return encoded.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("credential input must be UTF-8") from exc


def _read_stdin(max_bytes: int) -> str:
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    data = stream.read(max_bytes + 1)
    return _decode_input(data, max_bytes).removeprefix("\ufeff")


def _read_input_file(path: Path, max_bytes: int = MAX_STDIN_BYTES) -> str:
    source = path.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError("credential input file was not found")
    try:
        with source.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise RuntimeError("credential input file could not be read") from exc
    return _decode_input(data, max_bytes)


def _parse_assignments(text: str, *, set_four_k: bool = False) -> tuple[str | None, str | None]:
    assignments: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        if "=" not in line:
            raise ValueError("credential assignments must use the exact Key=value format")
        key, value = line.split("=", 1)
        if key not in {DEFAULT_SECRET_KEY, FOUR_K_SECRET_KEY}:
            raise ValueError("credential input contains an unknown key")
        if key in assignments:
            raise ValueError("credential input contains a duplicate key")
        assignments[key] = validate_secret(value, key=key)

    if set_four_k:
        if set(assignments) != {FOUR_K_SECRET_KEY}:
            raise ValueError(f"--set-4k input must contain exactly {FOUR_K_SECRET_KEY}")
        return None, assignments[FOUR_K_SECRET_KEY]
    if DEFAULT_SECRET_KEY not in assignments:
        raise ValueError(f"credential input must contain {DEFAULT_SECRET_KEY}")
    return assignments[DEFAULT_SECRET_KEY], assignments.get(FOUR_K_SECRET_KEY)


def _looks_like_assignments(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    for line in lines:
        if "=" not in line:
            continue
        key = line.split("=", 1)[0]
        if key in {DEFAULT_SECRET_KEY, FOUR_K_SECRET_KEY} or key.isidentifier():
            return True
    return False


def _parse_stdin_credentials(text: str, *, set_four_k: bool = False) -> tuple[str | None, str | None]:
    if _looks_like_assignments(text):
        return _parse_assignments(text, set_four_k=set_four_k)

    lines = text.splitlines()
    if set_four_k:
        if len(lines) != 1:
            raise ValueError("stdin must contain exactly one 4K credential")
        return None, validate_secret(lines[0], key=FOUR_K_SECRET_KEY)
    if not lines or len(lines) > 2:
        raise ValueError("stdin must contain a default credential and an optional 4K credential")
    default_secret = validate_secret(lines[0], key=DEFAULT_SECRET_KEY)
    four_k_secret = (
        validate_secret(lines[1], key=FOUR_K_SECRET_KEY)
        if len(lines) == 2 and lines[1].strip()
        else None
    )
    return default_secret, four_k_secret


def _read_credentials_from_stdin() -> tuple[str, str | None]:
    default_secret, four_k_secret = _parse_stdin_credentials(_read_stdin(MAX_STDIN_BYTES))
    if default_secret is None:
        raise RuntimeError("default credential parsing failed")
    return default_secret, four_k_secret


def _read_four_k_from_stdin() -> str:
    _, four_k_secret = _parse_stdin_credentials(
        _read_stdin(MAX_STDIN_BYTES),
        set_four_k=True,
    )
    if four_k_secret is None:
        raise RuntimeError("4K credential parsing failed")
    return four_k_secret


def _read_credentials_from_file(path: Path, *, set_four_k: bool = False) -> tuple[str | None, str | None]:
    return _parse_assignments(_read_input_file(path), set_four_k=set_four_k)


def _paths_refer_to_same_file(first: Path, second: Path) -> bool:
    first_resolved = first.expanduser().resolve()
    second_resolved = second.expanduser().resolve()
    if first_resolved == second_resolved:
        return True
    if not first_resolved.exists() or not second_resolved.exists():
        return False
    try:
        return first_resolved.samefile(second_resolved)
    except OSError:
        return False


def _safe_error_message(exc: Exception, secret_values: Iterable[str]) -> str:
    try:
        return runtime.redact_secrets(str(exc), tuple(secret_values))
    except Exception:
        return "credential configuration failed"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write Podotion image credentials to the private runtime config."
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--stdin", action="store_true", help="read credentials from stdin")
    source.add_argument(
        "--input-file",
        help="read exact PodotionImageSk=... assignments from a local UTF-8 file",
    )
    parser.add_argument("--force", action="store_true", help="replace an existing credential file")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the existing credential file locally without changing it",
    )
    parser.add_argument(
        "--set-4k",
        action="store_true",
        help="preserve the existing default credential and set only the 4K credential",
    )
    parser.add_argument(
        "--credential-file",
        help="target path; defaults to $CODEX_HOME/podotion-image/provider.toml",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    secret_values: list[str] = []
    try:
        destination = (
            Path(args.credential_file)
            if args.credential_file
            else runtime.direct_provider_config_path()
        )
        if args.check:
            if args.stdin or args.input_file or args.force or args.set_4k:
                raise ValueError("--check cannot be combined with an input source, --force, or --set-4k")
            _, four_k_secret = read_existing_config(destination)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "operation": "check",
                        "config_path": str(destination.expanduser().resolve()),
                        "credential_profiles": {
                            "default": True,
                            "4k": four_k_secret is not None,
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        input_path = Path(args.input_file) if args.input_file else None
        if input_path is not None and _paths_refer_to_same_file(input_path, destination):
            raise ValueError("credential input file must be different from the destination")

        if args.set_4k:
            if not (args.stdin or input_path is not None) or not args.force:
                raise ValueError("--set-4k requires an input source and --force")
            if args.stdin:
                four_k_secret = _read_four_k_from_stdin()
            else:
                if input_path is None:
                    raise RuntimeError("credential input source is unavailable")
                _, four_k_secret = _read_credentials_from_file(
                    input_path, set_four_k=True
                )
                if four_k_secret is None:
                    raise RuntimeError("4K credential parsing failed")
            secret_values.append(four_k_secret)
            default_secret, _ = read_existing_config(destination)
            secret_values.append(default_secret)
            content = render_config(default_secret, four_k_secret)
        else:
            if args.stdin:
                default_secret, four_k_secret = _read_credentials_from_stdin()
            elif input_path is not None:
                default_secret, four_k_secret = _read_credentials_from_file(input_path)
                if default_secret is None:
                    raise RuntimeError("default credential parsing failed")
            else:
                default_secret = validate_secret(
                    getpass.getpass(f"{DEFAULT_SECRET_KEY}: "), key=DEFAULT_SECRET_KEY
                )
                entered_four_k = getpass.getpass(f"{FOUR_K_SECRET_KEY} (optional): ")
                four_k_secret = (
                    validate_secret(entered_four_k, key=FOUR_K_SECRET_KEY)
                    if entered_four_k.strip()
                    else None
                )
            secret_values.append(default_secret)
            if four_k_secret:
                secret_values.append(four_k_secret)
            content = render_config(default_secret, four_k_secret)

        target = write_private_config(destination, content, force=args.force)
        print(
            json.dumps(
                {
                    "ok": True,
                    "config_path": str(target),
                    "base_url": runtime.DIRECT_BASE_URL,
                    "credential_profiles": {
                        "default": True,
                        "4k": four_k_secret is not None,
                    },
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
                        "message": _safe_error_message(exc, secret_values),
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

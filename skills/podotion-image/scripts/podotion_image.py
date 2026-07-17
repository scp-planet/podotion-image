#!/usr/bin/env python3
"""Generate and edit images through Podotion's direct image endpoint."""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import errno
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import struct
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any


PLUGIN_DIRECTORY = Path(__file__).resolve().parents[3]
try:
    sys.path.remove(str(PLUGIN_DIRECTORY))
except ValueError:
    pass
sys.path.insert(0, str(PLUGIN_DIRECTORY))

from podotion_image.paths import codex_home_path


SITE_NAME = "Podotion"
IMAGE_MODEL = "gpt-image-2"
DIRECT_BASE_URL = "https://ai.podotion.com/v1"
DIRECT_PROVIDER_ID = "podotion-direct"
DIRECT_CONFIG_FILENAME = "provider.toml"
DIRECT_CONFIG_DIRECTORY = "podotion-image"
DIRECT_SECRET_KEY = "PodotionImageSk"
DIRECT_SECRET_PLACEHOLDER = "__PODOTION_IMAGE_SK__"
IMAGES_GENERATIONS_ENDPOINT = "images/generations"
IMAGES_EDITS_ENDPOINT = "images/edits"
MODELS_ENDPOINT = "models"
QUALITY = "auto"
AUTO_SIZE = "auto"
DEFAULT_TIER = "1k"
TIMEOUT_SECONDS = 600
PROBE_TIMEOUT_SECONDS = 15
REQUEST_REUSE_SECONDS = 10 * 60
REQUEST_RECORD_VERSION = 1
MAX_JSON_RESPONSE_BYTES = 75 * 1024 * 1024
MAX_ERROR_RESPONSE_BYTES = 1024 * 1024
MAX_IMAGE_DOWNLOAD_BYTES = 50 * 1024 * 1024
MAX_INPUT_FILE_BYTES = 20 * 1024 * 1024
MAX_PROMPT_BYTES = 1024 * 1024
MAX_INPUT_IMAGES = 5
REQUEST_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{7,127}")

SUPPORTED_TIERS = {
    "1k": 1024,
    "2k": 2048,
    "4k": 3840,
}
SUPPORTED_RATIOS = {
    "1:1": (1, 1),
    "2:3": (2, 3),
    "3:2": (3, 2),
    "3:4": (3, 4),
    "4:3": (4, 3),
    "16:9": (16, 9),
    "9:16": (9, 16),
}

MAX_EDGE = 3840
MIN_PIXELS = 655_360
MAX_PIXELS = 8_294_400
MULTIPLE = 16

@dataclass(frozen=True)
class ProviderConfig:
    provider_id: str
    name: str
    base_url: str
    bearer_token: str = field(repr=False)
    credential_mode: str
    config_path: Path | None = None

    @property
    def secret_values(self) -> tuple[str, ...]:
        return (self.bearer_token,) if self.bearer_token else ()


@dataclass(frozen=True)
class ImageResult:
    value: str = field(repr=False)
    kind: str
    source: str
    mime_type: str | None = None
    output_format: str | None = None


@dataclass(frozen=True)
class SavedImage:
    path: Path
    mime_type: str
    bytes: int
    width: int | None = None
    height: int | None = None

    def as_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "path": str(self.path),
            "markdown_path": self.path.as_posix(),
            "mime_type": self.mime_type,
            "bytes": self.bytes,
            "width": self.width,
            "height": self.height,
        }
        return data


class ProviderRequestError(RuntimeError):
    """A provider failure with safe, machine-readable diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        error_kind: str,
        http_status: int | None = None,
        request_id: str | None = None,
        cf_ray: str | None = None,
        elapsed_ms: int | None = None,
        attempts: int = 1,
        retry_after: float | None = None,
        first_http_status: int | None = None,
        first_error_message: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_kind = error_kind
        self.http_status = http_status
        self.request_id = request_id
        self.cf_ray = cf_ray
        self.elapsed_ms = elapsed_ms
        self.attempts = attempts
        self.retry_after = retry_after
        self.first_http_status = first_http_status
        self.first_error_message = first_error_message
        self.details = dict(details or {})

    def as_json(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "error_kind": self.error_kind,
            "message": str(self),
            "http_status": self.http_status,
            "request_id": self.request_id,
            "cf_ray": self.cf_ray,
            "elapsed_ms": self.elapsed_ms,
            "attempts": self.attempts,
            "retry_after": self.retry_after,
            "first_http_status": self.first_http_status,
            "first_error_message": self.first_error_message,
            "details": self.details,
        }


class ImageCandidateError(RuntimeError):
    """All explicitly declared image candidates failed safe decoding."""

    def __init__(self, candidate_count: int, warnings: Sequence[Mapping[str, Any]]) -> None:
        super().__init__(f"all {candidate_count} image candidates were invalid")
        self.candidate_count = candidate_count
        self.warnings = [dict(warning) for warning in warnings]

    @property
    def details(self) -> dict[str, Any]:
        return {
            "candidate_count": self.candidate_count,
            "invalid_candidates": self.warnings,
        }


def normalize_ratio(value: str) -> str:
    ratio = value.strip().replace("：", ":")
    if ratio not in SUPPORTED_RATIOS:
        choices = ", ".join(SUPPORTED_RATIOS)
        raise argparse.ArgumentTypeError(f"unsupported ratio {value!r}; choose one of: {choices}")
    return ratio


def validate_dimensions(width: int, height: int) -> None:
    pixels = width * height
    long_edge = max(width, height)
    short_edge = min(width, height)
    if long_edge > MAX_EDGE:
        raise ValueError(f"long edge {long_edge}px exceeds {MAX_EDGE}px")
    if width % MULTIPLE or height % MULTIPLE:
        raise ValueError(f"dimensions must be multiples of {MULTIPLE}px")
    if long_edge / short_edge > 3:
        raise ValueError("long edge to short edge ratio exceeds 3:1")
    if pixels < MIN_PIXELS or pixels > MAX_PIXELS:
        raise ValueError(f"pixel count {pixels} is outside {MIN_PIXELS}-{MAX_PIXELS}")


def resolve_size(tier: str, ratio: str) -> str:
    if tier not in SUPPORTED_TIERS:
        choices = ", ".join(SUPPORTED_TIERS)
        raise ValueError(f"unsupported size tier {tier!r}; choose one of: {choices}")
    if ratio not in SUPPORTED_RATIOS:
        choices = ", ".join(SUPPORTED_RATIOS)
        raise ValueError(f"unsupported ratio {ratio!r}; choose one of: {choices}")

    width_ratio, height_ratio = SUPPORTED_RATIOS[ratio]
    target_long = SUPPORTED_TIERS[tier]
    ratio_long = max(width_ratio, height_ratio)
    ratio_short = min(width_ratio, height_ratio)
    if ratio_long / ratio_short > 3:
        raise ValueError(f"ratio {ratio} exceeds the 3:1 long-edge limit")

    unit = math.lcm(
        MULTIPLE // math.gcd(width_ratio, MULTIPLE),
        MULTIPLE // math.gcd(height_ratio, MULTIPLE),
    )
    max_by_edge = MAX_EDGE // (ratio_long * unit)
    max_by_tier = target_long // (ratio_long * unit)
    max_by_pixels = int(
        math.floor(math.sqrt(MAX_PIXELS / (width_ratio * height_ratio * unit * unit)))
    )
    scale = min(max_by_edge, max_by_tier, max_by_pixels)
    if scale < 1:
        raise ValueError(f"cannot map {tier} {ratio} into a valid image size")

    width = width_ratio * unit * scale
    height = height_ratio * unit * scale
    if width * height < MIN_PIXELS:
        scale = int(
            math.ceil(math.sqrt(MIN_PIXELS / (width_ratio * height_ratio * unit * unit)))
        )
        width = width_ratio * unit * scale
        height = height_ratio * unit * scale

    validate_dimensions(width, height)
    return f"{width}x{height}"


def resolve_request_size(tier: str | None, ratio: str | None) -> str:
    if tier and not ratio:
        raise ValueError("--ratio is required when --size is set")
    if not tier and not ratio:
        return AUTO_SIZE
    return resolve_size(tier or DEFAULT_TIER, ratio or "1:1")


def _is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.lower().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("provider base_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise RuntimeError("provider base_url must not contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise RuntimeError("provider base_url must not contain a query or fragment")
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise RuntimeError("plain HTTP provider base_url is allowed only for a loopback host")
    return base_url


def direct_provider_config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return _default_codex_home(env) / DIRECT_CONFIG_DIRECTORY / DIRECT_CONFIG_FILENAME


def load_direct_provider(
    config_path: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ProviderConfig:
    path = (
        Path(config_path).expanduser().resolve()
        if config_path
        else direct_provider_config_path(environ).resolve()
    )
    if not path.is_file():
        raise FileNotFoundError(
            f"Podotion image credential file not found: {path}; "
            "run scripts/configure_direct.py first"
        )
    if path.stat().st_size > MAX_ERROR_RESPONSE_BYTES:
        raise RuntimeError("Podotion image credential file exceeds the 1 MB safety limit")
    try:
        with path.open("rb") as handle:
            config = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        # Do not include parser context because a malformed line may contain the secret.
        raise RuntimeError(f"failed to parse Podotion image credential file: {path}") from exc
    if not isinstance(config, dict):
        raise RuntimeError("Podotion image credential file must contain a TOML table")

    base_url = validate_base_url(str(config.get("base_url") or ""))
    if base_url != DIRECT_BASE_URL:
        raise RuntimeError(
            f"direct provider base_url must be exactly {DIRECT_BASE_URL}; "
            "the direct endpoint cannot be overridden"
        )
    bearer_token = str(config.get(DIRECT_SECRET_KEY) or "").strip()
    if not bearer_token:
        raise RuntimeError(f"Podotion image credential file does not contain {DIRECT_SECRET_KEY}")
    if bearer_token == DIRECT_SECRET_PLACEHOLDER or "PODOTION_IMAGE_SK" in bearer_token.upper():
        raise RuntimeError("Podotion image credential placeholder has not been replaced")

    return ProviderConfig(
        provider_id=DIRECT_PROVIDER_ID,
        name=SITE_NAME,
        base_url=DIRECT_BASE_URL,
        bearer_token=bearer_token,
        credential_mode="podotion_image_sk",
        config_path=path,
    )


def _build_provider_url(base_url: str, endpoint: str) -> str:
    base = validate_base_url(base_url)
    parsed = urllib.parse.urlsplit(base)
    normalized_path = parsed.path.rstrip("/")
    endpoint_path = "/" + endpoint.strip("/")
    if normalized_path.endswith(endpoint_path):
        path = normalized_path
    else:
        path = normalized_path + endpoint_path
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def build_images_url(base_url: str, operation: str) -> str:
    if operation == "generate":
        endpoint = IMAGES_GENERATIONS_ENDPOINT
    elif operation == "edit":
        endpoint = IMAGES_EDITS_ENDPOINT
    else:
        raise ValueError("operation must be 'generate' or 'edit'")
    return _build_provider_url(base_url, endpoint)


def build_models_url(base_url: str) -> str:
    return _build_provider_url(base_url, MODELS_ENDPOINT)


def _detect_image_type(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise RuntimeError("image data had an unsupported or invalid file signature")


def validate_input_file(path: Path, label: str = "image") -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} file not found: {path}")
    size = path.stat().st_size
    if size <= 0:
        raise ValueError(f"{label} file is empty: {path}")
    if size > MAX_INPUT_FILE_BYTES:
        raise ValueError(f"{label} file exceeds the 20 MB safety limit: {path}")


def build_images_generation_payload(prompt: str, size: str = AUTO_SIZE) -> dict[str, Any]:
    normalized_prompt = prompt.strip()
    if not normalized_prompt:
        raise ValueError("prompt cannot be empty")
    if len(normalized_prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError("prompt exceeds the 1 MB safety limit")
    return {
        "model": IMAGE_MODEL,
        "prompt": normalized_prompt,
        "size": size,
        "quality": QUALITY,
        "output_format": "png",
        "n": 1,
    }


def build_images_edit_multipart(
    prompt: str,
    image_paths: Sequence[Path],
    size: str = AUTO_SIZE,
) -> tuple[bytes, str]:
    normalized_prompt = prompt.strip()
    if not normalized_prompt:
        raise ValueError("prompt cannot be empty")
    if len(normalized_prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError("prompt exceeds the 1 MB safety limit")

    paths = [Path(path).resolve() for path in image_paths]
    if not paths:
        raise ValueError("edit operation requires at least one input image")
    if len(paths) > MAX_INPUT_IMAGES:
        raise ValueError(f"at most {MAX_INPUT_IMAGES} input images are supported")

    boundary = f"----podotion-image-{secrets.token_hex(16)}"
    body = bytearray()

    def add_field(name: str, value: str) -> None:
        body.extend(f"--{boundary}\r\n".encode("ascii"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"))
        body.extend(value.encode("utf-8"))
        body.extend(b"\r\n")

    for name, value in (
        ("model", IMAGE_MODEL),
        ("prompt", normalized_prompt),
        ("size", size),
        ("quality", QUALITY),
        ("output_format", "png"),
    ):
        add_field(name, value)

    for index, path in enumerate(paths, start=1):
        validate_input_file(path)
        data = path.read_bytes()
        mime_type, suffix = _detect_image_type(data)
        filename = f"image-{index}{suffix}"
        body.extend(f"--{boundary}\r\n".encode("ascii"))
        body.extend(
            (
                'Content-Disposition: form-data; name="image[]"; '
                f'filename="{filename}"\r\n'
            ).encode("ascii")
        )
        body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode("ascii"))
        body.extend(data)
        body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode("ascii"))
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def redact_secrets(text: str, secrets: Iterable[str] = ()) -> str:
    redacted = str(text)
    for secret in secrets:
        if secret:
            redacted = redacted.replace(str(secret), "<redacted>")
    redacted = re.sub(
        r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;\"']+",
        r"\1<redacted>",
        redacted,
    )
    redacted = re.sub(
        r"(?i)(PodotionImageSk\s*=\s*)[\"'][^\"']+[\"']",
        r'\1"<redacted>"',
        redacted,
    )
    return redacted


def _request_headers(
    provider: ProviderConfig,
    *,
    content_type: str | None = None,
) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "podotion-image-skill/2.0",
    }
    if provider.bearer_token:
        headers["Authorization"] = f"Bearer {provider.bearer_token}"
    if content_type:
        headers["Content-Type"] = content_type
    return headers


class _RejectProviderRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _open_provider_request(request: urllib.request.Request, timeout: float) -> Any:
    opener = urllib.request.build_opener(_RejectProviderRedirectHandler())
    return opener.open(request, timeout=timeout)


def _read_limited(resp: Any, limit: int, label: str) -> bytes:
    content_length = resp.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > limit:
                raise RuntimeError(f"{label} exceeded the {limit // (1024 * 1024)} MB safety limit")
        except ValueError:
            pass
    data = resp.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError(f"{label} exceeded the {limit // (1024 * 1024)} MB safety limit")
    return data


def _header_value(headers: Any, *names: str) -> str | None:
    if headers is None:
        return None
    for name in names:
        value = headers.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _parse_retry_after(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _find_nested_value(value: Any, *names: str) -> Any:
    wanted = set(names)
    if isinstance(value, Mapping):
        for name in names:
            if name in value and value[name] not in (None, ""):
                return value[name]
        for nested in value.values():
            found = _find_nested_value(nested, *wanted)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_nested_value(nested, *wanted)
            if found not in (None, ""):
                return found
    return None


def _upstream_error_details(
    error: urllib.error.HTTPError,
    provider: ProviderConfig,
    prompt: str,
) -> tuple[str, str, str | None, str | None, float | None]:
    try:
        raw = error.read(MAX_ERROR_RESPONSE_BYTES + 1)
    except Exception:
        raw = b""
    finally:
        try:
            error.close()
        except Exception:
            pass
    detail = ""
    request_id = _header_value(error.headers, "x-request-id", "request-id", "x-correlation-id")
    cf_ray = _header_value(error.headers, "cf-ray")
    retry_after = _parse_retry_after(_header_value(error.headers, "retry-after"))
    if raw and len(raw) <= MAX_ERROR_RESPONSE_BYTES:
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
            error_value = payload.get("error", payload) if isinstance(payload, dict) else payload
            if isinstance(error_value, dict):
                detail = str(error_value.get("message") or error_value.get("code") or "")
                request_id = request_id or str(
                    _find_nested_value(error_value, "request_id", "requestId") or ""
                ).strip() or None
                cf_ray = cf_ray or str(
                    _find_nested_value(error_value, "cf_ray", "ray_id") or ""
                ).strip() or None
                if retry_after is None:
                    retry_after = _parse_retry_after(
                        _find_nested_value(error_value, "retry_after", "retryAfter")
                    )
            elif isinstance(error_value, str):
                detail = error_value
        except json.JSONDecodeError:
            detail = ""
    suffix = f": {detail}" if detail else ""
    secret_values = (*provider.secret_values, prompt)
    message = redact_secrets(
        f"provider request failed with HTTP {error.code}{suffix}",
        secret_values,
    )
    if request_id:
        request_id = redact_secrets(request_id, secret_values)
    if cf_ray:
        cf_ray = redact_secrets(cf_ray, secret_values)
    error_kind = "upstream_error"
    return message, error_kind, request_id, cf_ray, retry_after


def post_provider_request(
    provider: ProviderConfig,
    url: str,
    body: bytes,
    content_type: str,
    prompt: str,
) -> dict[str, Any]:
    started = time.monotonic()
    request = urllib.request.Request(
        url,
        data=body,
        headers=_request_headers(provider, content_type=content_type),
        method="POST",
    )
    try:
        with _open_provider_request(request, TIMEOUT_SECONDS) as resp:
            raw = _read_limited(resp, MAX_JSON_RESPONSE_BYTES, "provider response")
    except urllib.error.HTTPError as exc:
        message, error_kind, request_id, cf_ray, retry_after = _upstream_error_details(
            exc, provider, prompt
        )
        raise ProviderRequestError(
            message,
            error_kind=error_kind,
            http_status=int(exc.code),
            request_id=request_id,
            cf_ray=cf_ray,
            elapsed_ms=round((time.monotonic() - started) * 1000),
            attempts=1,
            retry_after=retry_after,
            first_http_status=int(exc.code),
            first_error_message=message,
        ) from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        reason_value = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        reason = redact_secrets(str(reason_value), (*provider.secret_values, prompt))
        raise ProviderRequestError(
            f"provider request failed: {reason}",
            error_kind="upstream_error",
            elapsed_ms=round((time.monotonic() - started) * 1000),
            attempts=1,
        ) from exc
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderRequestError(
            "provider response was not valid UTF-8 JSON",
            error_kind="output_decode_error",
            elapsed_ms=round((time.monotonic() - started) * 1000),
            attempts=1,
        ) from exc
    if not isinstance(result, dict):
        raise ProviderRequestError(
            "provider response JSON must be an object",
            error_kind="output_decode_error",
            elapsed_ms=round((time.monotonic() - started) * 1000),
            attempts=1,
        )
    return result


def post_images(
    provider: ProviderConfig,
    operation: str,
    prompt: str,
    image_paths: Sequence[Path] | None = None,
    size: str = AUTO_SIZE,
) -> dict[str, Any]:
    paths = list(image_paths or [])
    if operation == "generate":
        if paths:
            raise ValueError("generate operation does not accept input images")
        payload = build_images_generation_payload(prompt, size)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        content_type = "application/json"
    elif operation == "edit":
        body, content_type = build_images_edit_multipart(prompt, paths, size)
    else:
        raise ValueError("operation must be 'generate' or 'edit'")

    return post_provider_request(
        provider,
        build_images_url(provider.base_url, operation),
        body,
        content_type,
        prompt,
    )


def probe_provider(provider: ProviderConfig, timeout: float = PROBE_TIMEOUT_SECONDS) -> dict[str, Any]:
    url = build_models_url(provider.base_url)
    request = urllib.request.Request(url, headers=_request_headers(provider), method="GET")
    try:
        with _open_provider_request(request, timeout) as resp:
            resp.read(4096)
            return {"reachable": True, "http_status": int(resp.status), "endpoint": url}
    except urllib.error.HTTPError as exc:
        # Any HTTP response proves that the configured provider is reachable.
        return {"reachable": True, "http_status": int(exc.code), "endpoint": url}
    except urllib.error.URLError as exc:
        reason = redact_secrets(str(exc.reason), provider.secret_values)
        return {"reachable": False, "http_status": None, "endpoint": url, "error": reason}


def _classify_result_value(
    value: str,
    field_name: str,
    source: str,
    mime_type: str | None,
    output_format: str | None,
) -> ImageResult:
    normalized = value.strip()
    if field_name == "url":
        kind = "url"
    elif normalized.startswith("data:image/"):
        kind = "data_url"
    else:
        kind = "base64"
    return ImageResult(
        value=normalized,
        kind=kind,
        source=source,
        mime_type=mime_type,
        output_format=output_format,
    )


def extract_image_results(payload: Mapping[str, Any]) -> list[ImageResult]:
    """Extract only explicit Images API b64_json/url fields from known envelopes."""

    results: list[ImageResult] = []
    seen: set[tuple[str, str]] = set()

    def add_item(item: Mapping[str, Any], source: str) -> None:
        mime_type = str(item.get("mime_type") or "").strip() or None
        output_format = str(item.get("output_format") or "").strip() or None
        for field_name in ("b64_json", "url"):
            value = item.get(field_name)
            if not isinstance(value, str) or not value.strip():
                continue
            normalized = value.strip()
            identity = (field_name, normalized)
            if identity in seen:
                continue
            seen.add(identity)
            results.append(
                _classify_result_value(
                    normalized,
                    field_name,
                    f"{source}.{field_name}",
                    mime_type,
                    output_format,
                )
            )

    def walk_envelope(envelope: Mapping[str, Any], source: str) -> None:
        for container_name in ("data", "images"):
            container = envelope.get(container_name)
            if isinstance(container, Mapping):
                add_item(container, f"{source}.{container_name}")
            elif isinstance(container, list):
                for index, item in enumerate(container):
                    if isinstance(item, Mapping):
                        add_item(item, f"{source}.{container_name}[{index}]")
        response = envelope.get("response")
        if isinstance(response, Mapping):
            walk_envelope(response, f"{source}.response")

    walk_envelope(payload, "$")
    if not results:
        raise RuntimeError("provider response did not include an Images API b64_json or url result")
    return results


def validate_download_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("image result URL must be an absolute HTTPS URL")
    if parsed.username or parsed.password:
        raise RuntimeError("image result URL must not contain embedded credentials")
    hostname = parsed.hostname or ""
    if _is_loopback_host(hostname) or hostname.lower().endswith(".local"):
        raise RuntimeError("image result URL points to a local address")
    try:
        address = ipaddress.ip_address(hostname)
        if not address.is_global:
            raise RuntimeError("image result URL points to a non-public address")
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(sockaddr[0])
                for _family, _type, _proto, _canonname, sockaddr in socket.getaddrinfo(
                    hostname,
                    parsed.port or 443,
                    type=socket.SOCK_STREAM,
                )
            }
        except socket.gaierror as exc:
            raise RuntimeError("image result URL hostname could not be resolved") from exc
        if not addresses or any(not address.is_global for address in addresses):
            raise RuntimeError("image result URL resolves to a non-public address")
    return value


class _SafeImageRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        validate_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _decode_image_result(
    result: ImageResult, timeout: float = TIMEOUT_SECONDS
) -> tuple[bytes, str, str]:
    if result.kind == "url":
        url = validate_download_url(result.value)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*",
                "User-Agent": "podotion-image-skill/2.0",
            },
        )
        opener = urllib.request.build_opener(_SafeImageRedirectHandler())
        with opener.open(request, timeout=timeout) as resp:
            content_type = resp.headers.get_content_type()
            if not content_type.startswith("image/"):
                raise RuntimeError(f"image download returned unexpected content type {content_type!r}")
            data = _read_limited(resp, MAX_IMAGE_DOWNLOAD_BYTES, "image download")
    else:
        encoded = result.value
        if result.kind == "data_url":
            header, separator, encoded = result.value.partition(",")
            if (
                not separator
                or not header.lower().startswith("data:image/")
                or ";base64" not in header.lower()
            ):
                raise RuntimeError("image result contained an invalid data URL")
        encoded = "".join(encoded.split())
        max_encoded_bytes = 4 * math.ceil(MAX_IMAGE_DOWNLOAD_BYTES / 3)
        if len(encoded) < 32:
            raise RuntimeError("image result base64 data was too short")
        if len(encoded) > max_encoded_bytes:
            raise RuntimeError("image result base64 data exceeded the encoded size limit")
        if (
            len(encoded) % 4
            or re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", encoded) is None
        ):
            raise RuntimeError("image result contained invalid base64 characters or padding")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RuntimeError("image result contained invalid base64 data") from exc
        if len(data) > MAX_IMAGE_DOWNLOAD_BYTES:
            raise RuntimeError("decoded image exceeded the 50 MB safety limit")

    mime_type, suffix = _detect_image_type(data)
    return data, mime_type, suffix


def _png_dimensions(data: bytes) -> tuple[int | None, int | None]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    return None, None


def save_image_results(
    results: Sequence[ImageResult], output_dir: Path, timeout: float = TIMEOUT_SECONDS
) -> tuple[list[SavedImage], list[dict[str, Any]]]:
    if not results:
        raise RuntimeError("no image results to save")

    candidate_count = len(results)
    decoded: list[tuple[bytes, str, str]] = []
    warnings: list[dict[str, Any]] = []
    for index, result in enumerate(results, start=1):
        try:
            decoded.append(_decode_image_result(result, timeout))
        except Exception as exc:
            reason = (
                "image result URL could not be downloaded"
                if isinstance(exc, urllib.error.URLError)
                else str(exc)
            )
            warnings.append(
                {
                    "code": "invalid_image_candidate",
                    "message": (
                        "ignored an explicitly declared image candidate that failed validation"
                    ),
                    "result_index": index,
                    "candidate_count": candidate_count,
                    "source": result.source,
                    "value_length": len(result.value),
                    "reason": reason,
                }
            )
    if not decoded:
        raise ImageCandidateError(candidate_count, warnings)

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    multiple = len(decoded) > 1
    saved: list[SavedImage] = []
    staged: list[tuple[Path, Path, SavedImage]] = []
    temporary_paths: list[Path] = []
    committed: list[Path] = []
    try:
        for index, (data, mime_type, suffix) in enumerate(decoded, start=1):
            filename = f"{stamp}_{index:02d}{suffix}" if multiple else f"{stamp}{suffix}"
            destination = output_dir / filename
            if destination.exists():
                raise RuntimeError("generated image destination already exists")
            temporary = output_dir / f".{filename}.{secrets.token_hex(6)}.tmp"
            temporary_paths.append(temporary)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            width, height = _png_dimensions(data)
            staged.append(
                (
                    temporary,
                    destination,
                    SavedImage(
                        path=destination,
                        mime_type=mime_type,
                        bytes=len(data),
                        width=width,
                        height=height,
                    ),
                )
            )
        for temporary, destination, image in staged:
            os.replace(temporary, destination)
            committed.append(destination)
            saved.append(image)
    except Exception as exc:
        for temporary in temporary_paths:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        for destination in committed:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
        raise RuntimeError("failed to atomically save the decoded image batch") from exc
    return saved, warnings


def image_size_warnings(
    requested_size: str, images: Sequence[SavedImage]
) -> list[dict[str, Any]]:
    if requested_size == AUTO_SIZE:
        return []
    expected_width, expected_height = (int(part) for part in requested_size.split("x", 1))
    warnings: list[dict[str, Any]] = []
    for index, image in enumerate(images, start=1):
        if image.width is None or image.height is None:
            continue
        if (image.width, image.height) == (expected_width, expected_height):
            continue
        warnings.append(
            {
                "code": "image_size_mismatch",
                "message": "provider returned different pixel dimensions than requested",
                "image_index": index,
                "requested_size": requested_size,
                "actual_size": f"{image.width}x{image.height}",
            }
        )
    return warnings


def _safe_thread_id(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    thread_id = str(env.get("CODEX_THREAD_ID") or "unscoped")
    safe_thread_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", thread_id).strip("._") or "unscoped"
    if len(safe_thread_id) > 96:
        digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:12]
        safe_thread_id = f"{safe_thread_id[:80]}-{digest}"
    return safe_thread_id


def _state_path(
    output_dir: Path, environ: Mapping[str, str] | None = None
) -> Path:
    return output_dir.resolve() / ".state" / _safe_thread_id(environ) / "last.json"


def normalize_request_key(value: str) -> str:
    request_key = str(value).strip()
    if not REQUEST_KEY_PATTERN.fullmatch(request_key):
        raise argparse.ArgumentTypeError(
            "request key must be 8-128 ASCII letters, digits, dots, underscores, or hyphens"
        )
    return request_key


def _request_state_dir(
    output_dir: Path, environ: Mapping[str, str] | None = None
) -> Path:
    return output_dir.resolve() / ".state" / _safe_thread_id(environ)


def _request_record_path(
    output_dir: Path,
    request_key: str,
    environ: Mapping[str, str] | None = None,
) -> Path:
    normalized = normalize_request_key(request_key)
    return _request_state_dir(output_dir, environ) / "requests" / f"{normalized}.json"


def _request_lock_path(
    output_dir: Path,
    fingerprint: str,
    environ: Mapping[str, str] | None = None,
) -> Path:
    return _request_state_dir(output_dir, environ) / "locks" / f"{fingerprint}.lock"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_request_record(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid request state: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid request state: {path}")
    return value


def _lock_file(handle: Any) -> bool:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise


def _unlock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _request_lock(path: Path) -> Iterable[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        acquired = _lock_file(handle)
        try:
            yield acquired
        finally:
            if acquired:
                _unlock_file(handle)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _request_fingerprint(
    operation: str,
    endpoint: str,
    size: str,
    output_dir: Path,
    state_scope: str,
    prompt: str,
    input_images: Sequence[Path],
) -> tuple[str, dict[str, Any]]:
    image_metadata = [
        {"sha256": _sha256_file(path), "bytes": path.stat().st_size}
        for path in input_images
    ]
    metadata: dict[str, Any] = {
        "fingerprint_version": 1,
        "operation": operation,
        "model": IMAGE_MODEL,
        "endpoint": endpoint,
        "size": size,
        "output_dir": os.path.normcase(str(output_dir.resolve())),
        "state_scope": state_scope,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "input_images": image_metadata,
    }
    encoded = json.dumps(
        metadata, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), metadata


def _iter_request_records(
    output_dir: Path, environ: Mapping[str, str] | None = None
) -> Iterable[tuple[Path, dict[str, Any]]]:
    directory = _request_state_dir(output_dir, environ) / "requests"
    if not directory.is_dir():
        return
    for path in directory.glob("*.json"):
        record = _read_request_record(path)
        if record is not None:
            yield path, record


def _record_saved_images(record: Mapping[str, Any]) -> list[SavedImage] | None:
    values = record.get("images")
    if not isinstance(values, list) or not values:
        return None
    images: list[SavedImage] = []
    for value in values:
        if not isinstance(value, Mapping) or not isinstance(value.get("path"), str):
            return None
        path = Path(str(value["path"])).expanduser().resolve()
        if not path.is_file():
            return None
        expected_bytes = value.get("bytes")
        if isinstance(expected_bytes, int) and path.stat().st_size != expected_bytes:
            return None
        expected_sha256 = value.get("sha256")
        if isinstance(expected_sha256, str) and _sha256_file(path) != expected_sha256:
            return None
        images.append(
            SavedImage(
                path=path,
                mime_type=str(value.get("mime_type") or "application/octet-stream"),
                bytes=path.stat().st_size,
                width=value.get("width") if isinstance(value.get("width"), int) else None,
                height=value.get("height") if isinstance(value.get("height"), int) else None,
            )
        )
    return images


def _request_failure(exc: ProviderRequestError) -> dict[str, Any]:
    return {
        "error_kind": exc.error_kind,
        "http_status": exc.http_status,
        "request_id": exc.request_id,
        "cf_ray": exc.cf_ray,
        "attempts": exc.attempts,
    }


def _request_state_error(
    message: str,
    error_kind: str,
    record: Mapping[str, Any] | None = None,
) -> ProviderRequestError:
    details: dict[str, Any] = {}
    if record:
        details = {
            "request_key": record.get("request_key"),
            "fingerprint": record.get("fingerprint"),
            "status": record.get("status"),
            "may_have_been_billed": bool(record.get("may_have_been_billed")),
        }
    return ProviderRequestError(
        message,
        error_kind=error_kind,
        attempts=0,
        details=details,
    )


def _is_recent_success(record: Mapping[str, Any]) -> bool:
    if record.get("status") != "succeeded" or _record_saved_images(record) is None:
        return False
    raw_completed = record.get("completed_at")
    if not isinstance(raw_completed, str):
        return False
    try:
        completed = datetime.fromisoformat(raw_completed)
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    age = (_now_utc() - completed).total_seconds()
    return 0 <= age <= REQUEST_REUSE_SECONDS


def get_request_status(
    output_dir: Path | str,
    request_key: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    directory = Path(output_dir).expanduser().resolve()
    path = _request_record_path(directory, request_key, environ)
    record = _read_request_record(path)
    if record is None:
        return {
            "ok": True,
            "found": False,
            "request_key": normalize_request_key(request_key),
            "state_path": str(path),
        }

    stored_status = str(record.get("status") or "unknown")
    effective_status = stored_status
    if stored_status in {"prepared", "submitted"} and isinstance(
        record.get("fingerprint"), str
    ):
        lock_path = _request_lock_path(directory, str(record["fingerprint"]), environ)
        with _request_lock(lock_path) as acquired:
            if not acquired:
                effective_status = "request_in_progress"
            elif stored_status == "submitted":
                effective_status = "outcome_unknown"

    recommendation = "do_not_resubmit"
    if effective_status == "request_in_progress":
        recommendation = "wait_and_check_the_same_request_key"
    elif effective_status == "succeeded":
        recommendation = "reuse_saved_images"
    elif effective_status in {"outcome_unknown", "completed_unusable"}:
        recommendation = "ask_before_abandoning_possible_charge"
    elif effective_status == "prepared":
        recommendation = "resume_the_same_request_key"

    return {
        "ok": True,
        "found": True,
        "request_key": record.get("request_key"),
        "stored_status": stored_status,
        "effective_status": effective_status,
        "may_have_been_billed": bool(record.get("may_have_been_billed")),
        "safe_to_retry": effective_status == "prepared",
        "recommendation": recommendation,
        "state_path": str(path),
        "request": record,
    }


def abandon_request(
    output_dir: Path | str,
    request_key: str,
    acknowledge_possible_charge: bool,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if not acknowledge_possible_charge:
        raise ValueError("abandoning a request requires acknowledging a possible charge")
    directory = Path(output_dir).expanduser().resolve()
    path = _request_record_path(directory, request_key, environ)
    record = _read_request_record(path)
    if record is None:
        raise FileNotFoundError(f"request state not found at {path}")
    fingerprint = record.get("fingerprint")
    if not isinstance(fingerprint, str):
        raise RuntimeError(f"invalid request state: {path}")
    lock_path = _request_lock_path(directory, fingerprint, environ)
    with _request_lock(lock_path) as acquired:
        if not acquired:
            raise _request_state_error(
                "request is still in progress and cannot be abandoned",
                "request_in_progress",
                record,
            )
        status = record.get("status")
        if status == "succeeded":
            raise _request_state_error(
                "a successful request cannot be abandoned",
                "request_already_succeeded",
                record,
            )
        if status == "abandoned":
            return {
                "ok": True,
                "request_key": request_key,
                "status": "abandoned",
                "state_path": str(path),
            }
        record.update(
            {
                "status": "abandoned",
                "updated_at": _now_iso(),
                "abandoned_at": _now_iso(),
                "may_have_been_billed": bool(record.get("may_have_been_billed")),
            }
        )
        _atomic_write_json(path, record)
    return {
        "ok": True,
        "request_key": request_key,
        "status": "abandoned",
        "may_have_been_billed": bool(record.get("may_have_been_billed")),
        "state_path": str(path),
    }


def write_last_state(
    output_dir: Path,
    images: Sequence[SavedImage],
    operation: str,
    size: str,
    environ: Mapping[str, str] | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = _state_path(output_dir, environ)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "model": IMAGE_MODEL,
        "size": size,
        "images": [image.as_json() for image in images],
    }
    temporary = state_path.with_name(f".{state_path.name}.{secrets.token_hex(6)}.tmp")
    try:
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(state_path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return state_path


def read_last_image(
    output_dir: Path, environ: Mapping[str, str] | None = None
) -> Path:
    state_path = _state_path(output_dir, environ)
    if not state_path.is_file():
        raise FileNotFoundError(f"no previous generated image state found at {state_path}")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid previous image state: {state_path}") from exc
    images = state.get("images") if isinstance(state, dict) else None
    if not isinstance(images, list) or not images:
        raise RuntimeError("previous image state does not contain an image")
    if len(images) != 1:
        raise RuntimeError(
            f"previous generation contains {len(images)} images; ask the user to choose one and pass --image"
        )
    item = images[0]
    if not isinstance(item, dict) or not isinstance(item.get("path"), str):
        raise RuntimeError("previous image state contains an invalid path")
    path = Path(item["path"]).expanduser().resolve()
    validate_input_file(path, "previous image")
    return path


def _default_codex_home(environ: Mapping[str, str]) -> Path:
    return Path(codex_home_path(environ=environ))


def default_output_dir(cwd: Path | None = None) -> Path:
    return (Path.cwd() if cwd is None else cwd).expanduser().resolve() / "PodotionImage"


def resolve_output_dir(value: str | None, cwd: Path | None = None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    return default_output_dir(cwd)


def read_prompt(args: argparse.Namespace) -> str:
    if getattr(args, "prompt", None) is not None:
        prompt = args.prompt
    else:
        prompt_file = str(args.prompt_file)
        if prompt_file == "-":
            prompt = sys.stdin.read(MAX_PROMPT_BYTES + 1)
        else:
            path = Path(prompt_file).expanduser()
            if not path.is_file():
                raise FileNotFoundError(f"prompt file not found: {path}")
            if path.stat().st_size > MAX_PROMPT_BYTES:
                raise ValueError("prompt file exceeds the 1 MB safety limit")
            prompt = path.read_text(encoding="utf-8")
    normalized = prompt.strip()
    if not normalized:
        raise ValueError("prompt cannot be empty")
    if len(normalized.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError("prompt exceeds the 1 MB safety limit")
    return normalized


def _generation_result(
    operation: str,
    provider: ProviderConfig,
    endpoint: str,
    size: str,
    input_images: Sequence[Path],
    saved: Sequence[SavedImage],
    state_path: Path,
    warnings: Sequence[Mapping[str, Any]],
    request_key: str,
    fingerprint: str,
    *,
    reused: bool = False,
    reused_from_request_key: str | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "operation": operation,
        "model": IMAGE_MODEL,
        "provider": {
            "id": provider.provider_id,
            "name": provider.name,
            "credential_mode": provider.credential_mode,
        },
        "request": {
            "transport": "images",
            "endpoint": endpoint,
            "size": size,
            "input_image_count": len(input_images),
            "request_key": request_key,
            "fingerprint": fingerprint,
            "provider_timeout_seconds": TIMEOUT_SECONDS,
            "upstream_attempts": 0 if reused else 1,
            "reused": reused,
            "reused_from_request_key": reused_from_request_key,
        },
        "images": [image.as_json() for image in saved],
        "state_path": str(state_path),
        "warnings": [dict(warning) for warning in warnings],
    }


def _result_from_request_record(
    record: Mapping[str, Any],
    provider: ProviderConfig,
    endpoint: str,
    size: str,
    input_images: Sequence[Path],
    request_key: str,
    fingerprint: str,
    reused_from_request_key: str | None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    saved = _record_saved_images(record)
    if saved is None:
        raise _request_state_error(
            "saved images for this request are missing or changed",
            "request_result_missing",
            record,
        )
    state_value = record.get("last_state_path")
    state_path = (
        Path(str(state_value)).expanduser().resolve()
        if isinstance(state_value, str)
        else _state_path(Path(str(record["output_dir"])), environ)
    )
    warnings = record.get("warnings")
    if not isinstance(warnings, list):
        warnings = []
    return _generation_result(
        str(record.get("operation") or "generate"),
        provider,
        endpoint,
        size,
        input_images,
        saved,
        state_path,
        warnings,
        request_key,
        fingerprint,
        reused=True,
        reused_from_request_key=reused_from_request_key,
    )


def run_generation(args: argparse.Namespace, operation: str) -> dict[str, Any]:
    started = time.monotonic()
    provider = load_direct_provider(args.credential_file)
    prompt = read_prompt(args)
    output_dir = resolve_output_dir(args.output_dir)
    size = resolve_request_size(args.size, args.ratio)
    request_key = normalize_request_key(args.request_key)
    force_new = bool(getattr(args, "force_new", False))
    state_scope_value = getattr(args, "state_scope", None)
    state_environ = (
        {"CODEX_THREAD_ID": str(state_scope_value)}
        if state_scope_value is not None
        else None
    )
    state_scope = _safe_thread_id(state_environ)

    input_images: list[Path] = []
    if operation == "edit":
        if args.last:
            input_images = [read_last_image(output_dir, state_environ)]
        else:
            input_images = [Path(value).expanduser().resolve() for value in args.image]
            for path in input_images:
                validate_input_file(path)

    endpoint = build_images_url(provider.base_url, operation)
    fingerprint, fingerprint_metadata = _request_fingerprint(
        operation, endpoint, size, output_dir, state_scope, prompt, input_images
    )
    record_path = _request_record_path(output_dir, request_key, state_environ)
    lock_path = _request_lock_path(output_dir, fingerprint, state_environ)

    with _request_lock(lock_path) as acquired:
        if not acquired:
            active_record = next(
                (
                    record
                    for _path, record in _iter_request_records(output_dir, state_environ)
                    if record.get("fingerprint") == fingerprint
                    and record.get("status") in {"prepared", "submitted"}
                ),
                None,
            )
            raise _request_state_error(
                "an equivalent image request is still in progress; check its status instead of resubmitting",
                "request_in_progress",
                active_record
                or {
                    "request_key": request_key,
                    "fingerprint": fingerprint,
                    "status": "submitted",
                    "may_have_been_billed": True,
                },
            )

        existing = _read_request_record(record_path)
        if existing is not None and existing.get("fingerprint") != fingerprint:
            raise _request_state_error(
                "request key was already used for different image inputs",
                "request_key_conflict",
                existing,
            )
        if existing is not None:
            status = existing.get("status")
            if status == "succeeded":
                if _record_saved_images(existing) is None:
                    existing.update(
                        {
                            "status": "completed_unusable",
                            "updated_at": _now_iso(),
                            "failure": {"error_kind": "request_result_missing"},
                        }
                    )
                    _atomic_write_json(record_path, existing)
                    raise _request_state_error(
                        "saved images for this request are missing or changed; the request cannot be sent again",
                        "request_result_missing",
                        existing,
                    )
                else:
                    return _result_from_request_record(
                        existing,
                        provider,
                        endpoint,
                        size,
                        input_images,
                        request_key,
                        fingerprint,
                        str(existing.get("reused_from_request_key") or request_key),
                        state_environ,
                    )
            elif status == "prepared":
                pass
            elif status == "submitted":
                existing.update(
                    {
                        "status": "outcome_unknown",
                        "updated_at": _now_iso(),
                        "may_have_been_billed": True,
                        "failure": {
                            "error_kind": "interrupted_after_submission",
                            "message": "the prior process ended after submission without a durable result",
                        },
                    }
                )
                _atomic_write_json(record_path, existing)
                raise _request_state_error(
                    "the prior request outcome is unknown; do not resubmit it automatically",
                    "request_outcome_unknown",
                    existing,
                )
            else:
                raise _request_state_error(
                    "request key has already reached a terminal state and cannot send another upstream request",
                    f"request_{status or 'state_invalid'}",
                    existing,
                )

        for prior_path, prior in _iter_request_records(output_dir, state_environ):
            if prior_path == record_path or prior.get("fingerprint") != fingerprint:
                continue
            prior_status = prior.get("status")
            if prior_status == "submitted":
                prior.update(
                    {
                        "status": "outcome_unknown",
                        "updated_at": _now_iso(),
                        "may_have_been_billed": True,
                    }
                )
                _atomic_write_json(prior_path, prior)
                prior_status = "outcome_unknown"
            if prior_status in {"outcome_unknown", "completed_unusable"}:
                raise _request_state_error(
                    "an equivalent request may already have been charged; explicitly abandon it before creating another",
                    "request_outcome_unknown",
                    prior,
                )
            if prior_status == "succeeded" and _is_recent_success(prior) and not force_new:
                alias = dict(prior)
                alias.update(
                    {
                        "request_key": request_key,
                        "created_at": _now_iso(),
                        "updated_at": _now_iso(),
                        "upstream_attempts": 0,
                        "may_have_been_billed": False,
                        "reused_from_request_key": prior.get("request_key"),
                    }
                )
                _atomic_write_json(record_path, alias)
                return _result_from_request_record(
                    alias,
                    provider,
                    endpoint,
                    size,
                    input_images,
                    request_key,
                    fingerprint,
                    str(prior.get("request_key")),
                    state_environ,
                )

        now = _now_iso()
        record: dict[str, Any] = {
            "version": REQUEST_RECORD_VERSION,
            "request_key": request_key,
            "fingerprint": fingerprint,
            **fingerprint_metadata,
            "status": "prepared",
            "force_new": force_new,
            "created_at": now,
            "updated_at": now,
            "submitted_at": None,
            "completed_at": None,
            "upstream_attempts": 0,
            "may_have_been_billed": False,
            "images": [],
            "warnings": [],
            "failure": None,
        }
        _atomic_write_json(record_path, record)

        submitted_at = _now_iso()
        record.update(
            {
                "status": "submitted",
                "updated_at": submitted_at,
                "submitted_at": submitted_at,
                "upstream_attempts": 1,
                "may_have_been_billed": True,
            }
        )
        _atomic_write_json(record_path, record)

        try:
            response = post_images(provider, operation, prompt, input_images, size)
        except ProviderRequestError as exc:
            if exc.error_kind == "output_decode_error":
                request_status = "completed_unusable"
            elif exc.http_status is not None and 400 <= exc.http_status < 500:
                request_status = "failed_definitive"
            else:
                request_status = "outcome_unknown"
            record.update(
                {
                    "status": request_status,
                    "updated_at": _now_iso(),
                    "completed_at": _now_iso(),
                    "failure": _request_failure(exc),
                }
            )
            _atomic_write_json(record_path, record)
            exc.details.update(
                {
                    "request_key": request_key,
                    "fingerprint": fingerprint,
                    "status": request_status,
                    "may_have_been_billed": True,
                }
            )
            raise
        except BaseException:
            # Keep the durable submitted marker. A later invocation will treat it
            # as unknown once this process releases the fingerprint lock.
            raise

        try:
            results = extract_image_results(response)
            saved, warnings = save_image_results(results, output_dir, TIMEOUT_SECONDS)
        except ImageCandidateError as exc:
            error = ProviderRequestError(
                str(exc),
                error_kind="output_decode_error",
                elapsed_ms=round((time.monotonic() - started) * 1000),
                attempts=1,
                details=exc.details,
            )
            record.update(
                {
                    "status": "completed_unusable",
                    "updated_at": _now_iso(),
                    "completed_at": _now_iso(),
                    "failure": _request_failure(error),
                }
            )
            _atomic_write_json(record_path, record)
            error.details.update(
                {
                    "request_key": request_key,
                    "fingerprint": fingerprint,
                    "status": "completed_unusable",
                    "may_have_been_billed": True,
                }
            )
            raise error from exc
        except Exception as exc:
            error = ProviderRequestError(
                redact_secrets(str(exc), (*provider.secret_values, prompt)),
                error_kind="output_decode_error",
                elapsed_ms=round((time.monotonic() - started) * 1000),
                attempts=1,
            )
            record.update(
                {
                    "status": "completed_unusable",
                    "updated_at": _now_iso(),
                    "completed_at": _now_iso(),
                    "failure": _request_failure(error),
                }
            )
            _atomic_write_json(record_path, record)
            raise error from exc

        warnings.extend(image_size_warnings(size, saved))
        try:
            state_path = write_last_state(
                output_dir, saved, operation, size, state_environ
            )
        except Exception as exc:
            for image in saved:
                try:
                    image.path.unlink()
                except FileNotFoundError:
                    pass
            error = ProviderRequestError(
                "failed to write image state; saved image batch was rolled back",
                error_kind="output_save_error",
                elapsed_ms=round((time.monotonic() - started) * 1000),
                attempts=1,
            )
            record.update(
                {
                    "status": "completed_unusable",
                    "updated_at": _now_iso(),
                    "completed_at": _now_iso(),
                    "failure": _request_failure(error),
                }
            )
            _atomic_write_json(record_path, record)
            raise error from exc

        record_images = []
        for image in saved:
            value = image.as_json()
            value["sha256"] = _sha256_file(image.path)
            record_images.append(value)
        completed_at = _now_iso()
        record.update(
            {
                "status": "succeeded",
                "updated_at": completed_at,
                "completed_at": completed_at,
                "last_state_path": str(state_path),
                "images": record_images,
                "warnings": [dict(warning) for warning in warnings],
                "failure": None,
            }
        )
        try:
            _atomic_write_json(record_path, record)
        except Exception as exc:
            for image in saved:
                try:
                    image.path.unlink()
                except FileNotFoundError:
                    pass
            try:
                state_path.unlink()
            except FileNotFoundError:
                pass
            raise ProviderRequestError(
                "failed to commit request state; saved image batch was rolled back",
                error_kind="output_save_error",
                elapsed_ms=round((time.monotonic() - started) * 1000),
                attempts=1,
                details={
                    "request_key": request_key,
                    "fingerprint": fingerprint,
                    "status": "outcome_unknown",
                    "may_have_been_billed": True,
                },
            ) from exc

        return _generation_result(
            operation,
            provider,
            endpoint,
            size,
            input_images,
            saved,
            state_path,
            warnings,
            request_key,
            fingerprint,
        )


def run_doctor(args: argparse.Namespace) -> dict[str, Any]:
    provider = load_direct_provider(args.credential_file)
    probe = probe_provider(provider)
    capability: dict[str, Any] = {
        "attempted": False,
        "may_bill": False,
        "note": "use --image-probe to test billable image generation",
    }
    if args.image_probe:
        capability = {
            "attempted": True,
            "may_bill": True,
            "max_attempts": 1,
            "available": False,
        }
        prompt = "Generate a plain light gray square with no text."
        size = resolve_size("1k", "1:1")
        try:
            response = post_images(
                provider,
                "generate",
                prompt,
                size=size,
            )
            endpoint = build_images_url(provider.base_url, "generate")
            results = extract_image_results(response)
            output_dir = default_output_dir() / "doctor-probe"
            saved, warnings = save_image_results(results, output_dir, TIMEOUT_SECONDS)
            warnings.extend(image_size_warnings(size, saved))
            capability.update({
                "available": True,
                "endpoint": endpoint,
                "image_result_count": len(saved),
                "images": [image.as_json() for image in saved],
                "warnings": warnings,
            })
        except ProviderRequestError as exc:
            capability["error"] = exc.as_json()
        except ImageCandidateError as exc:
            capability["error"] = {
                "type": type(exc).__name__,
                "error_kind": "output_decode_error",
                "message": str(exc),
                "details": exc.details,
            }
        except RuntimeError as exc:
            capability["error"] = {
                "type": type(exc).__name__,
                "error_kind": "output_decode_error",
                "message": redact_secrets(str(exc), provider.secret_values),
            }
    ok = bool(probe["reachable"]) and (
        not args.image_probe or bool(capability.get("available"))
    )
    return {
        "ok": ok,
        "site": SITE_NAME,
        "provider_id": provider.provider_id,
        "provider_name": provider.name,
        "base_url": provider.base_url,
        "credential_mode": provider.credential_mode,
        "config_path": str(provider.config_path) if provider.config_path else None,
        "transport": "images",
        "endpoints": {
            "generate": build_images_url(provider.base_url, "generate"),
            "edit": build_images_url(provider.base_url, "edit"),
        },
        "model": IMAGE_MODEL,
        "output_directory": str(default_output_dir()),
        "connectivity": probe,
        "image_capability": capability,
    }


def run_sizes(_args: argparse.Namespace) -> dict[str, Any]:
    return {
        "ok": True,
        "sizes": {
            tier: {ratio: resolve_size(tier, ratio) for ratio in SUPPORTED_RATIOS}
            for tier in SUPPORTED_TIERS
        },
    }


def run_request_status(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = resolve_output_dir(args.output_dir)
    return get_request_status(output_dir, args.request_key)


def run_request_abandon(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = resolve_output_dir(args.output_dir)
    return abandon_request(
        output_dir,
        args.request_key,
        args.acknowledge_possible_charge,
    )


def _add_prompt_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prompt", help="image prompt")
    group.add_argument("--prompt-file", help="UTF-8 prompt file, or - for stdin")


def _add_render_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--size", choices=SUPPORTED_TIERS.keys(), help="size tier")
    parser.add_argument("--ratio", type=normalize_ratio, help="aspect ratio")
    parser.add_argument(
        "--output-dir",
        help="output directory; defaults to ./PodotionImage in the current working directory",
    )


def _add_request_safety_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--request-key",
        type=normalize_request_key,
        required=True,
        help="stable key for exactly one user image action",
    )
    parser.add_argument(
        "--force-new",
        action="store_true",
        help="create a new variant instead of reusing a recent equivalent success",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate or edit images through Podotion's direct endpoint."
    )
    parser.add_argument(
        "--credential-file",
        help="private provider.toml path; defaults to $CODEX_HOME/podotion-image/provider.toml",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="check provider config and connectivity")
    doctor_parser.add_argument(
        "--image-probe",
        action="store_true",
        help="perform a real, potentially billable image generation capability probe",
    )
    doctor_parser.set_defaults(func=run_doctor)

    sizes_parser = subparsers.add_parser("sizes", help="print the resolved size mapping")
    sizes_parser.set_defaults(func=run_sizes)

    generate_parser = subparsers.add_parser("generate", help="create an image from text")
    _add_prompt_arguments(generate_parser)
    _add_render_arguments(generate_parser)
    _add_request_safety_arguments(generate_parser)
    generate_parser.set_defaults(func=lambda args: run_generation(args, "generate"))

    edit_parser = subparsers.add_parser("edit", help="edit one or more images")
    _add_prompt_arguments(edit_parser)
    edit_source = edit_parser.add_mutually_exclusive_group(required=True)
    edit_source.add_argument("--image", action="append", help="input image; repeat up to five times")
    edit_source.add_argument("--last", action="store_true", help="use the last generated image")
    _add_render_arguments(edit_parser)
    _add_request_safety_arguments(edit_parser)
    edit_parser.set_defaults(func=lambda args: run_generation(args, "edit"))

    status_parser = subparsers.add_parser(
        "request-status", help="inspect a durable image request without contacting the provider"
    )
    status_parser.add_argument("--request-key", type=normalize_request_key, required=True)
    status_parser.add_argument("--output-dir")
    status_parser.set_defaults(func=run_request_status)

    abandon_parser = subparsers.add_parser(
        "request-abandon",
        help="acknowledge and abandon an uncertain request without contacting the provider",
    )
    abandon_parser.add_argument("--request-key", type=normalize_request_key, required=True)
    abandon_parser.add_argument("--output-dir")
    abandon_parser.add_argument(
        "--acknowledge-possible-charge",
        action="store_true",
        required=True,
        help="confirm that the abandoned request may already have been charged",
    )
    abandon_parser.set_defaults(func=run_request_abandon)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = args.func(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", True) else 1
    except Exception as exc:
        message = redact_secrets(str(exc))
        error = (
            exc.as_json()
            if isinstance(exc, ProviderRequestError)
            else {
                "type": type(exc).__name__,
                "error_kind": "local_error",
                "message": message,
                "http_status": None,
                "request_id": None,
                "cf_ray": None,
                "elapsed_ms": None,
                "attempts": 0,
                "retry_after": None,
                "first_http_status": None,
                "first_error_message": None,
            }
        )
        error["message"] = redact_secrets(str(error.get("message") or ""))
        print(
            json.dumps(
                {"ok": False, "error": error},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

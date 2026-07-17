#!/usr/bin/env python3
"""Podotion Image MCP server implemented with the Python standard library."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import inspect
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, BinaryIO, Mapping, Sequence
from urllib.parse import quote


# Python isolated mode intentionally omits the script directory from sys.path.
# The installer launches this server with `-I`, so add only this trusted plugin
# directory before importing the sibling protocol module.
MCP_DIRECTORY = Path(__file__).resolve().parent
if str(MCP_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(MCP_DIRECTORY))
PLUGIN_DIRECTORY = MCP_DIRECTORY.parent
if str(PLUGIN_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIRECTORY))

from podotion_image.paths import describe_path, resolve_workspace_path, runtime_home_path

try:
    from .protocol import (
        LATEST_PROTOCOL_VERSION,
        ProtocolError,
        error_response,
        negotiated_protocol_version,
        read_message,
        request_parts,
        result_response,
        write_message,
    )
except ImportError:
    from protocol import (  # type: ignore[no-redef]
        LATEST_PROTOCOL_VERSION,
        ProtocolError,
        error_response,
        negotiated_protocol_version,
        read_message,
        request_parts,
        result_response,
        write_message,
    )


SERVER_NAME = "podotion-image"
MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
MAX_RESOURCE_BYTES = 50 * 1024 * 1024
REQUEST_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{7,127}$")


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_image_core(root: Path | None = None) -> ModuleType:
    base = plugin_root() if root is None else root.resolve()
    path = base / "skills" / "podotion-image" / "scripts" / "podotion_image.py"
    if not path.is_file():
        raise RuntimeError("Podotion Image executor is missing from the plugin")
    module_name = "podotion_image_plugin_core"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Podotion Image executor could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def server_version(root: Path | None = None) -> str:
    manifest = (plugin_root() if root is None else root) / ".codex-plugin" / "plugin.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "0.0.0"
    version = payload.get("version") if isinstance(payload, dict) else None
    return version if isinstance(version, str) and version.strip() else "0.0.0"


def default_registry_path(environ: Mapping[str, str] | None = None) -> Path:
    home = runtime_home_path(environ=environ)
    return Path(home) / ".codex" / "podotion-image" / "mcp-resources.json"


def _image_mime_type(data: bytes, suffix: str) -> str:
    expected = MIME_TYPES.get(suffix.lower())
    if expected == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return expected
    if expected == "image/jpeg" and data.startswith(b"\xff\xd8\xff"):
        return expected
    if (
        expected == "image/webp"
        and len(data) >= 12
        and data.startswith(b"RIFF")
        and data[8:12] == b"WEBP"
    ):
        return expected
    raise ValueError("file extension and image content do not match a supported format")


def _read_local_image(raw_path: str | Path) -> tuple[Path, bytes, str]:
    path = Path(raw_path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("image output must be a regular file")
    size = path.stat().st_size
    if size <= 0 or size > MAX_RESOURCE_BYTES:
        raise ValueError("image output is empty or exceeds the 50 MB limit")
    data = path.read_bytes()
    return path, data, _image_mime_type(data, path.suffix)


class ResourceRegistry:
    """Persistent allow-list for image resources exposed through MCP."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or default_registry_path()).expanduser().resolve()
        self._items: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        resources = payload.get("resources") if isinstance(payload, dict) else None
        if not isinstance(resources, dict):
            return
        for uri, value in resources.items():
            if isinstance(uri, str) and isinstance(value, dict):
                self._items[uri] = dict(value)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "resources": self._items}
        fd, raw_temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(raw_temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def register(self, raw_path: str | Path, request_key: str) -> dict[str, Any]:
        path = Path(raw_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError("image resource must be a regular file")
        size = path.stat().st_size
        if size <= 0 or size > MAX_RESOURCE_BYTES:
            raise ValueError("image resource is empty or exceeds the 50 MB limit")
        data = path.read_bytes()
        mime_type = _image_mime_type(data, path.suffix)
        digest = hashlib.sha256(data).hexdigest()
        safe_key = _validated_request_key(request_key)
        uri = f"podotion-image://outputs/{quote(safe_key, safe='')}/{quote(path.name, safe='')}"
        item = {
            "uri": uri,
            "name": path.name,
            "title": path.name,
            "description": "Generated or published Podotion image",
            "mimeType": mime_type,
            "size": size,
            "path": str(path),
            "sha256": digest,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        previous = self._items.get(uri)
        if previous is not None and (
            previous.get("path") != item["path"]
            or previous.get("sha256") != item["sha256"]
        ):
            raise ValueError("request_key already identifies a different image resource")
        self._items[uri] = item
        try:
            self._save()
        except OSError as exc:
            # Keep the resource available to the active MCP process. Outputs
            # can still read it immediately; only cross-process durability is
            # reduced when a mounted/shared runtime directory is temporarily
            # not writable.
            item["persistent"] = False
            item["persistence_error"] = type(exc).__name__
        else:
            item["persistent"] = True
        return dict(item)

    def list_resources(self) -> list[dict[str, Any]]:
        resources: list[dict[str, Any]] = []
        for uri in sorted(self._items):
            item = self._items[uri]
            path = Path(str(item.get("path") or ""))
            if not path.is_file():
                continue
            resources.append(_public_resource(item))
        return resources

    def read(self, uri: str) -> tuple[dict[str, Any], bytes]:
        item = self._items.get(uri)
        if item is None:
            raise ProtocolError(-32002, "resource is not registered")
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ProtocolError(-32002, "registered resource is invalid")
        try:
            path = Path(raw_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ProtocolError(-32002, "registered resource is unavailable") from exc
        if str(path) != raw_path or not path.is_file():
            raise ProtocolError(-32002, "registered resource path changed")
        size = path.stat().st_size
        if size != item.get("size") or size <= 0 or size > MAX_RESOURCE_BYTES:
            raise ProtocolError(-32002, "registered resource size changed")
        data = path.read_bytes()
        try:
            mime_type = _image_mime_type(data, path.suffix)
        except ValueError as exc:
            raise ProtocolError(-32002, "registered resource is no longer a valid image") from exc
        if hashlib.sha256(data).hexdigest() != item.get("sha256"):
            raise ProtocolError(-32002, "registered resource content changed")
        if mime_type != item.get("mimeType"):
            raise ProtocolError(-32002, "registered resource MIME type changed")
        return dict(item), data


def _public_resource(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: item[key]
        for key in ("uri", "name", "title", "description", "mimeType", "size")
        if key in item
    }


def _validated_request_key(value: Any) -> str:
    if value is None or value == "":
        return str(uuid.uuid4())
    if not isinstance(value, str) or REQUEST_KEY_RE.fullmatch(value) is None:
        raise ValueError("request_key must be 8-128 ASCII letters, digits, dots, underscores, or hyphens")
    return value


def _string_arg(arguments: Mapping[str, Any], key: str, *, required: bool = False) -> str | None:
    value = arguments.get(key)
    if value is None:
        if required:
            raise ValueError(f"{key} is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _safe_error(exc: Exception) -> dict[str, Any]:
    if hasattr(exc, "as_json") and callable(exc.as_json):
        value = exc.as_json()
        if isinstance(value, dict):
            error = dict(value)
        else:
            error = {"type": type(exc).__name__, "message": "request failed"}
    else:
        error = {"type": type(exc).__name__, "message": str(exc)}
    message = str(error.get("message") or "request failed")
    message = re.sub(r"(?i)\b(sk-[A-Za-z0-9._-]+|bearer\s+\S+)", "[REDACTED]", message)
    error["message"] = message[:1000]
    error.pop("traceback", None)
    return error


def _call_compatible(function: Any, values: Mapping[str, Any]) -> Any:
    signature = inspect.signature(function)
    kwargs = {
        name: values[name]
        for name in signature.parameters
        if name in values
    }
    return function(**kwargs)


class PodotionMCPServer:
    def __init__(
        self,
        core: ModuleType | Any | None = None,
        registry: ResourceRegistry | None = None,
    ) -> None:
        self.core = load_image_core() if core is None else core
        self.registry = ResourceRegistry() if registry is None else registry

    def initialize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": negotiated_protocol_version(params.get("protocolVersion")),
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            },
            "serverInfo": {"name": SERVER_NAME, "version": server_version()},
            "instructions": (
                "Use request_key and state_scope for generation status recovery. "
                "Generated image resources can be read only through their registered URI."
            ),
        }

    def tools(self) -> list[dict[str, Any]]:
        render_properties = {
            "prompt": {"type": "string", "minLength": 1},
            "output_dir": {"type": "string", "minLength": 1},
            "workspace_root": {
                "type": "string",
                "minLength": 1,
                "description": "Absolute active workspace used to resolve relative paths.",
            },
            "size": {"type": "string", "enum": ["1k", "2k", "4k"]},
            "ratio": {
                "type": "string",
                "enum": ["1:1", "2:3", "3:2", "3:4", "4:3", "16:9", "9:16"],
            },
            "request_key": {"type": "string", "minLength": 8, "maxLength": 128},
            "state_scope": {
                "type": "string",
                "minLength": 1,
                "description": "Stable task or conversation identifier used to isolate state.",
            },
            "force_new": {
                "type": "boolean",
                "description": "Create a new variant instead of reusing a recent equivalent success.",
            },
        }
        return [
            {
                "name": "generate",
                "description": "Generate one image through Podotion. The call may take several minutes.",
                "inputSchema": {
                    "type": "object",
                    "properties": render_properties,
                    "required": ["prompt", "output_dir", "request_key", "state_scope"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "edit",
                "description": "Edit explicit images or the last image in the same state scope.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        **render_properties,
                        "input_images": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                            "minItems": 1,
                            "maxItems": 5,
                        },
                        "use_last": {"type": "boolean"},
                    },
                    "required": ["prompt", "output_dir", "request_key", "state_scope"],
                    "oneOf": [
                        {"required": ["input_images"]},
                        {"required": ["use_last"]},
                    ],
                    "additionalProperties": False,
                },
            },
            {
                "name": "publish_existing_image",
                "description": "Publish an existing local image as an MCP image resource without generating a new image.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "minLength": 1},
                        "request_key": {"type": "string", "minLength": 8, "maxLength": 128},
                        "workspace_root": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Absolute active workspace used to resolve a relative path.",
                        },
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "doctor",
                "description": "Run the non-billable Podotion configuration and connectivity check.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "request_status",
                "description": "Read generation status without starting or retrying a request.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "request_key": {"type": "string", "minLength": 8, "maxLength": 128},
                        "output_dir": {"type": "string", "minLength": 1},
                        "workspace_root": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Absolute active workspace used to resolve a relative output directory.",
                        },
                        "state_scope": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Stable task or conversation identifier used to isolate state.",
                        },
                    },
                    "required": ["output_dir", "request_key", "state_scope"],
                    "additionalProperties": False,
                },
            },
        ]

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        try:
            if name == "generate":
                return self._generate(arguments, "generate")
            if name == "edit":
                return self._generate(arguments, "edit")
            if name == "publish_existing_image":
                return self._publish(arguments)
            if name == "doctor":
                return self._doctor(arguments)
            if name == "request_status":
                return self._request_status(arguments)
            raise ProtocolError(-32601, f"unknown tool: {name}")
        except ProtocolError:
            raise
        except Exception as exc:
            error = _safe_error(exc)
            return {
                "content": [{"type": "text", "text": json.dumps({"ok": False, "error": error}, ensure_ascii=False)}],
                "structuredContent": {"ok": False, "error": error},
                "isError": True,
            }

    def _generate(self, arguments: Mapping[str, Any], operation: str) -> dict[str, Any]:
        prompt = _string_arg(arguments, "prompt", required=True)
        output_dir = _string_arg(arguments, "output_dir", required=True)
        workspace_root = _string_arg(arguments, "workspace_root")
        request_key = _validated_request_key(arguments.get("request_key"))
        state_scope = _string_arg(arguments, "state_scope", required=True)
        resolved_output = resolve_workspace_path(
            output_dir,
            workspace=workspace_root,
        )
        input_images = arguments.get("input_images")
        use_last = arguments.get("use_last") is True
        raw_force_new = arguments.get("force_new", False)
        if not isinstance(raw_force_new, bool):
            raise ValueError("force_new must be a boolean")
        if operation == "edit":
            if input_images is not None:
                if (
                    not isinstance(input_images, list)
                    or not input_images
                    or len(input_images) > 5
                    or not all(isinstance(value, str) and value for value in input_images)
                ):
                    raise ValueError("input_images must contain one to five paths")
                input_images = [
                    resolve_workspace_path(value, workspace=workspace_root)
                    for value in input_images
                ]
            if bool(input_images) == use_last:
                raise ValueError("edit requires exactly one of input_images or use_last=true")
        args = argparse.Namespace(
            credential_file=None,
            prompt=prompt,
            prompt_file=None,
            size=arguments.get("size"),
            ratio=arguments.get("ratio"),
            output_dir=resolved_output,
            image=list(input_images or []),
            last=use_last,
            request_key=request_key,
            state_scope=state_scope,
            force_new=raw_force_new,
        )
        result = self.core.run_generation(args, operation)
        return self._image_result(result, request_key, state_scope, workspace_root)

    def _publish(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        path = _string_arg(arguments, "path", required=True)
        workspace_root = _string_arg(arguments, "workspace_root")
        request_key = _validated_request_key(arguments.get("request_key"))
        resolved_path = resolve_workspace_path(path, workspace=workspace_root)
        image = {
            "path": resolved_path,
        }
        return self._image_result(
            {"ok": True, "operation": "publish", "images": [image], "warnings": []},
            request_key,
            None,
            workspace_root,
        )

    def _doctor(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if arguments:
            raise ValueError("doctor does not accept arguments")
        args = argparse.Namespace(
            credential_file=None,
            image_probe=False,
        )
        result = self.core.run_doctor(args)
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "structuredContent": result,
            "isError": not bool(result.get("ok")),
        }

    def _request_status(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        request_key = _validated_request_key(arguments.get("request_key"))
        state_scope = _string_arg(arguments, "state_scope", required=True)
        output_dir = _string_arg(arguments, "output_dir", required=True)
        workspace_root = _string_arg(arguments, "workspace_root")
        resolved_output = resolve_workspace_path(
            output_dir,
            workspace=workspace_root,
        )
        function = getattr(self.core, "get_request_status", None)
        if not callable(function):
            raise RuntimeError("installed executor does not support request status")
        result = _call_compatible(
            function,
            {
                "request_key": request_key,
                "state_scope": state_scope,
                "scope": state_scope,
                "output_dir": resolved_output,
                "environ": {"CODEX_THREAD_ID": state_scope},
            },
        )
        if not isinstance(result, dict):
            result = {"ok": True, "request_key": request_key, "status": str(result)}
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "structuredContent": result,
            "isError": not bool(result.get("ok", True)),
        }

    def _image_result(
        self,
        raw_result: Mapping[str, Any],
        request_key: str,
        state_scope: str | None,
        workspace_root: str | None,
    ) -> dict[str, Any]:
        result = dict(raw_result)
        images = result.get("images")
        if not result.get("ok") or not isinstance(images, list) or not images:
            raise RuntimeError("executor did not return a successful image result")
        warnings: list[Any] = []
        raw_warnings = result.get("warnings", [])
        if isinstance(raw_warnings, list):
            warnings.extend(raw_warnings)
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "ok": True,
                        "operation": result.get("operation"),
                        "request_key": request_key,
                        "image_count": len(images),
                        "warnings": warnings,
                    },
                    ensure_ascii=False,
                ),
            }
        ]
        structured_images: list[dict[str, Any]] = []
        for index, value in enumerate(images):
            if not isinstance(value, dict) or not isinstance(value.get("path"), str):
                warnings.append(
                    {
                        "type": "invalid_image_entry",
                        "result_index": index,
                        "message": "executor returned an invalid image entry",
                    }
                )
                continue
            try:
                path, data, mime_type = _read_local_image(value["path"])
            except Exception as exc:
                error = _safe_error(exc)
                warnings.append(
                    {
                        "type": "resource_registration_failed",
                        "result_index": index,
                        "message": error.get(
                            "message", "resource registration failed"
                        ),
                    }
                )
                continue

            resource: dict[str, Any] | None = None
            try:
                candidate = self.registry.register(path, request_key)
                self.registry.read(candidate["uri"])
                resource = candidate
            except Exception as exc:
                # Outputs registration is post-generation work. The billed
                # image remains a successful result when the local file can
                # still be returned inline.
                error = _safe_error(exc)
                warnings.append(
                    {
                        "type": "resource_registration_failed",
                        "result_index": index,
                        "message": error.get(
                            "message", "resource registration failed"
                        ),
                    }
                )
            if resource is not None and resource.get("persistent") is False:
                warnings.append(
                    {
                        "type": "resource_registry_memory_fallback",
                        "result_index": index,
                        "message": (
                            "resource link is available in the active MCP process "
                            "but its registry could not be persisted"
                        ),
                    }
                )
            content.append(
                {
                    "type": "image",
                    "data": base64.b64encode(data).decode("ascii"),
                    "mimeType": mime_type,
                }
            )
            if resource is not None:
                content.append({"type": "resource_link", **_public_resource(resource)})
            structured = dict(value)
            try:
                described = describe_path(
                    path,
                    request_id=request_key,
                    workspace=workspace_root,
                )
            except Exception as exc:
                # A foreign path can be unrenderable by the current host (for
                # example a Windows path returned to a WSL process). The MCP
                # image remains usable, so retain it and expose only a
                # sanitized warning.
                error = _safe_error(exc)
                warnings.append(
                    {
                        "type": "path_description_failed",
                        "result_index": index,
                        "message": error.get("message", "path description failed"),
                    }
                )
                described = {
                    "path": str(path),
                    "markdown_path": str(path),
                    "file_uri": "",
                }
            structured.update(
                {
                    "filename": path.name,
                    "path": described["path"],
                    "markdown_path": described["markdown_path"],
                    "file_uri": described["file_uri"],
                    "mime_type": mime_type,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "outputs_registered": resource is not None,
                }
            )
            # describe_path also computes an opaque URI, but the public result
            # must advertise it only after the registry accepted the file.
            structured.pop("resource_uri", None)
            if resource is not None:
                structured["resource_uri"] = resource["uri"]
            structured_images.append(structured)
        if not structured_images:
            raise RuntimeError("executor returned no publishable image resources")
        # Update the already-created summary block now that post-processing
        # warnings and the count of publishable files are known.
        content[0]["text"] = json.dumps(
            {
                "ok": True,
                "operation": result.get("operation"),
                "request_key": request_key,
                "image_count": len(structured_images),
                "warnings": warnings,
            },
            ensure_ascii=False,
        )
        structured_result = dict(result)
        structured_result["request_key"] = request_key
        if state_scope is not None:
            structured_result["state_scope"] = state_scope
        structured_result["warnings"] = warnings
        structured_result["images"] = structured_images
        return {
            "content": content,
            "structuredContent": structured_result,
            "isError": False,
        }

    def dispatch(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return self.initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.tools()}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                raise ProtocolError(-32602, "tools/call requires a tool name and object arguments")
            return self.call_tool(name, arguments)
        if method == "resources/list":
            return {"resources": self.registry.list_resources()}
        if method == "resources/read":
            uri = params.get("uri")
            if not isinstance(uri, str) or not uri:
                raise ProtocolError(-32602, "resources/read requires a URI")
            item, data = self.registry.read(uri)
            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": item["mimeType"],
                        "blob": base64.b64encode(data).decode("ascii"),
                    }
                ]
            }
        raise ProtocolError(-32601, f"method not found: {method}")


def serve(
    server: PodotionMCPServer,
    input_stream: BinaryIO | None = None,
    output_stream: BinaryIO | None = None,
) -> None:
    source = sys.stdin.buffer if input_stream is None else input_stream
    destination = sys.stdout.buffer if output_stream is None else output_stream
    while True:
        request_id: Any = None
        try:
            message = read_message(source)
            if message is None:
                return
            request_id, method, params = request_parts(message)
            if request_id is None:
                continue
            result = server.dispatch(method, params)
            write_message(destination, result_response(request_id, result))
        except ProtocolError as exc:
            write_message(destination, error_response(request_id, exc))
        except Exception:
            write_message(
                destination,
                error_response(request_id, ProtocolError(-32603, "internal server error")),
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Podotion Image MCP server")
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="serve MCP over standard input and output",
    )
    parser.parse_args()
    serve(PodotionMCPServer())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

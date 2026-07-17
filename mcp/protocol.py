"""Minimal JSON-RPC and MCP stdio protocol helpers.

MCP stdio messages are UTF-8 JSON objects separated by newlines. This module
keeps framing and JSON-RPC validation independent from the Podotion tools so it
can be tested without importing the image executor.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, BinaryIO, Mapping


JSONRPC_VERSION = "2.0"
LATEST_PROTOCOL_VERSION = "2025-06-18"
# ResourceLink tool content is part of the 2025-06-18 contract used here.
# Do not claim older protocol versions whose content union lacks that block.
SUPPORTED_PROTOCOL_VERSIONS = frozenset({LATEST_PROTOCOL_VERSION})
MAX_MESSAGE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class ProtocolError(Exception):
    code: int
    message: str
    data: Any = None

    def __str__(self) -> str:
        return self.message


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    line = stream.readline(MAX_MESSAGE_BYTES + 1)
    if line == b"":
        return None
    if len(line) > MAX_MESSAGE_BYTES:
        raise ProtocolError(-32700, "MCP message exceeds the 4 MB limit")
    try:
        payload = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(-32700, "invalid JSON-RPC message") from exc
    if not isinstance(payload, dict):
        raise ProtocolError(-32600, "JSON-RPC message must be an object")
    return payload


def write_message(stream: BinaryIO, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        dict(payload), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    stream.write(encoded + b"\n")
    stream.flush()


def request_parts(message: Mapping[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    if message.get("jsonrpc") != JSONRPC_VERSION:
        raise ProtocolError(-32600, "unsupported JSON-RPC version")
    method = message.get("method")
    if not isinstance(method, str) or not method:
        raise ProtocolError(-32600, "JSON-RPC method must be a non-empty string")
    params = message.get("params", {})
    if not isinstance(params, dict):
        raise ProtocolError(-32602, "JSON-RPC params must be an object")
    return message.get("id"), method, params


def result_response(request_id: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": dict(result)}


def error_response(request_id: Any, error: ProtocolError) -> dict[str, Any]:
    value: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.data is not None:
        value["data"] = error.data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": value}


def negotiated_protocol_version(requested: Any) -> str:
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return LATEST_PROTOCOL_VERSION

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterable
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PLUGIN_ROOT / "skills" / "podotion-image"
SCRIPT_PATH = SKILL_ROOT / "scripts" / "podotion_image.py"

# A valid 1x1 RGBA PNG. Keeping the image tiny makes every test deterministic.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAF"
    "gAI/ScL+vgAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")


class FakeProviderServer:
    """A local provider that records Images requests and serves queued JSON."""

    def __init__(self, responses: Iterable[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *args: object) -> None:
                return

            def _write_json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                content_type = self.headers.get("Content-Type", "")
                parsed_body = (
                    json.loads(raw.decode("utf-8"))
                    if content_type.lower().startswith("application/json")
                    else None
                )
                owner.requests.append(
                    {
                        "method": "POST",
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "content_type": content_type,
                        "body": parsed_body,
                        "raw_body": raw,
                    }
                )
                if not owner._responses:
                    self._write_json(500, {"error": {"message": "no queued response"}})
                    return
                self._write_json(200, owner._responses.pop(0))

            def do_HEAD(self) -> None:
                owner.requests.append(
                    {
                        "method": "HEAD",
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                    }
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()

            def do_OPTIONS(self) -> None:
                owner.requests.append(
                    {
                        "method": "OPTIONS",
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                    }
                )
                self.send_response(204)
                self.end_headers()

            def do_GET(self) -> None:
                owner.requests.append(
                    {
                        "method": "GET",
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                    }
                )
                self._write_json(200, {"status": "ok"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> "FakeProviderServer":
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def images_response() -> dict[str, Any]:
    return {
        "created": 1710000000,
        "data": [{"b64_json": PNG_B64}],
    }


def parse_multipart_request(request: dict[str, Any]) -> list[dict[str, Any]]:
    content_type = str(request.get("content_type") or "")
    raw_body = request.get("raw_body")
    if not content_type.lower().startswith("multipart/form-data") or not isinstance(
        raw_body, bytes
    ):
        raise AssertionError("request is not multipart/form-data")
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii")
        + raw_body
    )
    return [
        {
            "name": part.get_param("name", header="content-disposition"),
            "filename": part.get_filename(),
            "content_type": part.get_content_type(),
            "data": part.get_payload(decode=True),
        }
        for part in message.iter_parts()
    ]


def run_cli(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        check=False,
        capture_output=True,
        text=True,
        env=merged_env,
        timeout=20,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"CLI failed with {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def parse_cli_json(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise AssertionError(f"expected a JSON object, got {type(payload).__name__}")
    return payload


def walk_values(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for nested in value.values():
            yield from walk_values(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from walk_values(nested)
    else:
        yield value

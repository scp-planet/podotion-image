from __future__ import annotations

import base64
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "mcp"))

import protocol  # noqa: E402
import server  # noqa: E402


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"test-image-bytes"


class FakeCore:
    def __init__(self, image_path: Path) -> None:
        self.image_path = image_path
        self.calls: list[tuple[str, object]] = []

    def run_generation(self, args: object, operation: str) -> dict[str, object]:
        self.calls.append((operation, args))
        return {
            "ok": True,
            "operation": operation,
            "model": "gpt-image-2",
            "request": {"size": "1024x1024"},
            "images": [
                {
                    "path": str(self.image_path),
                    "markdown_path": self.image_path.as_posix(),
                    "width": 1,
                    "height": 1,
                }
            ],
            "warnings": [],
        }

    def run_doctor(self, args: object) -> dict[str, object]:
        self.calls.append(("doctor", args))
        return {"ok": True, "transport": "images"}

    def get_request_status(
        self,
        output_dir: str,
        request_key: str,
        environ: dict[str, str] | None = None,
    ) -> dict[str, object]:
        self.calls.append(("request_status", (request_key, output_dir, environ)))
        return {
            "ok": True,
            "request_key": request_key,
            "state_scope": (environ or {}).get("CODEX_THREAD_ID"),
            "status": "completed",
        }


class MultiImageCore(FakeCore):
    def __init__(self, image_path: Path, invalid_path: Path) -> None:
        super().__init__(image_path)
        self.invalid_path = invalid_path

    def run_generation(self, args: object, operation: str) -> dict[str, object]:
        result = super().run_generation(args, operation)
        result["images"] = [
            {"path": str(self.image_path), "width": 1, "height": 1},
            {"path": str(self.invalid_path), "width": 1, "height": 1},
        ]
        return result


class MCPServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.image_path = self.root / "image.png"
        self.image_path.write_bytes(PNG_BYTES)
        self.core = FakeCore(self.image_path)
        self.registry = server.ResourceRegistry(self.root / "registry.json")
        self.server = server.PodotionMCPServer(self.core, self.registry)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_manifest_uses_one_hour_tool_timeout(self) -> None:
        manifest = json.loads((PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8"))
        config = manifest["mcpServers"]["podotion-image"]
        self.assertEqual(config["tool_timeout_sec"], 3600)
        self.assertEqual(config["args"][-1], "--stdio")
        self.assertEqual(config["env_vars"], ["CODEX_HOME"])

    def test_server_version_is_read_from_plugin_manifest(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        initialized = self.server.initialize({"protocolVersion": "2025-06-18"})
        self.assertEqual(initialized["serverInfo"]["version"], manifest["version"])

    def test_registry_path_uses_runtime_home_not_shared_codex_home(self) -> None:
        path = server.default_registry_path(
            {
                "HOME": "/home/ada",
                "USERPROFILE": r"C:\Users\Ada",
                "CODEX_HOME": "/mnt/c/Users/Ada/.codex",
                "WSL_DISTRO_NAME": "Ubuntu",
            }
        )
        self.assertEqual(
            path,
            Path("/home/ada/.codex/podotion-image/mcp-resources.json"),
        )

    def test_tool_list_has_all_public_tools(self) -> None:
        names = {tool["name"] for tool in self.server.tools()}
        self.assertEqual(
            names,
            {
                "generate",
                "edit",
                "publish_existing_image",
                "doctor",
                "request_status",
            },
        )

    def test_generate_returns_image_link_and_structured_content(self) -> None:
        result = self.server.call_tool(
            "generate",
            {
                "prompt": "a test image",
                "output_dir": "renders",
                "workspace_root": str(self.root),
                "request_key": "request-1",
                "state_scope": "thread-generate",
                "force_new": True,
            },
        )

        self.assertFalse(result["isError"])
        self.assertEqual(
            [item["type"] for item in result["content"]],
            ["text", "image", "resource_link"],
        )
        image_content = result["content"][1]
        self.assertEqual(base64.b64decode(image_content["data"]), PNG_BYTES)
        self.assertEqual(image_content["mimeType"], "image/png")
        resource_link = result["content"][2]
        self.assertEqual(resource_link["mimeType"], "image/png")
        self.assertEqual(resource_link["size"], len(PNG_BYTES))
        structured = result["structuredContent"]
        self.assertEqual(structured["request_key"], "request-1")
        self.assertEqual(
            structured["images"][0]["resource_uri"], resource_link["uri"]
        )
        self.assertEqual(
            structured["images"][0]["file_uri"], self.image_path.as_uri()
        )
        self.assertTrue(structured["images"][0]["outputs_registered"])

        operation, args = self.core.calls[0]
        self.assertEqual(operation, "generate")
        self.assertEqual(args.request_key, "request-1")
        self.assertEqual(args.output_dir, str(self.root / "renders"))
        self.assertEqual(args.state_scope, "thread-generate")
        self.assertTrue(args.force_new)

    def test_registered_resource_can_be_listed_and_read(self) -> None:
        generated = self.server.call_tool(
            "generate",
            {
                "prompt": "test",
                "output_dir": str(self.root),
                "request_key": "request-2",
                "state_scope": "thread-list",
            },
        )
        uri = generated["structuredContent"]["images"][0]["resource_uri"]

        listed = self.server.dispatch("resources/list", {})
        self.assertEqual([item["uri"] for item in listed["resources"]], [uri])
        read = self.server.dispatch("resources/read", {"uri": uri})
        self.assertEqual(base64.b64decode(read["contents"][0]["blob"]), PNG_BYTES)
        self.assertEqual(read["contents"][0]["mimeType"], "image/png")

    def test_publish_failure_does_not_hide_successful_image(self) -> None:
        invalid = self.root / "missing.png"
        core = MultiImageCore(self.image_path, invalid)
        isolated = server.PodotionMCPServer(
            core, server.ResourceRegistry(self.root / "mixed-registry.json")
        )
        result = isolated.call_tool(
            "generate",
            {
                "prompt": "test",
                "output_dir": str(self.root),
                "request_key": "mixed-results-1",
                "state_scope": "thread-mixed-results",
            },
        )
        self.assertFalse(result["isError"])
        self.assertEqual(
            [item["type"] for item in result["content"]],
            ["text", "image", "resource_link"],
        )
        warnings = result["structuredContent"]["warnings"]
        self.assertEqual(warnings[0]["type"], "resource_registration_failed")
        self.assertEqual(warnings[0]["result_index"], 1)

    def test_registry_persistence_failure_keeps_image_and_resource_link(self) -> None:
        with mock.patch.object(
            self.registry,
            "_save",
            side_effect=PermissionError("mounted registry is temporarily read-only"),
        ):
            result = self.server.call_tool(
                "publish_existing_image",
                {
                    "path": str(self.image_path),
                    "request_key": "memory-resource-1",
                },
            )

        self.assertFalse(result["isError"])
        self.assertEqual(
            [item["type"] for item in result["content"]],
            ["text", "image", "resource_link"],
        )
        warnings = result["structuredContent"]["warnings"]
        self.assertEqual(warnings[0]["type"], "resource_registry_memory_fallback")
        uri = result["structuredContent"]["images"][0]["resource_uri"]
        read = self.server.dispatch("resources/read", {"uri": uri})
        self.assertEqual(base64.b64decode(read["contents"][0]["blob"]), PNG_BYTES)
        self.assertEqual(len(result["structuredContent"]["images"]), 1)
        self.assertTrue(
            result["structuredContent"]["images"][0]["outputs_registered"]
        )

    def test_registry_failure_keeps_inline_image_and_saved_path(self) -> None:
        with mock.patch.object(
            self.registry,
            "register",
            side_effect=PermissionError("registry unavailable"),
        ):
            result = self.server.call_tool(
                "publish_existing_image",
                {
                    "path": str(self.image_path),
                    "request_key": "inline-fallback-1",
                },
            )

        self.assertFalse(result["isError"])
        self.assertEqual(
            [item["type"] for item in result["content"]],
            ["text", "image"],
        )
        self.assertEqual(base64.b64decode(result["content"][1]["data"]), PNG_BYTES)
        image = result["structuredContent"]["images"][0]
        self.assertEqual(image["path"], str(self.image_path))
        self.assertFalse(image["outputs_registered"])
        self.assertNotIn("resource_uri", image)
        self.assertEqual(
            result["structuredContent"]["warnings"][0]["type"],
            "resource_registration_failed",
        )

    def test_path_description_failure_keeps_mcp_image_and_resource(self) -> None:
        original = server.describe_path

        def fail_path(*args, **kwargs):
            raise ValueError("foreign path")

        server.describe_path = fail_path
        try:
            result = self.server.call_tool(
                "generate",
                {
                    "prompt": "test",
                    "output_dir": str(self.root),
                    "request_key": "path-warning-1",
                    "state_scope": "thread-path-warning",
                },
            )
        finally:
            server.describe_path = original
        self.assertFalse(result["isError"])
        self.assertEqual(
            [item["type"] for item in result["content"]],
            ["text", "image", "resource_link"],
        )
        self.assertEqual(
            result["structuredContent"]["warnings"][0]["type"],
            "path_description_failed",
        )
        image = result["structuredContent"]["images"][0]
        self.assertEqual(image["file_uri"], "")

    def test_unregistered_or_changed_resource_is_rejected(self) -> None:
        with self.assertRaises(protocol.ProtocolError):
            self.server.dispatch(
                "resources/read", {"uri": "podotion-image://outputs/x/private.png"}
            )

        generated = self.server.call_tool(
            "generate",
            {
                "prompt": "test",
                "output_dir": str(self.root),
                "request_key": "request-3",
                "state_scope": "thread-change",
            },
        )
        uri = generated["structuredContent"]["images"][0]["resource_uri"]
        self.image_path.write_bytes(PNG_BYTES + b"changed")
        with self.assertRaises(protocol.ProtocolError):
            self.server.dispatch("resources/read", {"uri": uri})

    def test_publish_existing_image_never_calls_core(self) -> None:
        result = self.server.call_tool(
            "publish_existing_image",
            {
                "path": "image.png",
                "workspace_root": str(self.root),
                "request_key": "publish-1",
            },
        )
        self.assertFalse(result["isError"])
        self.assertEqual(self.core.calls, [])
        self.assertEqual(result["structuredContent"]["operation"], "publish")

    def test_edit_requires_exactly_one_input_mode(self) -> None:
        invalid = self.server.call_tool(
            "edit",
            {
                "prompt": "edit",
                "output_dir": str(self.root),
                "request_key": "edit-invalid-1",
                "state_scope": "thread-edit-invalid",
            },
        )
        self.assertTrue(invalid["isError"])

        valid = self.server.call_tool(
            "edit",
            {
                "prompt": "edit",
                "use_last": True,
                "request_key": "edit-valid-2",
                "output_dir": str(self.root / "PodotionImage"),
                "state_scope": "thread-edit-valid",
                "force_new": True,
            },
        )
        self.assertFalse(valid["isError"])
        operation, args = self.core.calls[0]
        self.assertEqual(operation, "edit")
        self.assertTrue(args.last)
        self.assertEqual(args.state_scope, "thread-edit-valid")
        self.assertTrue(args.force_new)

    def test_edit_input_paths_are_resolved_from_workspace(self) -> None:
        result = self.server.call_tool(
            "edit",
            {
                "prompt": "edit",
                "output_dir": "renders",
                "input_images": ["image.png"],
                "workspace_root": str(self.root),
                "request_key": "edit-paths-1",
                "state_scope": "thread-edit-paths",
            },
        )
        self.assertFalse(result["isError"])
        _, args = self.core.calls[0]
        self.assertEqual(args.output_dir, str(self.root / "renders"))
        self.assertEqual(args.image, [str(self.image_path)])

    def test_doctor_is_non_billable(self) -> None:
        result = self.server.call_tool("doctor", {})
        self.assertFalse(result["isError"])
        _, args = self.core.calls[0]
        self.assertFalse(args.image_probe)

    def test_request_status_is_read_only(self) -> None:
        with mock.patch.dict(
            server.os.environ,
            {
                "CODEX_HOME": "/mnt/c/Users/Ada/.codex",
                "PODOTION_RUNTIME_TEST": "preserved",
            },
            clear=True,
        ):
            result = self.server.call_tool(
                "request_status",
                {
                    "output_dir": "renders",
                    "workspace_root": str(self.root),
                    "request_key": "status-1",
                    "state_scope": "thread-status",
                },
            )
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["status"], "completed")
        self.assertEqual(
            self.core.calls,
            [
                (
                    "request_status",
                    (
                        "status-1",
                        str(self.root / "renders"),
                        {
                            "CODEX_HOME": "/mnt/c/Users/Ada/.codex",
                            "PODOTION_RUNTIME_TEST": "preserved",
                            "CODEX_THREAD_ID": "thread-status",
                        },
                    ),
                )
            ],
        )

    def test_generate_schema_requires_explicit_scope_and_output(self) -> None:
        generate = next(tool for tool in self.server.tools() if tool["name"] == "generate")
        schema = generate["inputSchema"]
        self.assertEqual(
            schema["required"],
            ["prompt", "output_dir", "request_key", "state_scope"],
        )
        self.assertEqual(schema["properties"]["force_new"]["type"], "boolean")

    def test_stdio_server_handles_initialize_and_tools_list(self) -> None:
        source = io.BytesIO(
            b"\n".join(
                [
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18"},
                        }
                    ).encode(),
                    json.dumps(
                        {"jsonrpc": "2.0", "method": "notifications/initialized"}
                    ).encode(),
                    json.dumps(
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
                    ).encode(),
                    b"",
                ]
            )
        )
        destination = io.BytesIO()

        server.serve(self.server, source, destination)
        messages = [json.loads(line) for line in destination.getvalue().splitlines()]
        self.assertEqual(messages[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(messages[1]["id"], 2)
        self.assertEqual(len(messages[1]["result"]["tools"]), 5)


if __name__ == "__main__":
    unittest.main()

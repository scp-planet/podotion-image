from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "mcp"))

import protocol  # noqa: E402


class ProtocolTests(unittest.TestCase):
    def test_round_trip_message(self) -> None:
        stream = io.BytesIO()
        protocol.write_message(
            stream,
            {"jsonrpc": "2.0", "id": 3, "result": {"text": "图片"}},
        )
        stream.seek(0)

        self.assertEqual(
            protocol.read_message(stream),
            {"jsonrpc": "2.0", "id": 3, "result": {"text": "图片"}},
        )

    def test_invalid_json_is_protocol_error(self) -> None:
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.read_message(io.BytesIO(b"not-json\n"))
        self.assertEqual(caught.exception.code, -32700)

    def test_negotiates_resource_link_protocol_and_rejects_older_contracts(self) -> None:
        self.assertEqual(
            protocol.negotiated_protocol_version("2025-06-18"), "2025-06-18"
        )
        self.assertEqual(
            protocol.negotiated_protocol_version("2024-11-05"),
            protocol.LATEST_PROTOCOL_VERSION,
        )

    def test_request_validation(self) -> None:
        request_id, method, params = protocol.request_parts(
            {"jsonrpc": "2.0", "id": "a", "method": "ping"}
        )
        self.assertEqual((request_id, method, params), ("a", "ping", {}))

        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.request_parts(
                {"jsonrpc": "1.0", "id": 1, "method": "ping"}
            )
        self.assertEqual(caught.exception.code, -32600)


if __name__ == "__main__":
    unittest.main()

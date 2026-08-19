"""Tests for mcpforge — schema generation, protocol handling, transports,
and end-to-end integration through NORA's own MCP client (nora.mcp_bridge).

Stdlib unittest only — run with:  python -m unittest tests.test_mcpforge -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Literal

from mcpforge import LATEST_VERSION, MCPServer, build_input_schema

_ROOT = Path(__file__).resolve().parent.parent
_NOTES_SERVER = _ROOT / "mcp_servers" / "notes_server.py"


class TestSchemaGeneration(unittest.TestCase):
    def test_basic_types_and_required(self):
        def fn(a: str, b: int, c: float, d: bool) -> str:
            """Do a thing."""
        desc, schema = build_input_schema(fn)
        self.assertEqual(desc, "Do a thing.")
        self.assertEqual(schema["properties"]["a"], {"type": "string"})
        self.assertEqual(schema["properties"]["b"], {"type": "integer"})
        self.assertEqual(schema["properties"]["c"], {"type": "number"})
        self.assertEqual(schema["properties"]["d"], {"type": "boolean"})
        self.assertEqual(schema["required"], ["a", "b", "c", "d"])

    def test_defaults_are_optional_and_recorded(self):
        def fn(a: str, n: int = 3) -> str: ...
        _, schema = build_input_schema(fn)
        self.assertEqual(schema["required"], ["a"])
        self.assertEqual(schema["properties"]["n"]["default"], 3)

    def test_optional_union_and_list(self):
        def fn(tags: list[str] | None = None) -> str: ...
        _, schema = build_input_schema(fn)
        self.assertEqual(schema["properties"]["tags"]["type"], "array")
        self.assertEqual(schema["properties"]["tags"]["items"], {"type": "string"})
        self.assertNotIn("required", schema)

    def test_literal_becomes_enum(self):
        def fn(mode: Literal["fast", "slow"]) -> str: ...
        _, schema = build_input_schema(fn)
        self.assertEqual(schema["properties"]["mode"]["enum"], ["fast", "slow"])

    def test_docstring_param_descriptions(self):
        def fn(city: str, days: int = 1) -> str:
            """Get the forecast.

            city: name of the city
            days: days ahead
            """
        desc, schema = build_input_schema(fn)
        self.assertEqual(desc, "Get the forecast.")
        self.assertEqual(schema["properties"]["city"]["description"], "name of the city")
        self.assertEqual(schema["properties"]["days"]["description"], "days ahead")

    def test_unannotated_falls_back_to_string(self):
        def fn(x) -> str: ...
        _, schema = build_input_schema(fn)
        self.assertEqual(schema["properties"]["x"]["type"], "string")


class TestProtocol(unittest.TestCase):
    def setUp(self):
        self.server = MCPServer("t", version="9")

        @self.server.tool()
        def echo(text: str) -> str:
            """Echo the text back."""
            return text

        @self.server.tool()
        async def async_add(a: int, b: int) -> str:
            """Add two numbers asynchronously."""
            return str(a + b)

        @self.server.tool()
        def boom() -> str:
            """Always raises."""
            raise RuntimeError("kaboom")

    def _req(self, method, params=None, id=1):
        return self.server.handle({"jsonrpc": "2.0", "id": id, "method": method,
                                   "params": params or {}})

    def test_initialize_echoes_known_version(self):
        resp = self._req("initialize", {"protocolVersion": "2024-11-05"})
        self.assertEqual(resp["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(resp["result"]["serverInfo"]["name"], "t")

    def test_initialize_unknown_version_returns_latest(self):
        resp = self._req("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(resp["result"]["protocolVersion"], LATEST_VERSION)

    def test_notification_gets_no_response(self):
        self.assertIsNone(self.server.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools_list(self):
        resp = self._req("tools/list", id=2)
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertEqual(sorted(names), ["async_add", "boom", "echo"])

    def test_call_sync_tool(self):
        resp = self._req("tools/call", {"name": "echo", "arguments": {"text": "hi"}})
        self.assertFalse(resp["result"]["isError"])
        self.assertEqual(resp["result"]["content"][0]["text"], "hi")

    def test_call_async_tool(self):
        resp = self._req("tools/call", {"name": "async_add", "arguments": {"a": 2, "b": 3}})
        self.assertEqual(resp["result"]["content"][0]["text"], "5")

    def test_tool_exception_is_isError_result_not_protocol_error(self):
        resp = self._req("tools/call", {"name": "boom", "arguments": {}})
        self.assertNotIn("error", resp)
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("kaboom", resp["result"]["content"][0]["text"])

    def test_missing_required_argument(self):
        resp = self._req("tools/call", {"name": "echo", "arguments": {}})
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("text", resp["result"]["content"][0]["text"])

    def test_unknown_tool_and_method(self):
        self.assertEqual(self._req("tools/call", {"name": "nope"})["error"]["code"], -32602)
        self.assertEqual(self._req("no/such")["error"]["code"], -32601)

    def test_ping(self):
        self.assertEqual(self._req("ping")["result"], {})


class TestStdioEndToEnd(unittest.TestCase):
    """Spawn the example notes server as a real subprocess and speak JSON-RPC."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        env = {**os.environ, "NOTES_FILE": str(Path(self._tmp.name) / "notes.json")}
        self.proc = subprocess.Popen(
            [sys.executable, str(_NOTES_SERVER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env,
        )

    def tearDown(self):
        self.proc.stdin.close()
        self.proc.stdout.close()
        self.proc.terminate()
        self.proc.wait(timeout=5)
        self._tmp.cleanup()

    def _rpc(self, method, params=None, id=1):
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def test_full_session(self):
        init = self._rpc("initialize", {"protocolVersion": "2025-11-25",
                                        "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-11-25")

        tools = self._rpc("tools/list", id=2)["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["add_note", "list_notes", "search_notes"])

        add = self._rpc("tools/call", {"name": "add_note",
                                       "arguments": {"text": "hello world", "tags": ["test"]}}, id=3)
        self.assertFalse(add["result"]["isError"])

        found = self._rpc("tools/call", {"name": "search_notes",
                                         "arguments": {"query": "hello"}}, id=4)
        self.assertIn("hello world", found["result"]["content"][0]["text"])

    def test_garbage_line_returns_parse_error_and_survives(self):
        self.proc.stdin.write("this is not json\n")
        self.proc.stdin.flush()
        err = json.loads(self.proc.stdout.readline())
        self.assertEqual(err["error"]["code"], -32700)
        # server must still be alive and functional
        self.assertIn("result", self._rpc("ping", id=9))


class TestNoraBridgeIntegration(unittest.TestCase):
    """The real proof: NORA's own MCP client talks to a mcpforge server."""

    def test_bridge_start_list_call(self):
        from nora.mcp_bridge import _MCPStdioServer

        with tempfile.TemporaryDirectory() as tmp:
            server = _MCPStdioServer(
                "notes",
                [sys.executable, str(_NOTES_SERVER)],
                env={"NOTES_FILE": str(Path(tmp) / "notes.json")},
            )
            try:
                self.assertTrue(server.start())
                tools = server.list_tools()
                self.assertIn("add_note", [t["name"] for t in tools])
                # inputSchema survives the trip in the shape NORA's registrar expects
                add = next(t for t in tools if t["name"] == "add_note")
                self.assertEqual(add["inputSchema"]["properties"]["text"]["type"], "string")

                reply = server.call_tool("add_note", {"text": "bridge works"})
                self.assertIn("bridge works", reply)
                reply = server.call_tool("list_notes", {})
                self.assertIn("bridge works", reply)
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main()

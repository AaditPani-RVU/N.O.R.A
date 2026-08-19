"""mcpforge — turn plain Python functions into an MCP server.

Zero dependencies (stdlib only). Decorate functions with @server.tool(),
call server.run(), and any MCP client — NORA's mcp_bridge, Claude Code,
Claude Desktop — can discover and call them. The JSON Schema for each
tool is generated from the function's type hints, defaults, and docstring,
so the only thing you write is the function itself.

    from mcpforge import MCPServer

    server = MCPServer("weather")

    @server.tool()
    def get_forecast(city: str, days: int = 1) -> str:
        \"\"\"Get the weather forecast for a city.

        city: name of the city
        days: how many days ahead (default 1)
        \"\"\"
        ...

    if __name__ == "__main__":
        server.run()          # stdio (newline-delimited JSON-RPC)
        # or: server.serve_http(8080, token="secret")

Protocol notes:
  - Implements the classic initialize / tools/list / tools/call handshake,
    valid for every published MCP spec through 2025-11-25. The requested
    protocolVersion is echoed back when recognised so old and new clients
    both connect.
  - stdout carries only JSON-RPC; all logging goes to stderr, as the
    stdio transport requires.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import sys
import types
import typing
from typing import Any, Callable

logger = logging.getLogger("mcpforge")

SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
LATEST_VERSION = SUPPORTED_VERSIONS[-1]

_PY_TO_JSON: dict[type, dict[str, Any]] = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
    list: {"type": "array"},
    dict: {"type": "object"},
}


def _annotation_schema(annotation: Any) -> dict[str, Any]:
    """Map a Python type annotation to a JSON Schema fragment."""
    if annotation is inspect.Parameter.empty or annotation is None or annotation is Any:
        return {"type": "string"}

    origin = typing.get_origin(annotation)

    if origin in (typing.Union, types.UnionType):
        non_none = [a for a in typing.get_args(annotation) if a is not type(None)]
        return _annotation_schema(non_none[0]) if non_none else {"type": "string"}

    if origin is typing.Literal:
        values = list(typing.get_args(annotation))
        base = _PY_TO_JSON.get(type(values[0]), {"type": "string"})
        return {**base, "enum": values}

    if origin in (list, tuple, set, frozenset):
        args = typing.get_args(annotation)
        items = _annotation_schema(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": items}

    if origin is dict:
        return {"type": "object"}

    return dict(_PY_TO_JSON.get(annotation, {"type": "string"}))


def _docstring_parts(fn: Callable, param_names: list[str]) -> tuple[str, dict[str, str]]:
    """Split a docstring into (tool description, per-parameter descriptions).

    Parameter lines are recognised as "name: text" anywhere in the docstring,
    which covers both bare lists and Google-style Args: sections.
    """
    doc = inspect.getdoc(fn) or ""
    if not doc:
        return "", {}
    param_desc: dict[str, str] = {}
    desc_lines: list[str] = []
    for line in doc.splitlines():
        m = re.match(r"^\s*(\w+)\s*[:—-]\s+(.+)$", line)
        if m and m.group(1) in param_names:
            param_desc[m.group(1)] = m.group(2).strip()
        elif not param_desc and line.strip().lower() not in ("args:", "arguments:", "params:"):
            desc_lines.append(line)
    description = " ".join(l.strip() for l in desc_lines if l.strip()).strip()
    return description, param_desc


def build_input_schema(fn: Callable) -> tuple[str, dict[str, Any]]:
    """Return (description, JSON Schema) for a tool function's parameters."""
    sig = inspect.signature(fn)
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = {}

    param_names = [
        n for n, p in sig.parameters.items()
        if p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]
    description, param_desc = _docstring_parts(fn, param_names)

    properties: dict[str, Any] = {}
    required: list[str] = []
    for name in param_names:
        param = sig.parameters[name]
        schema = _annotation_schema(hints.get(name, param.annotation))
        if name in param_desc:
            schema["description"] = param_desc[name]
        if param.default is inspect.Parameter.empty:
            required.append(name)
        elif param.default is not None:
            schema["default"] = param.default
        properties[name] = schema

    input_schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        input_schema["required"] = required
    return description, input_schema


class Tool:
    def __init__(self, name: str, fn: Callable, description: str, input_schema: dict) -> None:
        self.name = name
        self.fn = fn
        self.description = description
        self.input_schema = input_schema

    def spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


class MCPServer:
    """A minimal, spec-compliant MCP tool server."""

    def __init__(self, name: str, version: str = "1.0", instructions: str = "") -> None:
        self.name = name
        self.version = version
        self.instructions = instructions
        self._tools: dict[str, Tool] = {}

    # ── Registration ─────────────────────────────────────────────────────

    def tool(self, name: str | None = None, description: str | None = None) -> Callable:
        """Register a function as an MCP tool. Sync and async both work."""
        def _decorator(fn: Callable) -> Callable:
            tool_name = name or fn.__name__
            doc_desc, schema = build_input_schema(fn)
            self._tools[tool_name] = Tool(tool_name, fn, description or doc_desc or tool_name, schema)
            return fn
        return _decorator

    # ── Protocol handling ────────────────────────────────────────────────

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """Handle one JSON-RPC message. Returns the response, or None for notifications."""
        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if msg_id is None:  # notification — never respond
            return None

        if method == "initialize":
            requested = params.get("protocolVersion", LATEST_VERSION)
            return self._result(msg_id, {
                "protocolVersion": requested if requested in SUPPORTED_VERSIONS else LATEST_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": self.version},
                **({"instructions": self.instructions} if self.instructions else {}),
            })
        if method == "ping":
            return self._result(msg_id, {})
        if method == "tools/list":
            return self._result(msg_id, {"tools": [t.spec() for t in self._tools.values()]})
        if method == "tools/call":
            return self._call_tool(msg_id, params)
        return self._error(msg_id, -32601, f"Method not found: {method}")

    def _call_tool(self, msg_id: Any, params: dict) -> dict[str, Any]:
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        tool = self._tools.get(name)
        if tool is None:
            return self._error(msg_id, -32602, f"Unknown tool: {name}")

        missing = [
            r for r in tool.input_schema.get("required", []) if r not in arguments
        ]
        if missing:
            return self._tool_error(msg_id, f"Missing required argument(s): {', '.join(missing)}")

        try:
            result = tool.fn(**arguments)
            if inspect.iscoroutine(result):
                result = asyncio.run(result)
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
            return self._result(msg_id, {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            })
        except TypeError as e:
            return self._tool_error(msg_id, f"Bad arguments for {name}: {e}")
        except Exception as e:
            logger.exception("tool %s failed", name)
            return self._tool_error(msg_id, f"{type(e).__name__}: {e}")

    @staticmethod
    def _result(msg_id: Any, result: dict) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    @staticmethod
    def _tool_error(msg_id: Any, message: str) -> dict[str, Any]:
        # Tool failures are results with isError, not protocol errors (per spec)
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "content": [{"type": "text", "text": message}], "isError": True,
        }}

    # ── Transports ───────────────────────────────────────────────────────

    def run(self) -> None:
        """Serve over stdio: newline-delimited JSON-RPC, logs on stderr."""
        logging.basicConfig(stream=sys.stderr, level=logging.INFO)
        logger.info("mcpforge server %r listening on stdio (%d tools)", self.name, len(self._tools))
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self._write({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "Parse error"}})
                continue
            resp = self.handle(msg) if isinstance(msg, dict) else None
            if resp is not None:
                self._write(resp)

    @staticmethod
    def _write(obj: dict) -> None:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def serve_http(self, port: int, host: str = "127.0.0.1", token: str = "") -> None:
        """Serve over HTTP POST (path / or /messages), optional Bearer token."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        server_self = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                if self.path.rstrip("/") not in ("", "/messages"):
                    self.send_error(404)
                    return
                if token and self.headers.get("Authorization", "") != f"Bearer {token}":
                    self.send_error(401)
                    return
                length = int(self.headers.get("Content-Length", 0))
                try:
                    msg = json.loads(self.rfile.read(length))
                except json.JSONDecodeError:
                    self.send_error(400)
                    return
                resp = server_self.handle(msg) if isinstance(msg, dict) else None
                body = json.dumps(resp or {}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args: Any) -> None:
                logger.debug("http: " + fmt, *args)

        logging.basicConfig(stream=sys.stderr, level=logging.INFO)
        logger.info("mcpforge server %r listening on http://%s:%d (%d tools)",
                    self.name, host, port, len(self._tools))
        ThreadingHTTPServer((host, port), _Handler).serve_forever()

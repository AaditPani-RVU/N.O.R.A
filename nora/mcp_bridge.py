"""MCP client bridge — connects NORA to Model Context Protocol tool servers.

Configured servers live under ``mcp_servers:`` in config.yaml.  Each tool
that a server advertises is registered into NORA's command registry as
``mcp_<alias>_<tool_name>``, making it callable by the LLM like any built-in.

Transports
----------
stdio   Server is a child process.  JSON-RPC 2.0 messages are exchanged
        over stdin/stdout, one message per line (newline-delimited JSON).
http    Server is a remote endpoint.  JSON-RPC 2.0 over HTTP POST.

config.yaml example::

    mcp_servers:
      - name: playwright
        transport: stdio
        command: ["npx", "@playwright/mcp@latest"]
      - name: my_server
        transport: http
        url: "http://localhost:8080/mcp"
        token: "optional-bearer-token"
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from typing import Any

from nora.command_engine import CommandMeta
from nora.command_engine import _meta as _cmd_meta
from nora.command_engine import _registry as _cmd_registry
from nora.config import get_config

logger = logging.getLogger("nora.mcp_bridge")

_servers: dict[str, "_MCPServer"] = {}


# ---------------------------------------------------------------------------
# Base + transport implementations
# ---------------------------------------------------------------------------

class _MCPServer:
    def __init__(self, name: str) -> None:
        self.name = name
        self._id = 0
        self._lock = threading.Lock()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def start(self) -> bool:  # pragma: no cover
        raise NotImplementedError

    def stop(self) -> None:
        pass

    def list_tools(self) -> list[dict]:  # pragma: no cover
        raise NotImplementedError

    def call_tool(self, tool_name: str, arguments: dict) -> str:  # pragma: no cover
        raise NotImplementedError


class _MCPStdioServer(_MCPServer):
    """JSON-RPC MCP server over child-process stdin/stdout."""

    def __init__(self, name: str, cmd: list[str], env: dict | None = None) -> None:
        super().__init__(name)
        self._cmd = cmd
        self._env = env or {}
        self._proc: subprocess.Popen | None = None

    def start(self) -> bool:
        try:
            merged = {**os.environ, **self._env}
            self._proc = subprocess.Popen(
                self._cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=merged,
            )
            return self._initialize()
        except FileNotFoundError:
            logger.error("MCP server %s: command not found — %s", self.name, self._cmd[0])
            return False
        except Exception as exc:
            logger.error("MCP server %s failed to start: %s", self.name, exc)
            return False

    def stop(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None

    def _send(self, method: str, params: dict | None = None) -> dict | None:
        if not self._proc or self._proc.poll() is not None:
            return None
        msg_id = self._next_id()
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            msg["params"] = params
        line = json.dumps(msg) + "\n"
        with self._lock:
            try:
                self._proc.stdin.write(line)  # type: ignore[union-attr]
                self._proc.stdin.flush()       # type: ignore[union-attr]
                for _ in range(20):
                    raw = self._proc.stdout.readline()  # type: ignore[union-attr]
                    if not raw:
                        return None
                    try:
                        resp = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if resp.get("id") == msg_id:
                        return resp
            except Exception as exc:
                logger.error("MCP %s I/O error: %s", self.name, exc)
        return None

    def _notify(self, method: str) -> None:
        if not self._proc or self._proc.poll() is not None:
            return
        notif = json.dumps({"jsonrpc": "2.0", "method": method}) + "\n"
        try:
            self._proc.stdin.write(notif)  # type: ignore[union-attr]
            self._proc.stdin.flush()        # type: ignore[union-attr]
        except Exception:
            pass

    def _initialize(self) -> bool:
        resp = self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "NORA", "version": "1.0"},
        })
        if not resp or "error" in resp:
            logger.error("MCP %s initialize failed: %s", self.name, resp)
            return False
        self._notify("notifications/initialized")
        return True

    def list_tools(self) -> list[dict]:
        resp = self._send("tools/list", {})
        if not resp or "error" in resp:
            return []
        return resp.get("result", {}).get("tools", [])

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        resp = self._send("tools/call", {"name": tool_name, "arguments": arguments})
        if not resp:
            return f"MCP server {self.name!r} is not responding."
        if "error" in resp:
            err = resp["error"]
            return f"MCP tool error: {err.get('message', str(err))}"
        return _extract_content(resp.get("result", {}))


class _MCPHttpServer(_MCPServer):
    """JSON-RPC MCP server over HTTP POST."""

    def __init__(self, name: str, url: str, token: str = "") -> None:
        super().__init__(name)
        self._url = url.rstrip("/")
        self._token = token
        self._tools_cache: list[dict] = []

    def start(self) -> bool:
        return self._initialize()

    def _post(self, payload: dict) -> dict | None:
        import urllib.request
        data = json.dumps(payload).encode()
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(
            f"{self._url}/messages",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            logger.error("MCP HTTP %s: %s", self.name, exc)
            return None

    def _initialize(self) -> bool:
        resp = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "NORA", "version": "1.0"},
            },
        })
        return resp is not None and "error" not in resp

    def list_tools(self) -> list[dict]:
        if self._tools_cache:
            return self._tools_cache
        resp = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/list",
            "params": {},
        })
        if resp and "error" not in resp:
            self._tools_cache = resp.get("result", {}).get("tools", [])
        return self._tools_cache

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        resp = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        })
        if not resp:
            return f"MCP HTTP server {self.name!r} not reachable."
        if "error" in resp:
            err = resp["error"]
            return f"MCP tool error: {err.get('message', str(err))}"
        return _extract_content(resp.get("result", {}))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_content(result: dict) -> str:
    """Pull text out of an MCP tool-call result."""
    content = result.get("content", [])
    if isinstance(content, list):
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        return "\n".join(texts) or "Done."
    if isinstance(content, str):
        return content
    return str(result) or "Done."


def _py_type(json_type: str) -> str:
    return {"string": "str", "integer": "int", "boolean": "bool", "number": "float"}.get(json_type, "str")


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def _register_server_tools(server: _MCPServer) -> int:
    tools = server.list_tools()
    count = 0
    for tool in tools:
        tool_name = tool.get("name", "")
        description = tool.get("description", "")
        schema = tool.get("inputSchema", {})
        if not tool_name:
            continue
        action_name = f"mcp_{server.name}_{tool_name}"
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        params = []
        for pname, pschema in props.items():
            ptype = _py_type(pschema.get("type", "string"))
            if pname in required:
                params.append(f"{pname}: {ptype}")
            else:
                params.append(f'{pname}: {ptype} = ""')
        sig = f"{action_name}({', '.join(params)})"

        def _make(tn: str = tool_name, sv: _MCPServer = server) -> Any:
            def _handler(**kwargs: Any) -> str:
                return sv.call_tool(tn, kwargs)
            _handler.__name__ = f"mcp_{sv.name}_{tn}"
            return _handler

        _cmd_registry[action_name] = _make()
        _cmd_meta[action_name] = CommandMeta(
            sig=sig,
            description=f"[MCP:{server.name}] {description}",
            risk="low",
            category="mcp",
        )
        count += 1
        logger.info("Registered MCP tool: %s", action_name)
    return count


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_all() -> None:
    """Start all configured MCP servers and register their tools into NORA."""
    servers_cfg = get_config().get("mcp_servers", [])
    if not servers_cfg:
        logger.debug("No MCP servers configured in config.yaml")
        return
    for cfg in servers_cfg:
        raw_name = cfg.get("name", "").strip()
        name = raw_name.replace("-", "_").replace(" ", "_")
        transport = cfg.get("transport", "stdio")
        if not name:
            logger.warning("MCP server entry missing 'name' — skipping")
            continue
        if name in _servers:
            continue
        if transport == "stdio":
            cmd = cfg.get("command", [])
            if not cmd:
                logger.warning("MCP server %s: no 'command' configured — skipping", name)
                continue
            srv: _MCPServer = _MCPStdioServer(name=name, cmd=cmd, env=cfg.get("env", {}))
        elif transport == "http":
            url = cfg.get("url", "")
            if not url:
                logger.warning("MCP server %s: no 'url' configured — skipping", name)
                continue
            srv = _MCPHttpServer(name=name, url=url, token=cfg.get("token", ""))
        else:
            logger.warning("MCP server %s: unknown transport %r — skipping", name, transport)
            continue
        if srv.start():
            n = _register_server_tools(srv)
            _servers[name] = srv
            logger.info("MCP server %s online: %d tools registered", name, n)
        else:
            logger.warning("MCP server %s failed to start — skipping", name)


def stop_all() -> None:
    """Gracefully stop all running MCP servers."""
    for srv in _servers.values():
        srv.stop()
    _servers.clear()


def get_server(name: str) -> _MCPServer | None:
    return _servers.get(name)

"""God's Eye View, the globe service on the core (Sharp Phase H).

God's Eye View (github.com/bilawalsidhu/gods-eye-view, MIT) is a CesiumJS
globe of public live data: flights, satellites, earthquakes, fires,
weather. It runs as its own service (nora-globe.service, a Vite server on
localhost) and answers MCP over plain HTTP at `/mcp`.

This is the whole client: one JSON-RPC `tools/call` per question. It is
not an `mcp_servers` entry on purpose. The MCP bridge flattens object
arguments to strings, which GEV rejects, and its 30 tools would undo Phase
C's small prompt. NORA's commands (`nora/commands/globe.py`) call the few
tools they need with flat arguments, speak GEV's one-line summary, and keep
its 4-8 KB of raw data away from the model.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from nora.config import get_config

logger = logging.getLogger("nora.gev")

DEFAULT_URL = "http://127.0.0.1:5173"


class Unavailable(RuntimeError):
    """The globe service isn't running or didn't answer in time."""


class Refused(RuntimeError):
    """GEV answered, but couldn't do it ("No place matched ..."); the
    message is fit to say."""


def _cfg() -> dict:
    return get_config().get("globe", {}) or {}


def enabled() -> bool:
    return bool(_cfg().get("enabled", True))


def url() -> str:
    return str(_cfg().get("url") or DEFAULT_URL).rstrip("/")


def call(tool: str, arguments: dict[str, Any], timeout: float | None = None) -> tuple[str, dict]:
    """Call one GEV tool. Returns (summary, data). Raises Unavailable when the
    service is down or slow, Refused when it answered with an error."""
    if not enabled():
        raise Unavailable("the globe is switched off")
    timeout = float(timeout or _cfg().get("timeout_sec", 12))
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}}).encode()
    req = urllib.request.Request(f"{url()}/mcp", body, {
        "content-type": "application/json", "accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            reply = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        logger.info("globe unavailable for %s: %s", tool, e)
        raise Unavailable(str(e)) from None
    except ValueError as e:
        raise Unavailable(f"unreadable reply: {e}") from None

    if "error" in reply:
        raise Refused(str((reply["error"] or {}).get("message") or "the globe refused that"))
    result = reply.get("result") or {}
    text = ""
    for part in result.get("content") or []:
        if part.get("type") == "text":
            text = part.get("text") or ""
            break
    if result.get("isError"):
        raise Refused(text or "the globe couldn't do that")
    data = result.get("structuredContent") or {}
    summary = text
    # The text part is "summary\n\n{json}" for clients that read only text.
    if "\n" in summary:
        summary = summary.split("\n", 1)[0]
    return summary.strip(), data


def ping(timeout: float = 3.0) -> bool:
    """Whether the service answers MCP at all (for `nora doctor`)."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
    req = urllib.request.Request(f"{url()}/mcp", body, {
        "content-type": "application/json", "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return "result" in json.loads(resp.read().decode("utf-8"))
    except Exception:
        return False

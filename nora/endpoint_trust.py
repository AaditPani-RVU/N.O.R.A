"""D-Bus endpoint trust graph — Codex integration 5.7 (see CODEX_INTEGRATION.md).

The tool trust ledger (nora.tool_trust) already scores hardcoded actions
(media_play_pause, wifi_connect, ...) per action name, and MCP tools are
registered as their own action (mcp_<alias>_<tool_name>) so they already
get per-endpoint treatment for free. The gap is dbus_call(): one generic
action that can reach *any* D-Bus service, so a brand-new, never-audited
service and org.freedesktop.Notifications would otherwise share a single
trust score.

This module keys trust by the actual service name instead, reusing
tool_trust's ledger under an "endpoint:" namespace — same storage, same
decay/proof rules, just a different key. Not a parallel system.
"""
from __future__ import annotations

import logging

from nora import tool_trust

logger = logging.getLogger("nora.endpoint_trust")

_PREFIX = "endpoint:"

# Well-known services NORA already ships hardcoded, reviewed actions for —
# treated as proven from day one so those voice commands aren't gated.
_TRUSTED_BY_DEFAULT = (
    "org.freedesktop.Notifications",
    "org.freedesktop.NetworkManager",
    "org.mpris.MediaPlayer2",
    "org.bluez",
)


def _key(service: str) -> str:
    return f"{_PREFIX}{service}"


def record(service: str, success: bool) -> None:
    """Score one dbus_call() invocation against its target service."""
    tool_trust.record(_key(service), success)


def is_proven(service: str) -> bool:
    """Known-good services are proven by default; everything else earns it."""
    if any(service == s or service.startswith(s + ".") for s in _TRUSTED_BY_DEFAULT):
        return True
    return tool_trust.is_proven(_key(service))


def status() -> str:
    """Natural-language summary of ad-hoc D-Bus endpoint trust for the voice interface."""
    with tool_trust._lock:
        tool_trust._load()
        items = {
            k[len(_PREFIX):]: v for k, v in tool_trust._stats.items() if k.startswith(_PREFIX)
        }
    if not items:
        return "No D-Bus endpoints have been called outside the built-in set yet."
    ranked = sorted(items.items(), key=lambda kv: kv[1]["ok"] + kv[1]["fail"], reverse=True)
    lines = []
    for service, e in ranked[:8]:
        total = e["ok"] + e["fail"]
        pct = int(round(100 * tool_trust.score(_key(service))))
        proof = "proven" if is_proven(service) else "unproven"
        lines.append(f"{service}: {pct}% reliable over {total} calls, {proof}")
    return ". ".join(lines)

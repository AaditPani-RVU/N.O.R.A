"""Time-Travel Filesystem + CRIU Session Continuity — F4 Linux flagship.

Voice commands for COW filesystem snapshots and CRIU process checkpointing.
Auto-selects the best snapshot backend (btrfs > zfs > rsync).

Snapshot hooks into reversible.py via register_pre_action_hook() so risky
actions (file edit, package install) auto-snapshot before executing.

Install: sudo apt install btrfs-progs criu && pip install pycriu && nora-linux-setup
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from nora.command_engine import register

logger = logging.getLogger("nora.commands.time_travel")


def _get_backend():
    """Auto-detect and return the best available snapshot backend."""
    from nora.platform.linux import snapshot_btrfs, snapshot_zfs, snapshot_rsync

    if snapshot_btrfs.is_available():
        return snapshot_btrfs, "btrfs"
    if snapshot_zfs.is_available():
        return snapshot_zfs, "zfs"
    return snapshot_rsync, "rsync"


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ── Voice Commands ────────────────────────────────────────────────────────────

@register(
    "snapshot_now",
    sig="snapshot_now(label: str = 'manual')",
    description="Take a filesystem snapshot right now, labeled for easy rollback later.",
    risk="low",
    category="time",
)
async def snapshot_now(label: str = "manual") -> str:
    loop = asyncio.get_event_loop()
    backend, backend_name = _get_backend()
    result = await loop.run_in_executor(None, backend.create, label)
    if result["ok"]:
        loc = result.get("path") or result.get("snapshot", "")
        return f"Snapshot '{label}' taken via {backend_name}. Location: {loc}"
    return f"Snapshot failed ({backend_name}): {result.get('error', 'unknown error')}"


@register(
    "list_snapshots",
    sig="list_snapshots()",
    description="List all available filesystem snapshots with their labels and times.",
    risk="low",
    category="time",
)
async def list_snapshots() -> str:
    loop = asyncio.get_event_loop()
    backend, backend_name = _get_backend()
    snaps = await loop.run_in_executor(None, backend.list_snapshots)
    if not snaps:
        return f"No snapshots found ({backend_name} backend)."
    lines = []
    for s in snaps[:10]:
        label = s.get("label", "?")
        ts = s.get("ts", 0)
        when = _fmt_time(ts) if ts else s.get("creation", "?")
        lines.append(f"  '{label}' — {when}")
    return f"{len(snaps)} snapshot(s) via {backend_name}:\n" + "\n".join(lines)


@register(
    "rollback_to",
    sig="rollback_to(label_or_time: str)",
    description="Roll back the filesystem to a named snapshot. This is destructive — requires confirmation.",
    risk="high",
    requires_confirmation=True,
    category="time",
)
async def rollback_to(label_or_time: str) -> str:
    loop = asyncio.get_event_loop()
    backend, backend_name = _get_backend()
    result = await loop.run_in_executor(None, backend.rollback, label_or_time)
    if result["ok"]:
        loc = result.get("path") or result.get("snapshot", label_or_time)
        return (
            f"Rolled back to snapshot '{label_or_time}' via {backend_name}. "
            f"Files restored from: {loc}. "
            "Reload any open editors to see the changes."
        )
    return f"Rollback failed ({backend_name}): {result.get('error', 'unknown error')}"


@register(
    "pause_session",
    sig="pause_session(name: str, pids: list = None)",
    description="Checkpoint the current coding session (editor, terminals, dev server) to disk via CRIU so it can be resumed after a reboot.",
    risk="high",
    requires_confirmation=True,
    category="time",
)
async def pause_session(name: str, pids: list | None = None) -> str:
    loop = asyncio.get_event_loop()
    try:
        from nora.platform.linux import criu_session
    except ImportError:
        return "CRIU session module not available."

    if not criu_session.is_available():
        return (
            "CRIU is not installed or not functional on this kernel. "
            "Run: sudo apt install criu && criu check"
        )

    result = await loop.run_in_executor(None, criu_session.dump, name, pids)
    if result["ok"]:
        n = len(result.get("pids", []))
        return (
            f"Session '{name}' paused. {n} process tree(s) checkpointed to disk. "
            f"Say 'resume session {name}' to restore."
        )
    return f"Session pause failed: {result.get('error', 'unknown error')}"


@register(
    "resume_session",
    sig="resume_session(name: str)",
    description="Restore a CRIU-paused session — brings back editor, terminals, and dev server as they were.",
    risk="high",
    requires_confirmation=True,
    category="time",
)
async def resume_session(name: str) -> str:
    loop = asyncio.get_event_loop()
    try:
        from nora.platform.linux import criu_session
    except ImportError:
        return "CRIU session module not available."

    result = await loop.run_in_executor(None, criu_session.restore, name)
    if result["ok"]:
        pids = result.get("restored_pids", [])
        return f"Session '{name}' restored. PIDs resumed: {pids}."
    return f"Session restore failed: {result.get('error', 'unknown error')}"


@register(
    "list_sessions",
    sig="list_sessions()",
    description="List CRIU-paused sessions that can be resumed.",
    risk="low",
    category="time",
)
async def list_sessions() -> str:
    loop = asyncio.get_event_loop()
    try:
        from nora.platform.linux import criu_session
        sessions = await loop.run_in_executor(None, criu_session.list_sessions)
    except Exception:
        return "CRIU not available."
    if not sessions:
        return "No paused sessions found."
    lines = [f"  '{s.get('name', '?')}'" for s in sessions[:8]]
    return f"{len(sessions)} paused session(s):\n" + "\n".join(lines)


# ── Pre-action snapshot hook ──────────────────────────────────────────────────

# Actions that warrant an auto-snapshot before execution
_AUTO_SNAPSHOT_ACTIONS = {
    "patch_file", "move_file", "delete_file",
    "refactor_selection", "add_tests_for_selection",
}


def _pre_action_cb(action: str, params: dict) -> None:
    """Auto-snapshot before risky file-changing actions."""
    if action not in _AUTO_SNAPSHOT_ACTIONS:
        return
    try:
        backend, _ = _get_backend()
        label = f"auto_{action}_{int(time.time())}"
        result = backend.create(label)
        if result.get("ok"):
            logger.debug("Auto-snapshot taken before %s: %s", action, result.get("path") or result.get("snapshot"))
    except Exception as e:
        logger.debug("Auto-snapshot failed for %s: %s", action, e)


def register_with_reversible() -> None:
    """Wire the pre-action snapshot hook into reversible.py. Call at startup."""
    try:
        from nora import reversible
        reversible.register_pre_action_hook(_pre_action_cb)
        logger.debug("time_travel pre-action hook registered with reversible")
    except Exception as e:
        logger.debug("Could not register with reversible: %s", e)

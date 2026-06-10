"""Audit Log — Sprint 4 Deliverable #23 (P1).

Every NORA action is appended to nora_audit_log.jsonl as a timestamped record.
Supports:
  - record()          : called by command_engine after each action
  - get_recent()      : returns list of recent entries (for voice/dashboard)
  - summarize_recent(): natural-language spoken summary of recent actions
  - Voice commands    : audit_log_summary, what_did_you_do_last
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from nora.command_engine import register

logger = logging.getLogger("nora.audit_log")

_ROOT = Path(__file__).resolve().parent.parent
_LOG_PATH = _ROOT / "nora_audit_log.jsonl"
_lock = threading.Lock()

# In-memory ring buffer for fast recent lookups (avoids re-reading the file)
_BUFFER_SIZE = 200
_buffer: list[dict[str, Any]] = []


def record(
    action: str,
    params: dict[str, Any],
    result: str,
    success: bool,
    user_text: str = "",
) -> None:
    """Append one action entry to the audit log. Thread-safe."""
    entry: dict[str, Any] = {
        "ts": time.time(),
        "ts_human": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "params": params,
        "result": result[:300],
        "success": success,
        "user_text": user_text[:200],
    }
    with _lock:
        _buffer.append(entry)
        if len(_buffer) > _BUFFER_SIZE:
            del _buffer[:-_BUFFER_SIZE]
        try:
            with _LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("audit_log write failed: %s", e)


def get_recent(minutes: int = 60) -> list[dict[str, Any]]:
    """Return entries from the last N minutes, newest first."""
    cutoff = time.time() - (minutes * 60)
    with _lock:
        return [e for e in reversed(_buffer) if e["ts"] >= cutoff]


def get_last_n(n: int = 10) -> list[dict[str, Any]]:
    """Return the N most-recent entries."""
    with _lock:
        return list(reversed(_buffer[-n:]))


def _natural_list(entries: list[dict[str, Any]], cap: int = 8) -> str:
    """Convert a list of entries to a spoken natural-language summary."""
    if not entries:
        return "No recorded actions."
    shown = entries[:cap]
    lines: list[str] = []
    for e in shown:
        action_label = e["action"].replace("_", " ")
        ts_label = e.get("ts_human", "")
        ok = "succeeded" if e["success"] else "failed"
        user_text = e.get("user_text", "")
        if user_text:
            lines.append(f'{ts_label}: {action_label} — "{user_text}" — {ok}')
        else:
            lines.append(f"{ts_label}: {action_label} — {ok}")
    body = ". ".join(lines)
    if len(entries) > cap:
        body += f". Plus {len(entries) - cap} more."
    return body


# ── Voice Commands ─────────────────────────────────────────────────────────


@register(
    "audit_log_summary",
    sig="audit_log_summary(minutes: int = 60)",
    description="Speak a summary of everything NORA did in the last N minutes",
    category="memory",
)
def audit_log_summary(minutes: int = 60) -> str:
    """Summarize all actions taken in the last N minutes."""
    entries = get_recent(minutes)
    if not entries:
        return f"I haven't recorded any actions in the last {minutes} minutes."
    ok_count = sum(1 for e in entries if e["success"])
    fail_count = len(entries) - ok_count
    summary = _natural_list(entries)
    header = (
        f"In the last {minutes} minutes I ran {len(entries)} action"
        f"{'s' if len(entries) != 1 else ''}: "
        f"{ok_count} succeeded, {fail_count} failed. "
    )
    return header + summary


@register(
    "what_did_you_do_last",
    sig="what_did_you_do_last(count: int = 5)",
    description="Speak the last N actions NORA took",
    category="memory",
)
def what_did_you_do_last(count: int = 5) -> str:
    """Read back the N most recent actions taken by NORA."""
    entries = get_last_n(count)
    if not entries:
        return "I haven't done anything yet this session."
    return f"My last {min(count, len(entries))} actions: " + _natural_list(entries, cap=count)

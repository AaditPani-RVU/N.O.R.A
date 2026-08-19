"""Reflection / failure diary — Codex integration 2.3 + 5.9 (see CODEX_INTEGRATION.md).

Turns the signals NORA already collects (audit log, reversible log) into
a periodic self-review: what failed, what the user undid, and what that
suggests changing. This is the bridge from "assistant that learns your
commands" to "assistant that learns from its own mistakes."

Pure log analysis — no LLM call, no network, nothing to pay for.
Exposed to the voice interface via commands/autonomy_commands.py.
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("nora.reflection")

_ROOT = Path(__file__).resolve().parent.parent
_AUDIT_PATH = _ROOT / "nora_audit_log.jsonl"
_REVERSIBLE_PATH = _ROOT / "nora_reversible_log.json"

# An action undone at or above this rate deserves a behavior-change proposal
_REVERSAL_CONCERN_RATE = 0.3
_FAILURE_CONCERN_RATE = 0.4


def _read_audit(days: int) -> list[dict[str, Any]]:
    if not _AUDIT_PATH.exists():
        return []
    cutoff = time.time() - days * 86400
    entries: list[dict[str, Any]] = []
    try:
        with _AUDIT_PATH.open(encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("ts", 0) >= cutoff:
                    entries.append(e)
    except Exception as e:
        logger.warning("reflection: audit read failed: %s", e)
    return entries


def _read_reversals(days: int) -> list[dict[str, Any]]:
    if not _REVERSIBLE_PATH.exists():
        return []
    cutoff = time.time() - days * 86400
    try:
        raw = json.loads(_REVERSIBLE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("reflection: reversible read failed: %s", e)
        return []
    if not isinstance(raw, list):
        return []
    return [e for e in raw if e.get("undone") and e.get("ts", 0) >= cutoff]


def analyze(days: int = 7) -> dict[str, Any]:
    """Group the window's outcomes by action and flag concerning patterns."""
    audit = _read_audit(days)
    reversals = _read_reversals(days)

    by_action: dict[str, dict[str, int]] = defaultdict(lambda: {"ok": 0, "fail": 0, "undone": 0})
    for e in audit:
        by_action[e.get("action", "?")]["ok" if e.get("success") else "fail"] += 1
    for e in reversals:
        by_action[e.get("action", "?")]["undone"] += 1

    concerns: list[dict[str, Any]] = []
    for action, c in by_action.items():
        total = c["ok"] + c["fail"]
        if total == 0:
            continue
        fail_rate = c["fail"] / total
        undo_rate = c["undone"] / total
        if undo_rate >= _REVERSAL_CONCERN_RATE and c["undone"] >= 2:
            concerns.append({
                "action": action,
                "kind": "reversal",
                "detail": f"you undid {c['undone']} of {total} runs",
                "proposal": "I should ask before doing this, or change how I do it.",
            })
        elif fail_rate >= _FAILURE_CONCERN_RATE and c["fail"] >= 3:
            concerns.append({
                "action": action,
                "kind": "failure",
                "detail": f"{c['fail']} of {total} runs failed",
                "proposal": "This tool may be broken or I'm calling it wrong.",
            })

    return {
        "days": days,
        "total_actions": sum(c["ok"] + c["fail"] for c in by_action.values()),
        "total_failures": sum(c["fail"] for c in by_action.values()),
        "total_reversals": len(reversals),
        "by_action": dict(by_action),
        "concerns": concerns,
    }


def report(days: int = 7) -> str:
    """Spoken failure diary: what went wrong and what I'd change."""
    a = analyze(days)
    if a["total_actions"] == 0:
        return f"I have no recorded actions in the last {days} days."

    parts = [
        f"In the last {days} days I ran {a['total_actions']} actions, "
        f"{a['total_failures']} failed, and you reversed {a['total_reversals']}."
    ]
    if not a["concerns"]:
        parts.append("Nothing stands out as a pattern worth changing.")
    else:
        for c in a["concerns"][:5]:
            parts.append(
                f"With {c['action'].replace('_', ' ')}, {c['detail']}. {c['proposal']}"
            )
    return " ".join(parts)

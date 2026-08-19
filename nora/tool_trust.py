"""Tool trust ledger — Codex integration 2.4 (see CODEX_INTEGRATION.md).

Rolling per-action reliability: trust rises with clean executions, falls
with failures, and falls hardest when the user reverses the result.
Consumed by autonomy.classify() — a proven action can run silently; a
fresh plugin or MCP endpoint starts cautious ("new-tool caution").

Persisted to nora_tool_trust.json in the project root, matching the
other nora_* state files. Thread-safe; stdlib only.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from nora.config import get_config

logger = logging.getLogger("nora.tool_trust")

_ROOT = Path(__file__).resolve().parent.parent
_STORE_PATH = _ROOT / "nora_tool_trust.json"
_lock = threading.Lock()

_stats: dict[str, dict[str, Any]] = {}
_loaded = False

# A reversal is stronger evidence of misbehavior than a plain failure
_REVERSAL_WEIGHT = 2


def _load() -> None:
    global _stats, _loaded
    if _loaded:
        return
    if _STORE_PATH.exists():
        try:
            raw = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
            _stats = raw if isinstance(raw, dict) else {}
        except Exception as e:
            logger.warning("tool_trust load failed: %s", e)
            _stats = {}
    _loaded = True


def _save() -> None:
    try:
        _STORE_PATH.write_text(
            json.dumps(_stats, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        logger.warning("tool_trust save failed: %s", e)


def _entry(action: str) -> dict[str, Any]:
    return _stats.setdefault(
        action, {"ok": 0, "fail": 0, "reversed": 0, "first_ts": time.time(), "last_ts": 0.0}
    )


def record(action: str, success: bool) -> None:
    """Score one invocation. Called by the pipeline after every executed step."""
    with _lock:
        _load()
        e = _entry(action)
        e["ok" if success else "fail"] += 1
        e["last_ts"] = time.time()
        _save()


def record_reversal(action: str) -> None:
    """Called when the user undoes an action's result — the strongest negative signal."""
    with _lock:
        _load()
        e = _entry(action)
        e["reversed"] += 1
        e["last_ts"] = time.time()
        _save()
    logger.info("tool_trust: reversal recorded for %s", action)


def score(action: str) -> float:
    """Reliability in [0, 1] with Laplace smoothing; reversals count double."""
    with _lock:
        _load()
        e = _stats.get(action)
    if not e:
        return 0.5  # no evidence either way
    ok = e["ok"]
    bad = e["fail"] + e["reversed"] * _REVERSAL_WEIGHT
    return (ok + 1) / (ok + bad + 2)


def runs(action: str) -> int:
    with _lock:
        _load()
        e = _stats.get(action)
    return (e["ok"] + e["fail"]) if e else 0


def is_proven(action: str) -> bool:
    """Enough clean history to earn silent execution."""
    cfg = get_config().get("autonomy", {})
    min_runs = int(cfg.get("unproven_min_runs", 5))
    return runs(action) >= min_runs and score(action) >= 0.8


def summary(n: int = 10) -> str:
    """Natural-language trust report for the voice interface."""
    with _lock:
        _load()
        items = sorted(_stats.items(), key=lambda kv: kv[1]["ok"] + kv[1]["fail"], reverse=True)
    if not items:
        return "I haven't built up any tool history yet."
    lines = []
    for action, e in items[:n]:
        total = e["ok"] + e["fail"]
        pct = int(round(100 * score(action)))
        rev = f", reversed {e['reversed']} times" if e["reversed"] else ""
        lines.append(f"{action}: {pct}% reliable over {total} runs{rev}")
    return ". ".join(lines)

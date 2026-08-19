"""Consent memory — Codex integration 5.2 + 5.10 (see CODEX_INTEGRATION.md).

Every confirmation prompt is a chance to learn. This module records what
the user approved or denied, weighted by recency (consent decay: a "yes"
from six months ago is not a "yes" from yesterday), and lets the autonomy
layer stop asking about things the user always approves — and stop
assuming things they always deny.

Distinct from tool_trust: that scores the *tool's* reliability; this
scores the *user's consent pattern* for an action class.

Persisted to nora_consent_log.json. Thread-safe; stdlib only.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from pathlib import Path
from typing import Any

from nora.config import get_config

logger = logging.getLogger("nora.consent_memory")

_ROOT = Path(__file__).resolve().parent.parent
_STORE_PATH = _ROOT / "nora_consent_log.json"
_lock = threading.Lock()

_events: list[dict[str, Any]] = []
_loaded = False
_MAX_EVENTS = 2000


def _load() -> None:
    global _events, _loaded
    if _loaded:
        return
    if _STORE_PATH.exists():
        try:
            raw = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
            _events = raw if isinstance(raw, list) else []
        except Exception as e:
            logger.warning("consent_memory load failed: %s", e)
            _events = []
    _loaded = True


def _save() -> None:
    try:
        _STORE_PATH.write_text(
            json.dumps(_events, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        logger.warning("consent_memory save failed: %s", e)


def record(actions: list[str], approved: bool) -> None:
    """Log the outcome of one confirmation prompt (one event per action)."""
    now = time.time()
    with _lock:
        _load()
        for action in actions:
            _events.insert(0, {"ts": now, "action": action, "approved": approved})
        del _events[_MAX_EVENTS:]
        _save()


def record_false_yes(action: str) -> None:
    """The user approved, then reversed within minutes — count it as a denial."""
    record([action], approved=False)


def _decay_weight(ts: float, half_life_days: float) -> float:
    age_days = max(0.0, (time.time() - ts) / 86400)
    return math.pow(0.5, age_days / half_life_days)


def approval_stats(action: str) -> tuple[float, float]:
    """Return (decayed approval rate, decayed effective sample size)."""
    cfg = get_config().get("autonomy", {})
    half_life = float(cfg.get("consent_half_life_days", 30))
    with _lock:
        _load()
        events = [e for e in _events if e.get("action") == action]
    if not events:
        return 0.5, 0.0
    yes = sum(_decay_weight(e["ts"], half_life) for e in events if e.get("approved"))
    total = sum(_decay_weight(e["ts"], half_life) for e in events)
    return (yes / total if total else 0.5), total


def always_approves(action: str) -> bool:
    """User has consistently and recently said yes to this action class."""
    rate, n = approval_stats(action)
    return n >= 4.0 and rate >= 0.95


def always_denies(action: str) -> bool:
    """User has consistently and recently said no — propose, don't presume."""
    rate, n = approval_stats(action)
    return n >= 3.0 and rate <= 0.1

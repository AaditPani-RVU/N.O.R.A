"""User Model Layer — typed accessors over cognitive + task memory.

Exposes a compact user card (≤200 tokens) that is auto-injected into the
system prompt so NORA can make personalised decisions without re-asking.

Preference versioning (CODEX_INTEGRATION.md 2.5): learned preferences
("Aadit prefers concise responses") are not overwritten silently. Each
change is proposed with a reason, held for review, and either confirmed
by the user or auto-applied after a config-configurable window if no
objection is raised. Every version is kept for introspection/rollback.
Persisted to nora_user_preferences.json.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from nora.config import get_config

logger = logging.getLogger("nora.user_model")

_ROOT = Path(__file__).resolve().parent.parent
_BRIEF_PATH = _ROOT / "nora_daily_brief.json"
_PREFS_PATH = _ROOT / "nora_user_preferences.json"
_prefs_lock = threading.Lock()
_prefs: list[dict[str, Any]] | None = None


# ── Typed accessors ────────────────────────────────────────────────────────────

def peak_hours() -> list[str]:
    """Return the two time bins with the highest command volume."""
    from nora.cognitive_memory import _load_user_model
    m = _load_user_model()
    totals: dict[str, int] = {}
    for tb, days in m.get("activity_heatmap", {}).items():
        totals[tb] = sum(len(v) for v in days.values())
    if not totals:
        return []
    return sorted(totals, key=totals.get, reverse=True)[:2]  # type: ignore[arg-type]


def top_commands(n: int = 5) -> list[str]:
    """Return the n most frequently used action names."""
    from nora.cognitive_memory import _load_user_model
    m = _load_user_model()
    counter: Counter = Counter()
    for _tb, days in m.get("activity_heatmap", {}).items():
        for _dow, actions in days.items():
            counter.update(actions)
    return [a for a, _ in counter.most_common(n)]


def current_projects() -> list[str]:
    """Infer active project names from git log and today's daily brief."""
    projects: list[str] = []

    # Check daily brief first (already consolidated)
    if _BRIEF_PATH.exists():
        try:
            brief = json.loads(_BRIEF_PATH.read_text(encoding="utf-8"))
            projects.extend(brief.get("active_projects", []))
        except Exception:
            pass

    if projects:
        return projects[:3]

    # Fallback: parse git log for repo names
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=3, cwd=_ROOT
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            name = url.rstrip("/").split("/")[-1].removesuffix(".git")
            if name:
                projects.append(name)
    except Exception:
        pass

    # Also add the root dir name as a hint
    projects.append(_ROOT.name)
    return list(dict.fromkeys(projects))[:3]  # deduplicate, keep order


def recent_frustrations(hours: int = 48) -> list[str]:
    """Return text summaries of recent failed commands."""
    from nora.cognitive_memory import get_recent_episodes
    cutoff = time.time() - hours * 3600
    results = []
    for ep in get_recent_episodes(n=50):
        if ep.get("ts", 0) < cutoff:
            continue
        if not ep.get("success", True):
            results.append(ep.get("text", "")[:80])
        if len(results) >= 3:
            break
    return results


def stale_threads() -> list[str]:
    """Return titles of open tasks not touched in >2 days."""
    from nora import task_ledger
    cutoff = time.time() - 2 * 86400
    tasks = task_ledger.get_open_tasks()
    return [t["title"] for t in tasks if t.get("updated_at", 0) < cutoff][:3]


def open_task_count() -> int:
    from nora import task_ledger
    return len(task_ledger.get_open_tasks())


def preferred_terminology() -> list[str]:
    """Return the user's most-used non-trivial words from recent commands."""
    from nora.cognitive_memory import get_recent_episodes
    _STOP = {"the", "a", "an", "i", "to", "and", "or", "is", "it", "me", "my",
             "in", "on", "of", "for", "with", "that", "this", "was", "be", "can",
             "please", "nora", "hey"}
    counter: Counter = Counter()
    for ep in get_recent_episodes(n=30):
        words = ep.get("text", "").lower().split()
        counter.update(w for w in words if len(w) > 3 and w not in _STOP)
    return [w for w, _ in counter.most_common(5)]


# ── Preference versioning (2.5) ─────────────────────────────────────────────────

def _load_prefs() -> list[dict[str, Any]]:
    global _prefs
    if _prefs is not None:
        return _prefs
    try:
        _prefs = json.loads(_PREFS_PATH.read_text(encoding="utf-8")) if _PREFS_PATH.exists() else []
    except Exception:
        _prefs = []
    return _prefs


def _save_prefs() -> None:
    if _prefs is None:
        return
    try:
        _PREFS_PATH.write_text(json.dumps(_prefs, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("preference save failed: %s", e)


def _history(key: str) -> list[dict[str, Any]]:
    return [p for p in _load_prefs() if p["key"] == key]


def get_preference(key: str, default: Any = None) -> Any:
    """Return the current applied value for a preference, or default."""
    applied = [p for p in _history(key) if p["applied"]]
    return applied[-1]["new"] if applied else default


def propose_preference(key: str, value: Any, reason: str) -> dict[str, Any]:
    """Propose a change to a learned preference; does not apply it silently.

    Applied immediately only if auto_apply_hours is 0 (config opt-in for
    trivial preferences); otherwise held until confirm_preference() or
    apply_pending() ages it past auto_apply_hours.
    """
    previous = get_preference(key)
    now = time.time()
    entry = {
        "key": key,
        "previous": previous,
        "new": value,
        "reason": reason,
        "ts": now,
        "ts_human": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "applied": False,
    }
    auto_hours = float(get_config().get("user_model", {}).get("preference_auto_apply_hours", 24))
    with _prefs_lock:
        _load_prefs()
        if auto_hours <= 0:
            entry["applied"] = True
        _prefs.append(entry)
        _save_prefs()
    return entry


def confirm_preference(key: str) -> bool:
    """Apply the most recent pending proposal for a preference."""
    with _prefs_lock:
        for entry in reversed(_load_prefs()):
            if entry["key"] == key and not entry["applied"]:
                entry["applied"] = True
                _save_prefs()
                return True
    return False


def reject_preference(key: str) -> bool:
    """Discard the most recent pending proposal, keeping the prior value."""
    with _prefs_lock:
        prefs = _load_prefs()
        for i in range(len(prefs) - 1, -1, -1):
            if prefs[i]["key"] == key and not prefs[i]["applied"]:
                del prefs[i]
                _save_prefs()
                return True
    return False


def apply_pending(now: float | None = None) -> list[str]:
    """Auto-apply proposals older than preference_auto_apply_hours. Returns applied keys."""
    now = now or time.time()
    auto_hours = float(get_config().get("user_model", {}).get("preference_auto_apply_hours", 24))
    applied: list[str] = []
    with _prefs_lock:
        for entry in _load_prefs():
            if entry["applied"]:
                continue
            if now - entry["ts"] >= auto_hours * 3600:
                entry["applied"] = True
                applied.append(entry["key"])
        if applied:
            _save_prefs()
    return applied


def preference_history(key: str) -> list[dict[str, Any]]:
    """Full version history for a preference, oldest first."""
    return list(_history(key))


def rollback_preference(key: str) -> str:
    """Revert an applied preference to its previous value, recorded as a new version."""
    applied = [p for p in _history(key) if p["applied"]]
    if len(applied) < 2:
        return f"No prior version of '{key}' to roll back to."
    current, prior = applied[-1], applied[-2]
    propose_preference(key, prior["new"], reason=f"rollback from '{current['new']}'")
    confirm_preference(key)
    return f"Rolled '{key}' back from '{current['new']}' to '{prior['new']}'."


# ── User card ──────────────────────────────────────────────────────────────────

def get_user_card() -> dict[str, Any]:
    """Return a compact user-context dict for system prompt injection (≤200 tokens)."""
    card: dict[str, Any] = {}

    ph = peak_hours()
    if ph:
        card["peak_hours"] = ph

    tc = top_commands(5)
    if tc:
        card["top_commands"] = tc

    otc = open_task_count()
    if otc:
        card["open_tasks"] = otc

    st = stale_threads()
    if st:
        card["stale_threads"] = st

    rf = recent_frustrations(48)
    if rf:
        card["recent_failures"] = rf[:2]

    cp = current_projects()
    if cp:
        card["active_projects"] = cp

    return card


def format_user_card_for_prompt() -> str:
    """Format the user card as a compact string for the system prompt USER PROFILE block."""
    card = get_user_card()
    if not card:
        return ""
    lines: list[str] = []
    if "peak_hours" in card:
        lines.append(f"- Peak hours: {', '.join(card['peak_hours'])}")
    if "top_commands" in card:
        lines.append(f"- Frequent commands: {', '.join(card['top_commands'])}")
    if "active_projects" in card:
        lines.append(f"- Active projects: {', '.join(card['active_projects'])}")
    if "open_tasks" in card:
        lines.append(f"- Open tasks: {card['open_tasks']}")
    if "stale_threads" in card:
        lines.append(f"- Stale threads: {'; '.join(card['stale_threads'])}")
    if "recent_failures" in card:
        lines.append(f"- Recent failures: {card['recent_failures'][0][:60]}")
    return "\n".join(lines)

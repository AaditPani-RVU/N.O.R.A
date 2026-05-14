"""User Model Layer — typed accessors over cognitive + task memory.

Exposes a compact user card (≤200 tokens) that is auto-injected into the
system prompt so NORA can make personalised decisions without re-asking.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any

logger = logging.getLogger("nora.user_model")

_ROOT = Path(__file__).resolve().parent.parent
_BRIEF_PATH = _ROOT / "nora_daily_brief.json"


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

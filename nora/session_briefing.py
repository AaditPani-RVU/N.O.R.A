"""Session Replay Briefing — spoken summary delivered on the first wake of a new session.

A "new session" is defined as: >4 hours since last startup, OR a different
calendar day. Briefing sources: Task Ledger, episode log, and daily_brief.json.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("nora.session_briefing")

_ROOT = Path(__file__).resolve().parent.parent
_STATE_PATH = _ROOT / "nora_session_state.json"
_BRIEF_PATH = _ROOT / "nora_daily_brief.json"

_SESSION_GAP_SEC = 4 * 3600


def _load_state() -> dict:
    if _STATE_PATH.exists():
        try:
            return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(state: dict) -> None:
    try:
        _STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("Session state save failed: %s", e)


def is_new_session() -> bool:
    """True if this startup is a new session (gap >4h or different calendar day)."""
    state = _load_state()
    last_ts = state.get("last_startup_ts", 0)
    last_date = state.get("last_startup_date", "")
    today = datetime.now().strftime("%Y-%m-%d")
    return (time.time() - last_ts) > _SESSION_GAP_SEC or last_date != today


def mark_session_started() -> None:
    state = _load_state()
    state["last_startup_ts"] = time.time()
    state["last_startup_date"] = datetime.now().strftime("%Y-%m-%d")
    _save_state(state)


def generate_briefing() -> str | None:
    """Build a 3–5 sentence spoken briefing from available data sources."""
    parts: list[str] = []

    # Open tasks
    try:
        from nora import task_ledger
        open_tasks = task_ledger.get_open_tasks()
        if open_tasks:
            count = len(open_tasks)
            oldest = open_tasks[-1]["title"]
            if count == 1:
                parts.append(f"You have one open task: {oldest}.")
            else:
                parts.append(
                    f"You have {count} open tasks. "
                    f"The oldest is: {oldest}."
                )
    except Exception as e:
        logger.warning("Briefing: task ledger error: %s", e)

    # Yesterday's daily brief
    try:
        if _BRIEF_PATH.exists():
            brief = json.loads(_BRIEF_PATH.read_text(encoding="utf-8"))
            date_str = brief.get("date", "your last session")
            cmds = brief.get("commands_run", 0)
            errors = brief.get("errors_hit", 0)
            projects = brief.get("active_projects", [])
            git_commits = brief.get("git_commits", [])
            if cmds:
                tail = f" across {', '.join(projects[:2])}" if projects else ""
                err_note = f" with {errors} error{'s' if errors != 1 else ''}" if errors else ""
                parts.append(
                    f"On {date_str} you ran {cmds} commands{err_note}{tail}."
                )
            if git_commits:
                parts.append(f"Last commit: {git_commits[0]}.")
    except Exception as e:
        logger.warning("Briefing: daily brief error: %s", e)

    # Recent failures
    try:
        from nora.cognitive_memory import get_recent_episodes
        recent = get_recent_episodes(n=30)
        failures = [ep for ep in recent if not ep.get("success", True)][:2]
        if failures:
            sample = failures[0].get("text", "")[:60]
            count = len(failures)
            parts.append(
                f"Note: {count} recent command{'s' if count > 1 else ''} failed — "
                f"last was: \"{sample}\"."
            )
    except Exception as e:
        logger.warning("Briefing: episode error: %s", e)

    if not parts:
        return None

    return " ".join(parts)


def get_briefing() -> str | None:
    """
    Return a spoken briefing string if this is a new session, else None.
    Always updates the session startup timestamp.
    """
    new_session = is_new_session()
    mark_session_started()
    if not new_session:
        return None
    return generate_briefing()

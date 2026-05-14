"""Nightly Consolidation Job — scans the day's activity and writes nora_daily_brief.json.

Runs once per day at a configurable time (default 23:00). Produces a
daily_brief.json and stores a summary entry in ChromaDB as high-priority
retrieval context for the Session Replay Briefing and User Model.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("nora.consolidation")

_ROOT = Path(__file__).resolve().parent.parent
_BRIEF_PATH = _ROOT / "nora_daily_brief.json"
_STATE_PATH = _ROOT / "nora_consolidation_state.json"

_lock = threading.Lock()
_timer: threading.Timer | None = None
_running = False


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
        logger.warning("Consolidation state save failed: %s", e)


def already_ran_today() -> bool:
    return _load_state().get("last_run_date") == datetime.now().strftime("%Y-%m-%d")


def _git_activity() -> list[str]:
    """Return recent one-line commit messages from the project root git repo."""
    try:
        result = subprocess.run(
            ["git", "log", "--since=1.day", "--oneline"],
            capture_output=True, text=True, timeout=5, cwd=_ROOT,
        )
        if result.returncode == 0:
            return result.stdout.strip().splitlines()[:10]
    except Exception:
        pass
    return []


def run_consolidation() -> dict[str, Any] | None:
    """Build and persist today's daily brief. Returns the brief or None if already ran."""
    if already_ran_today():
        logger.info("Consolidation already ran today — skipping")
        return None

    logger.info("Running nightly consolidation…")
    now = datetime.now()
    cutoff = time.time() - 86400  # last 24 hours

    # Scan recent episodes
    commands_run = 0
    errors_hit = 0
    action_counter: Counter = Counter()
    try:
        from nora.cognitive_memory import get_recent_episodes
        for ep in get_recent_episodes(n=200):
            if ep.get("ts", 0) < cutoff:
                continue
            commands_run += 1
            if not ep.get("success", True):
                errors_hit += 1
            for act in ep.get("actions", []):
                action_counter[act] += 1
    except Exception as e:
        logger.warning("Consolidation: episode scan failed: %s", e)

    # Scan tasks updated today
    completed_tasks: list[str] = []
    abandoned_tasks: list[str] = []
    try:
        from nora import task_ledger
        for t in task_ledger.get_recent_tasks(n=50):
            if t.get("updated_at", 0) < cutoff:
                continue
            if t["status"] == task_ledger.STATUS_CLOSED:
                completed_tasks.append(t["title"])
            elif t["status"] == task_ledger.STATUS_ABANDONED:
                abandoned_tasks.append(t["title"])
    except Exception as e:
        logger.warning("Consolidation: task scan failed: %s", e)

    dominant_actions = [a for a, _ in action_counter.most_common(5)]

    git_commits = _git_activity()

    # Active projects from user model
    active_projects: list[str] = []
    try:
        from nora.user_model import current_projects
        active_projects = current_projects()
    except Exception:
        pass

    brief: dict[str, Any] = {
        "date": now.strftime("%Y-%m-%d"),
        "generated_at": time.time(),
        "commands_run": commands_run,
        "errors_hit": errors_hit,
        "dominant_actions": dominant_actions,
        "active_projects": active_projects,
        "completed_tasks": completed_tasks,
        "abandoned_tasks": abandoned_tasks,
        "git_commits": git_commits[:5],
    }

    try:
        _BRIEF_PATH.write_text(json.dumps(brief, indent=2), encoding="utf-8")
        logger.info("Daily brief written: %s", _BRIEF_PATH)
    except Exception as e:
        logger.warning("Consolidation: brief write failed: %s", e)

    # Store summary in ChromaDB as a high-priority knowledge entry
    try:
        from nora.cognitive_memory import record_knowledge
        summary = (
            f"Daily brief {brief['date']}: {commands_run} commands, {errors_hit} errors. "
            f"Active projects: {', '.join(active_projects) or 'none'}. "
            f"Completed tasks: {', '.join(completed_tasks) or 'none'}. "
            f"Git: {', '.join(git_commits[:3]) or 'no commits'}."
        )
        record_knowledge(summary, source="daily_consolidation", tags=["daily_brief", brief["date"]])
    except Exception as e:
        logger.warning("Consolidation: ChromaDB store failed: %s", e)

    _save_state({"last_run_date": now.strftime("%Y-%m-%d"), "last_run_ts": time.time()})
    logger.info(
        "Nightly consolidation complete — %d commands, %d errors",
        commands_run, errors_hit,
    )
    return brief


def _run_and_reschedule() -> None:
    run_consolidation()
    _schedule_next()


def _schedule_next(hour: int = 23, minute: int = 0) -> None:
    global _timer
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    delay = (target - now).total_seconds()
    logger.info(
        "Next consolidation in %.0f s (at %s)",
        delay, target.strftime("%Y-%m-%d %H:%M"),
    )
    _timer = threading.Timer(delay, _run_and_reschedule)
    _timer.daemon = True
    _timer.start()


def start() -> None:
    """Start the nightly consolidation scheduler. Call once from pipeline.run()."""
    global _running
    with _lock:
        if _running:
            return
        _running = True
    from nora.config import get_config
    cfg = get_config().get("consolidation", {})
    hour = int(cfg.get("run_hour", 23))
    minute = int(cfg.get("run_minute", 0))
    _schedule_next(hour, minute)
    logger.info("Consolidation scheduler started (target %02d:%02d)", hour, minute)


def stop() -> None:
    global _running, _timer
    with _lock:
        _running = False
        if _timer:
            _timer.cancel()
            _timer = None

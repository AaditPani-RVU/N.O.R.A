"""Task Ledger — persistent structured store of open and closed tasks.

Tasks are stored in nora_tasks.json at the project root.
Each task: id, title, status, notes, log, associated_files, commands,
created_at, updated_at, closed_at.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger("nora.task_ledger")

_ROOT = Path(__file__).resolve().parent.parent
_TASKS_PATH = _ROOT / "nora_tasks.json"
_lock = threading.RLock()
_tasks: dict[str, dict] | None = None

STATUS_OPEN = "open"
STATUS_IN_PROGRESS = "in_progress"
STATUS_CLOSED = "closed"
STATUS_ABANDONED = "abandoned"


def _load() -> dict[str, dict]:
    global _tasks
    if _tasks is not None:
        return _tasks
    if _TASKS_PATH.exists():
        try:
            _tasks = json.loads(_TASKS_PATH.read_text(encoding="utf-8"))
        except Exception:
            _tasks = {}
    else:
        _tasks = {}
    return _tasks


def _save() -> None:
    if _tasks is None:
        return
    try:
        _TASKS_PATH.write_text(json.dumps(_tasks, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("Task ledger save failed: %s", e)


def create_task(title: str, notes: str = "", associated_files: list[str] | None = None) -> str:
    """Create a new open task and return its short ID."""
    with _lock:
        tasks = _load()
        task_id = str(uuid.uuid4())[:8]
        now = time.time()
        tasks[task_id] = {
            "id": task_id,
            "title": title,
            "status": STATUS_OPEN,
            "notes": notes,
            "log": [],
            "associated_files": associated_files or [],
            "commands": [],
            "created_at": now,
            "updated_at": now,
            "closed_at": None,
        }
        _save()
        logger.info("Task created: %s — %s", task_id, title)
        return task_id


def update_task(task_id: str, **kwargs: Any) -> bool:
    """Update allowed task fields. Returns True if the task was found."""
    with _lock:
        tasks = _load()
        if task_id not in tasks:
            return False
        allowed = {"title", "status", "notes", "associated_files", "commands"}
        for k, v in kwargs.items():
            if k in allowed:
                tasks[task_id][k] = v
        tasks[task_id]["updated_at"] = time.time()
        _save()
        return True


def append_log(task_id: str, entry: str) -> bool:
    """Append a timestamped log note to a task."""
    with _lock:
        tasks = _load()
        if task_id not in tasks:
            return False
        tasks[task_id]["log"].append({"ts": time.time(), "entry": entry})
        tasks[task_id]["updated_at"] = time.time()
        _save()
        return True


def close_task(task_id: str, status: str = STATUS_CLOSED) -> bool:
    """Mark a task closed (or abandoned). Returns True if found."""
    with _lock:
        tasks = _load()
        if task_id not in tasks:
            return False
        tasks[task_id]["status"] = status
        tasks[task_id]["closed_at"] = time.time()
        tasks[task_id]["updated_at"] = time.time()
        _save()
        return True


def find_tasks(query: str, statuses: list[str] | None = None) -> list[dict]:
    """Return tasks whose title or notes contain query (case-insensitive), optionally filtered by status."""
    with _lock:
        tasks = _load()
        q = query.lower()
        results = []
        for t in tasks.values():
            if statuses and t["status"] not in statuses:
                continue
            if q in t["title"].lower() or q in t.get("notes", "").lower():
                results.append(dict(t))
        results.sort(key=lambda x: x["updated_at"], reverse=True)
        return results


def get_open_tasks() -> list[dict]:
    """Return all open/in_progress tasks sorted by most recently updated."""
    with _lock:
        tasks = _load()
        result = [dict(t) for t in tasks.values()
                  if t["status"] in (STATUS_OPEN, STATUS_IN_PROGRESS)]
        result.sort(key=lambda x: x["updated_at"], reverse=True)
        return result


def get_recent_tasks(n: int = 20, include_closed: bool = True) -> list[dict]:
    """Return the n most recently updated tasks."""
    with _lock:
        tasks = _load()
        result = list(tasks.values())
        if not include_closed:
            result = [t for t in result if t["status"] not in (STATUS_CLOSED, STATUS_ABANDONED)]
        result.sort(key=lambda x: x["updated_at"], reverse=True)
        return [dict(t) for t in result[:n]]


def get_task(task_id: str) -> dict | None:
    with _lock:
        tasks = _load()
        t = tasks.get(task_id)
        return dict(t) if t else None


def task_summary() -> dict[str, int]:
    """Return counts by status."""
    with _lock:
        tasks = _load()
        counts: dict[str, int] = {}
        for t in tasks.values():
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        return counts

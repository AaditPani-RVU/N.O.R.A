"""Task Ledger — persistent structured store of open and closed tasks.

Tasks live in the `tasks` table of the core store (`nora.store`).
Each task: id, title, status, notes, log, associated_files, commands,
created_at, updated_at, closed_at, due_on (an ISO date, or None). Every
function returns plain dicts in that shape, with `log`, `associated_files`
and `commands` as lists.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import date
from typing import Any

from nora import store

logger = logging.getLogger("nora.task_ledger")

STATUS_OPEN = "open"
STATUS_IN_PROGRESS = "in_progress"
STATUS_CLOSED = "closed"
STATUS_ABANDONED = "abandoned"

_LIST_FIELDS = ("log", "associated_files", "commands")
_COLUMNS = ("id", "title", "status", "notes", *_LIST_FIELDS,
            "created_at", "updated_at", "closed_at", "due_on")
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM tasks"


def _to_dict(row) -> dict:
    task = {k: row[k] for k in _COLUMNS}
    for k in _LIST_FIELDS:
        task[k] = json.loads(task[k] or "[]")
    return task


def _fetch(where: str = "", params: tuple = ()) -> list[dict]:
    sql = _SELECT + (f" WHERE {where}" if where else "") + " ORDER BY updated_at DESC"
    return [_to_dict(r) for r in store.query(sql, params)]


def create_task(title: str, notes: str = "", associated_files: list[str] | None = None,
                due_on: date | None = None) -> str:
    """Create a new open task and return its short ID."""
    task_id = str(uuid.uuid4())[:8]
    now = time.time()
    with store.transaction() as conn:
        conn.execute(
            f"INSERT INTO tasks ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})",
            (task_id, title, STATUS_OPEN, notes, "[]",
             json.dumps(associated_files or []), "[]", now, now, None,
             due_on.isoformat() if due_on else None),
        )
    logger.info("Task created: %s — %s", task_id, title)
    return task_id


def update_task(task_id: str, **kwargs: Any) -> bool:
    """Update allowed task fields. Returns True if the task was found."""
    allowed = {"title", "status", "notes", "associated_files", "commands", "due_on"}
    changes = {k: (json.dumps(v) if k in _LIST_FIELDS else v)
               for k, v in kwargs.items() if k in allowed}
    if isinstance(changes.get("due_on"), date):
        changes["due_on"] = changes["due_on"].isoformat()
    changes["updated_at"] = time.time()
    assigns = ", ".join(f"{k} = ?" for k in changes)
    with store.transaction() as conn:
        cur = conn.execute(f"UPDATE tasks SET {assigns} WHERE id = ?",
                           (*changes.values(), task_id))
        return cur.rowcount > 0


def append_log(task_id: str, entry: str) -> bool:
    """Append a timestamped log note to a task."""
    now = time.time()
    with store.transaction() as conn:
        row = conn.execute("SELECT log FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return False
        log = json.loads(row["log"] or "[]")
        log.append({"ts": now, "entry": entry})
        conn.execute("UPDATE tasks SET log = ?, updated_at = ? WHERE id = ?",
                     (json.dumps(log), now, task_id))
        return True


def close_task(task_id: str, status: str = STATUS_CLOSED) -> bool:
    """Mark a task closed (or abandoned). Returns True if found."""
    now = time.time()
    with store.transaction() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status = ?, closed_at = ?, updated_at = ? WHERE id = ?",
            (status, now, now, task_id),
        )
        return cur.rowcount > 0


def find_tasks(query: str, statuses: list[str] | None = None) -> list[dict]:
    """Return tasks whose title or notes contain query (case-insensitive), optionally filtered by status."""
    q = query.lower()
    results = []
    for t in _fetch():
        if statuses and t["status"] not in statuses:
            continue
        if q in t["title"].lower() or q in (t.get("notes") or "").lower():
            results.append(t)
    return results


def get_open_tasks() -> list[dict]:
    """Return all open/in_progress tasks sorted by most recently updated."""
    return _fetch("status IN (?, ?)", (STATUS_OPEN, STATUS_IN_PROGRESS))


def due_by(day: date) -> list[dict]:
    """Open tasks due on or before `day`, earliest first."""
    rows = _fetch("status IN (?, ?) AND due_on IS NOT NULL AND due_on <= ?",
                  (STATUS_OPEN, STATUS_IN_PROGRESS, day.isoformat()))
    return sorted(rows, key=lambda t: (t["due_on"], t["created_at"]))


def due_that_day(day: date) -> list[dict]:
    """Open tasks due on exactly `day`."""
    return [t for t in due_by(day) if t["due_on"] == day.isoformat()]


def undated_open() -> list[dict]:
    """Open tasks with no due date, most recently touched first."""
    return _fetch("status IN (?, ?) AND due_on IS NULL", (STATUS_OPEN, STATUS_IN_PROGRESS))


def get_recent_tasks(n: int = 20, include_closed: bool = True) -> list[dict]:
    """Return the n most recently updated tasks."""
    if include_closed:
        return _fetch()[:n]
    return _fetch("status NOT IN (?, ?)", (STATUS_CLOSED, STATUS_ABANDONED))[:n]


def get_task(task_id: str) -> dict | None:
    found = _fetch("id = ?", (task_id,))
    return found[0] if found else None


def task_summary() -> dict[str, int]:
    """Return counts by status."""
    rows = store.query("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
    return {r["status"]: r["n"] for r in rows}

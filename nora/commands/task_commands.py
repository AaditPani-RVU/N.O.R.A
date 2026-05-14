"""Task Ledger voice commands — create, list, close, and log tasks by voice."""
from __future__ import annotations

from datetime import datetime

from nora.command_engine import register


@register(
    "add_task",
    sig="add_task(title: str, notes: str = '')",
    description="Add a new open task to the Task Ledger",
    category="tasks",
)
def add_task(title: str, notes: str = "") -> str:
    from nora import task_ledger
    task_id = task_ledger.create_task(title, notes=notes)
    return f"Task added: {title}. ID {task_id}."


@register(
    "list_tasks",
    sig="list_tasks()",
    description="List open tasks from the Task Ledger",
    category="tasks",
)
def list_tasks() -> str:
    from nora import task_ledger
    tasks = task_ledger.get_open_tasks()
    if not tasks:
        return "No open tasks. You're clear."
    count = len(tasks)
    lines = []
    for t in tasks[:4]:
        age = datetime.now() - datetime.fromtimestamp(t["updated_at"])
        age_str = f"{age.days}d ago" if age.days > 0 else "today"
        lines.append(f"{t['title']} ({t['status'].replace('_', ' ')}, {age_str})")
    summary = ". ".join(lines)
    return f"You have {count} open task{'s' if count != 1 else ''}. {summary}."


@register(
    "close_task",
    sig="close_task(query: str)",
    description="Mark a task as closed by fuzzy-matching its title",
    category="tasks",
)
def close_task(query: str) -> str:
    from nora import task_ledger
    matches = task_ledger.find_tasks(
        query, statuses=[task_ledger.STATUS_OPEN, task_ledger.STATUS_IN_PROGRESS]
    )
    if not matches:
        return f"No open task matching '{query}'."
    task = matches[0]
    task_ledger.close_task(task["id"])
    return f"Closed task: {task['title']}."


@register(
    "start_task",
    sig="start_task(query: str)",
    description="Mark a task as in-progress by fuzzy-matching its title",
    category="tasks",
)
def start_task(query: str) -> str:
    from nora import task_ledger
    matches = task_ledger.find_tasks(query, statuses=[task_ledger.STATUS_OPEN])
    if not matches:
        return f"No open task matching '{query}'."
    task = matches[0]
    task_ledger.update_task(task["id"], status=task_ledger.STATUS_IN_PROGRESS)
    return f"Started task: {task['title']}."


@register(
    "log_task_note",
    sig="log_task_note(query: str, note: str)",
    description="Append a log note to a task by fuzzy-matching its title",
    category="tasks",
)
def log_task_note(query: str, note: str) -> str:
    from nora import task_ledger
    matches = task_ledger.find_tasks(query)
    if not matches:
        return f"No task matching '{query}'."
    task = matches[0]
    task_ledger.append_log(task["id"], note)
    return f"Logged note on '{task['title']}'."


@register(
    "task_status",
    sig="task_status()",
    description="Summarise task counts by status",
    category="tasks",
)
def task_status() -> str:
    from nora import task_ledger
    counts = task_ledger.task_summary()
    if not counts:
        return "No tasks recorded yet."
    parts = [f"{v} {k.replace('_', ' ')}" for k, v in counts.items()]
    return "Tasks: " + ", ".join(parts) + "."

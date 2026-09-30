"""Task Ledger voice commands — create, list, close, and log tasks by voice."""
from __future__ import annotations

import concurrent.futures
import logging
from datetime import date, datetime

from nora import days
from nora.command_engine import register

logger = logging.getLogger("nora.commands.tasks")

# The calendar is a network call on a turn someone is waiting for.
_CALENDAR_TIMEOUT_SEC = 5.0


def _tidy_title(title: str) -> str:
    t = title.strip().rstrip(".!?").strip()
    return t[:1].upper() + t[1:] if t else t


@register(
    "add_task",
    sig="add_task(title: str, notes: str = '', due: str = '')",
    description="Add a to-do to the Task Ledger. `due` is a day as said: "
                "'tomorrow', 'friday', 'the 5th'. A day left in the title is found too",
    category="tasks",
)
def add_task(title: str, notes: str = "", due: str = "") -> str:
    from nora import task_ledger
    today = date.today()
    when = days.parse(due, today) if due else None
    if when is None:
        # "submit the assignment tomorrow" arrives whole from the fast path,
        # and often from the model too.
        title, when = days.split(title, today)
        if due and when is None:
            # A day we can't read stays with the task rather than vanishing.
            notes = f"{notes} (due {due})".strip()
    title = _tidy_title(title)
    if not title:
        return "What's the task?"
    # Said twice (typed again, or once on each device) is one task, not two.
    same = [t for t in task_ledger.get_open_tasks() if t["title"].casefold() == title.casefold()]
    if same:
        task = same[0]
        if when is not None and task["due_on"] != when.isoformat():
            task_ledger.update_task(task["id"], due_on=when)
            return f"That's already on your list. Moved it to {days.say(when, today)}."
        due = f", due {days.say(date.fromisoformat(task['due_on']), today)}" if task["due_on"] else ""
        return f"That's already on your list{due}."
    task_ledger.create_task(title, notes=notes, due_on=when)
    if when is None:
        return f"Added to your list: {title}."
    return f"Got it: {title}, due {days.say(when, today)}."


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
    today = date.today()
    # Dated ones first, soonest first; then the rest, most recent first.
    tasks = sorted(tasks, key=lambda t: (t.get("due_on") is None, t.get("due_on") or ""))
    lines = []
    for t in tasks[:4]:
        if t.get("due_on"):
            lines.append(f"{t['title']} (due {days.say(date.fromisoformat(t['due_on']), today)})")
            continue
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


def _reminders_on(day: date) -> list[str]:
    """Reminders set to go off on `day`, as "5 pm: call mom"."""
    from nora import scheduler
    start = datetime.combine(day, datetime.min.time()).timestamp()
    end = start + 86400
    out = []
    for s in scheduler.listing():
        if start <= s.next_run < end and s.what.startswith(scheduler._REMINDER_PREFIX):
            clock = datetime.fromtimestamp(s.next_run).strftime("%-I:%M %p").replace(":00 ", " ")
            out.append(f"{clock.lower()}: {s.what[len(scheduler._REMINDER_PREFIX):].strip()}")
    return out


def _calendar_on(day: date) -> list[str]:
    """Calendar events on `day`, or nothing when the calendar isn't reachable."""
    def fetch() -> list[str]:
        from nora.commands.google_services import _calendar_service, _day_window, _fmt_event_time
        time_min, time_max = _day_window(datetime.combine(day, datetime.min.time()))
        items = _calendar_service().events().list(
            calendarId="primary", timeMin=time_min, timeMax=time_max, maxResults=10,
            singleEvents=True, orderBy="startTime").execute().get("items", [])
        return [f"{ev.get('summary', 'Untitled')} at {_fmt_event_time(ev)}" for ev in items]

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(fetch).result(timeout=_CALENDAR_TIMEOUT_SEC)
    except Exception as e:
        logger.info("agenda: calendar skipped (%s)", e or type(e).__name__)
        return []
    finally:
        pool.shutdown(wait=False)


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


@register(
    "agenda",
    sig="agenda(day: str = 'today')",
    description="What the user has to do on a day: tasks due (and overdue), reminders, "
                "calendar. For 'what do I need to do today/tomorrow'",
    category="tasks",
)
def agenda(day: str = "today") -> str:
    from nora import task_ledger
    today = date.today()
    when = days.parse(day or "today", today) or today
    label = days.say(when, today)
    is_today = when == today

    due = task_ledger.due_by(when) if is_today else task_ledger.due_that_day(when)
    overdue = [t for t in due if t["due_on"] < when.isoformat()]
    due = [t for t in due if t["due_on"] == when.isoformat()]
    reminders = _reminders_on(when)
    events = _calendar_on(when)
    undated = task_ledger.undated_open()

    parts = []
    if due:
        parts.append(f"Due {label}: {_join([t['title'] for t in due])}.")
    if overdue:
        late = [f"{t['title']} (was due {days.say(date.fromisoformat(t['due_on']), today)})"
                for t in overdue[:3]]
        parts.append(f"Overdue: {_join(late)}.")
    if events:
        parts.append(f"On your calendar: {_join(events[:5])}.")
    if reminders:
        parts.append(f"Reminders: {_join(reminders[:4])}.")
    if not parts:
        parts.append(f"Nothing due {label}.")
    if undated:
        n = len(undated)
        if n <= 2:
            parts.append(f"No date on: {_join([t['title'] for t in undated])}.")
        else:
            parts.append(f"And {n} open tasks with no date.")
    return " ".join(parts)

"""Scheduling commands — "remind me at six", "brief me every morning".

Thin voice surface over `nora.scheduler`. The parsing of "every weekday at
7:30" lives there; this module only turns results into sentences.
"""
from __future__ import annotations

import logging
from datetime import datetime

from nora import scheduler
from nora.command_engine import register

logger = logging.getLogger("nora.commands.schedule")


def _spoken_time(ts: float) -> str:
    """Render a due-time the way it would be said, not printed."""
    when = datetime.fromtimestamp(ts)
    now = datetime.now()
    clock = when.strftime("%-I:%M %p").lower().replace(":00", "")
    delta_days = (when.date() - now.date()).days
    if delta_days == 0:
        return f"today at {clock}"
    if delta_days == 1:
        return f"tomorrow at {clock}"
    if delta_days < 7:
        return f"{when.strftime('%A')} at {clock}"
    return f"{when.strftime('%B %-d')} at {clock}"


@register(
    "schedule_task",
    sig="schedule_task(when: str, what: str)",
    description=(
        "Schedule something to happen later, once or repeatedly. `when` takes "
        "natural time — 'at 6pm', 'in 20 minutes', 'every day at 7am', "
        "'every Monday at 9', 'every 30 minutes'. `what` is the instruction to "
        "carry out, which can be any spoken command. Survives restarts."
    ),
    category="tasks",
)
def schedule_task(when: str, what: str) -> str:
    sched = scheduler.add(when, what)
    if sched is None:
        return (
            "I couldn't work out a time from that. Try something like "
            "'at 6pm', 'in 20 minutes', or 'every day at 7'."
        )
    lead = "I'll do that" if sched.recurring else "Set"
    cadence = f" {sched.spec}" if sched.recurring else ""
    return f"{lead}{cadence}. Next run {_spoken_time(sched.next_run)}."


@register(
    "list_schedules",
    sig="list_schedules()",
    description="Say what's scheduled to run later.",
    category="tasks",
)
def list_schedules() -> str:
    items = scheduler.listing()
    if not items:
        return "Nothing scheduled."
    if len(items) == 1:
        s = items[0]
        return f"One thing: {s.what}, {_spoken_time(s.next_run)}."
    lines = [f"{len(items)} things scheduled."]
    for s in items[:5]:
        lines.append(f"{s.what}, {_spoken_time(s.next_run)}.")
    return " ".join(lines)


@register(
    "cancel_schedule",
    sig="cancel_schedule(what: str)",
    description="Cancel a scheduled task, matched by what it does.",
    category="tasks",
)
def cancel_schedule(what: str) -> str:
    sched = scheduler.remove(what)
    if sched is None:
        return "I couldn't find a scheduled task matching that."
    return f"Cancelled: {sched.what}."

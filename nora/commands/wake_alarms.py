"""The night-before alarm NORA sets on the phone (Phase 8c, `nora.calendar_rules`).

"Undo" right after she sets it cancels it through `nora.reversible`; these are
for later: "cancel tomorrow's alarm", "when's my alarm".
"""
from __future__ import annotations

from datetime import datetime

from nora import calendar_rules
from nora.command_engine import register


@register(
    "cancel_wake_alarm",
    sig="cancel_wake_alarm(day='')",
    description="Cancel the morning alarm NORA set on the phone from the calendar (day: ISO date, '' = the next one)",
    category="system",
)
def cancel_wake_alarm(day: str = "") -> str:
    at = calendar_rules.cancel_alarm(str(day or "").strip())
    if at is None:
        return "There's no alarm of mine to cancel."
    return f"Cancelled the {at} alarm on your phone."


@register(
    "wake_alarm_status",
    sig="wake_alarm_status()",
    description="When the next morning alarm NORA set from the calendar goes off, and what it's for",
    category="system",
)
def wake_alarm_status() -> str:
    upcoming = calendar_rules.alarms()
    if not upcoming:
        return "I haven't set any alarms from your calendar."
    a = upcoming[0]
    at = datetime.fromtimestamp(a["at_ms"] / 1000)
    day = "tomorrow" if (at.date() - datetime.now().date()).days == 1 else at.strftime("%A")
    return f"Your alarm is set for {calendar_rules._clock(at)} {day}, for {a['title']}."

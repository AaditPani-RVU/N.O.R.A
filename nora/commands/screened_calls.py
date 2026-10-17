"""Unknown callers, as the phone screened them (NORA_CALLS_PLAN §3.1).

"Block the last number that called" and "call that number back" are fast-path
rules straight to the phone's `phone.block_number` and `phone.call_contact`,
so a call back is approved on the phone like any other call.
"""
from __future__ import annotations

from nora import screened_calls
from nora.command_engine import register


@register(
    "who_called",
    sig="who_called(hours: int = 24)",
    category="device",
    description="unknown numbers (not in the phone's contacts) that called the user's phone "
                "recently, with times and whether they were silenced. For 'who called', "
                "'who called while I was out'. Calls from contacts are not tracked",
)
def who_called(hours: int = 24) -> str:
    try:
        hours = max(1, min(int(hours), 24 * 30))
    except (TypeError, ValueError):
        hours = 24
    return screened_calls.describe(screened_calls.recent(hours), hours)

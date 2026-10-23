"""Who called: the phone's missed calls (NORA_CALLS_PLAN §3.2) and the unknown
numbers it screened (§3.1).

"Block the last number that called" and "call that number back" are fast-path
rules straight to the phone's `phone.block_number` and `phone.call_contact`,
so a call back is approved on the phone like any other call. "Call back" on
its own is `call_back`: whoever NORA last said had called.
"""
from __future__ import annotations

from nora import missed_calls
from nora.command_engine import register
from nora.schemas import StepResult


@register(
    "who_called",
    sig="who_called(hours: int = 24)",
    category="device",
    description="calls the user missed on their phone recently, from contacts and unknown numbers, "
                "with when and the meeting they were in. For 'who called', 'did anyone call', "
                "'any missed calls', 'who called while I was out'",
)
async def who_called(hours: int = 24) -> StepResult:
    try:
        hours = max(1, min(int(hours), 24 * 30))
    except (TypeError, ValueError):
        hours = 24
    said, from_phone = await missed_calls.who_called(hours)
    # Caller names come from the phone's notifications: said, not kept.
    return StepResult(action="who_called", success=True, message=said, untrusted=from_phone)


@register(
    "call_back",
    sig="call_back()",
    category="device",
    description="call back whoever last called the user and was missed ('call her back', "
                "'call them back', 'call back'). Approved on the phone first",
)
async def call_back() -> StepResult:
    from nora.hub import server
    who = await missed_calls.who_to_call_back()
    if who is None:
        return StepResult(action="call_back", success=True,
                          message="I don't know of a missed call to return.")
    return await server.call_capability("phone.call_contact", missed_calls.callback_params(who))

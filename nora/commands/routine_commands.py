"""Routines by voice: "I fed Milo", "did I feed Milo?" (NORA_ASSIST_PLAN C)."""
from __future__ import annotations

from nora.command_engine import register


@register(
    "log_routine",
    sig="log_routine(what: str)",
    description="Log something the user just did that's a daily routine: 'I fed Milo' → "
                "log_routine('fed Milo'), 'walked the dog', 'took my meds'",
    category="routines",
)
def log_routine(what: str) -> str:
    from nora import routines
    return routines.log(what)


@register(
    "last_routine",
    sig="last_routine(what: str)",
    description="Whether/when the user last did a routine: 'did I feed Milo?', "
                "'when did I last walk Milo?', 'has Milo been fed?' → last_routine('feed Milo')",
    category="routines",
)
def last_routine(what: str) -> str:
    from nora import routines
    return routines.answer(what)


@register(
    "add_routine",
    sig="add_routine(what: str, times: str)",
    description="Make something a daily routine NORA reminds about if it isn't logged by its "
                "times: 'remind me if I haven't fed Milo by 9 am and 9 pm' → "
                "add_routine('feed Milo', '9 am and 9 pm')",
    category="routines",
)
def add_routine(what: str, times: str) -> str:
    from nora import routines
    return routines.add(what, times)


@register(
    "remove_routine",
    sig="remove_routine(what: str)",
    description="Stop reminders for a routine",
    category="routines",
)
def remove_routine(what: str) -> str:
    from nora import routines
    return routines.remove(what)


@register(
    "list_routines",
    sig="list_routines()",
    description="The routines NORA keeps track of",
    category="routines",
)
def list_routines() -> str:
    from nora import routines
    return routines.listing()

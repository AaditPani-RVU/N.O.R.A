"""Voice commands for the Codex integration layer (CODEX_INTEGRATION.md).

Exposes autonomy status, tool trust, focus state, and the reflection
failure diary to the voice interface.
"""
from __future__ import annotations

import logging

from nora import autonomy, endpoint_trust, focus, post_action_cards, reflection, silent_hours, tool_trust, user_model
from nora.command_engine import register

logger = logging.getLogger("nora.commands.autonomy")


@register(
    "autonomy_status",
    sig="autonomy_status()",
    description="Report the autonomy tiering state and any session demotions",
    category="system",
    risk="low",
)
def autonomy_status() -> str:
    return autonomy.status()


@register(
    "focus_status",
    sig="focus_status()",
    description="Report the current attention state (available, call, media, away)",
    category="system",
    risk="low",
)
def focus_status() -> str:
    return focus.status()


@register(
    "trust_report",
    sig="trust_report()",
    description="Report per-tool reliability scores from the trust ledger",
    category="system",
    risk="low",
)
def trust_report() -> str:
    return tool_trust.summary()


@register(
    "reflection_report",
    sig="reflection_report(days: int = 7)",
    description="Failure diary: what went wrong recently and what NORA would change",
    category="memory",
    risk="low",
)
def reflection_report(days: int = 7) -> str:
    return reflection.report(int(days))


@register(
    "silent_hours_status",
    sig="silent_hours_status()",
    description="Report whether now falls in a learned quiet window",
    category="system",
    risk="low",
)
def silent_hours_status() -> str:
    return silent_hours.status()


@register(
    "explain_last_action",
    sig="explain_last_action()",
    description="Explain what NORA thought you wanted, what it did, what it skipped, and how to undo it",
    category="memory",
    risk="low",
)
def explain_last_action() -> str:
    return post_action_cards.explain_last()


@register(
    "endpoint_trust_report",
    sig="endpoint_trust_report()",
    description="Report trust scores for D-Bus services reached via the generic dbus_call action",
    category="system",
    risk="low",
)
def endpoint_trust_report() -> str:
    return endpoint_trust.status()


@register(
    "preference_status",
    sig='preference_status(key="response_length")',
    description="Report the current value and any pending change for a learned preference",
    category="memory",
    risk="low",
)
def preference_status(key: str) -> str:
    current = user_model.get_preference(key)
    pending = [p for p in user_model.preference_history(key) if not p["applied"]]
    if pending:
        p = pending[-1]
        return (f"Current '{key}' is {current!r}. Pending change to {p['new']!r} "
                f"({p['reason']}) — say 'confirm preference' or 'reject preference' to decide.")
    if current is None:
        return f"No learned value for '{key}' yet."
    return f"'{key}' is currently {current!r}."


@register(
    "confirm_preference",
    sig='confirm_preference(key="response_length")',
    description="Confirm a pending learned-preference change",
    category="memory",
    risk="low",
)
def confirm_preference(key: str) -> str:
    if user_model.confirm_preference(key):
        return f"Applied the pending change to '{key}'."
    return f"No pending change for '{key}'."


@register(
    "reject_preference",
    sig='reject_preference(key="response_length")',
    description="Discard a pending learned-preference change and keep the prior value",
    category="memory",
    risk="low",
)
def reject_preference(key: str) -> str:
    if user_model.reject_preference(key):
        return f"Discarded the pending change to '{key}'."
    return f"No pending change for '{key}'."


@register(
    "rollback_preference",
    sig='rollback_preference(key="response_length")',
    description="Revert a learned preference to its previous value",
    category="memory",
    risk="low",
)
def rollback_preference(key: str) -> str:
    return user_model.rollback_preference(key)

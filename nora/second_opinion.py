"""Second-opinion mode — Codex integration 5.8 (see CODEX_INTEGRATION.md).

For high-stakes plans (aggregated risk >= 2, or an unproven tool/endpoint
in the mix) route the parsed intent through an independent, rule-based
sanity checker before autonomy.classify() picks a tier. Deliberately not
a second LLM call — offline-safe, zero added latency or API cost,
mirroring reflection.py's "pure analysis, no LLM" precedent. If a second
*model* opinion is ever wanted for the very highest-stakes plans, route
through nora.commands.ask_claude; this module is the always-on baseline.

A flag here can only ADD a confirmation, mirroring autonomy.py's own
escalate-only invariant — the two disagree, so NORA asks rather than guesses.
"""
from __future__ import annotations

import inspect
import logging

from nora.schemas import IntentResponse

logger = logging.getLogger("nora.second_opinion")

_DESTRUCTIVE_HINTS = ("delete", "remove", "kill", "shutdown", "wipe", "erase", "format", "destroy")


def _param_mismatch(action: str, params: dict) -> str | None:
    """Catches the LLM hallucinating an argument or omitting a required one."""
    from nora import command_engine

    handler = command_engine._registry.get(action)
    if handler is None:
        return f"'{action}' has no registered handler"
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return None

    accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    known = {
        name for name, p in sig.parameters.items()
        if p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    }
    if not accepts_kwargs:
        unknown = set(params) - known
        if unknown:
            return f"'{action}' called with unexpected argument(s): {', '.join(sorted(unknown))}"

    missing = [
        name for name, p in sig.parameters.items()
        if p.default is inspect.Parameter.empty
        and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        and name not in params
    ]
    if missing:
        return f"'{action}' is missing required argument(s): {', '.join(missing)}"
    return None


def _risk_label_mismatch(action: str) -> str | None:
    """A destructive-sounding action registered as low risk is a red flag."""
    from nora import command_engine

    meta = command_engine.get_action_meta(action)
    if meta is None or meta.risk != "low":
        return None
    if any(hint in action.lower() for hint in _DESTRUCTIVE_HINTS):
        return f"'{action}' sounds destructive but is registered risk=low"
    return None


def review(intent: IntentResponse) -> tuple[bool, str]:
    """Independent sanity pass over a parsed plan. Returns (flagged, reason)."""
    for step in intent.steps:
        reason = _param_mismatch(step.action, step.parameters) or _risk_label_mismatch(step.action)
        if reason:
            logger.info("second_opinion: flagged — %s", reason)
            return True, reason
    return False, ""

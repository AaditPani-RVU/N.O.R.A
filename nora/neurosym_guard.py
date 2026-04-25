"""NeuroSym-AI integration: voice-input injection detection and action policy validation.

Two guards sit at different points in the pipeline:
  - input_guard  : runs on raw transcription before the LLM sees it
  - action_guard : runs on the parsed intent plan before execution
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nora.schemas import IntentResponse

logger = logging.getLogger("nora.neurosym_guard")

try:
    from neurosym import Guard, PromptInjectionRule
    from neurosym.rules.action_policy import (
        destructive_needs_confirmation,
        max_steps,
        no_path_outside_sandbox,
    )
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False
    logger.warning(
        "neurosym-ai not installed â€” security guardrails disabled. "
        "Run: pip install neurosym-ai"
    )

_input_guard: object = None
_action_guard: object = None

# Allow operations anywhere under the user's home directory
_SANDBOX = [os.path.expanduser("~").replace("\\", "/")]


def _input() -> object:
    global _input_guard
    if not _AVAILABLE:
        return None
    if _input_guard is None:
        _input_guard = Guard(
            rules=[PromptInjectionRule()],
            deny_above="high",
        )
    return _input_guard


def _action() -> object:
    global _action_guard
    if not _AVAILABLE:
        return None
    if _action_guard is None:
        _action_guard = Guard(
            rules=[
                destructive_needs_confirmation(),
                max_steps(15),
                no_path_outside_sandbox(_SANDBOX),
            ],
            # Don't auto-block "high" â€” destructive_needs_confirmation is high and we
            # handle it by requiring confirmation rather than outright blocking.
            deny_above="critical",
        )
    return _action_guard


def check_input(text: str) -> tuple[bool, list[dict]]:
    """Prompt-injection check on raw transcription.

    Returns (is_safe, violations). Violations list is empty when neurosym-ai is absent.
    """
    guard = _input()
    if guard is None:
        return True, []
    result = guard.apply_text(text)
    if not result.ok:
        logger.warning("Input blocked [%s]: %.80s", result.violations[0].get("severity", "?"), text)
    return result.ok, list(result.violations)


def check_intent(intent: IntentResponse) -> tuple[bool, bool, list[dict]]:
    """Action-policy check on a parsed intent plan.

    Returns (is_safe, needs_confirmation, violations).
      - is_safe           : False â†’ block execution entirely
      - needs_confirmation: True  â†’ set intent.requires_confirmation before running
    """
    guard = _action()
    if guard is None:
        return True, False, []

    plan = intent.model_dump()
    result = guard.apply_json(plan)

    needs_confirm = any(
        v.get("rule_id") == "policy.destructive_needs_confirmation"
        for v in result.violations
    )

    if not result.ok:
        logger.warning("Action plan blocked by NeuroSym: %s", result.violations)

    return result.ok, needs_confirm, list(result.violations)

"""Confidence templates + verification loop — Codex integration 2.6/5.11
(see CODEX_INTEGRATION.md).

Heuristic confidence estimate over an already-parsed intent — no extra LLM
call, just signals already on hand: unknown actions, parse errors, and
ambiguous referents in the raw utterance. Feeds two things:

  - The Uncertainty template (2.6): "I don't know for certain. My best
    guess is X because Y. Confidence: medium. We could verify by Z."
  - The verification loop (5.11): a low-confidence intent gets routed
    through the normal confirmation flow instead of silently executing
    a guess, catching the "confidently wrong" failure mode where the
    LLM commits to a plausible-but-wrong action plan.
"""
from __future__ import annotations

import re

from nora.config import get_config
from nora.schemas import IntentResponse

_AMBIGUOUS_REFERENTS = re.compile(r"\b(it|that|this|them|those|these)\b", re.IGNORECASE)


def estimate(intent: IntentResponse, text: str) -> float:
    """Return a 0.0-1.0 confidence score for a parsed intent. 1.0 = certain."""
    if intent.error:
        return 0.1
    if not intent.steps:
        return 1.0 if intent.response else 0.3

    from nora import command_engine

    known = command_engine.get_available_actions()
    if any(s.action not in known for s in intent.steps):
        return 0.2

    score = 1.0
    words = text.split()
    if len(words) < 6 and _AMBIGUOUS_REFERENTS.search(text):
        # Short utterance leaning on a pronoun with no antecedent in the
        # same turn ("delete it", "close that") is a frequent misfire source.
        score -= 0.35
    if len(intent.steps) >= 4:
        score -= 0.15  # long multi-step guesses have more places to go wrong
    return max(0.0, min(1.0, score))


def needs_clarification(confidence: float) -> bool:
    threshold = float(get_config().get("confidence", {}).get("clarify_below", 0.4))
    return confidence < threshold


def confidence_label(confidence: float) -> str:
    if confidence < 0.4:
        return "low"
    if confidence < 0.75:
        return "medium"
    return "high"


def uncertainty_response(guess: str, reason: str, confidence: float, verify_hint: str = "") -> str:
    """Codex's Uncertainty template (2.6)."""
    parts = [f"I don't know for certain. My best guess is {guess} because {reason}. "
             f"Confidence: {confidence_label(confidence)}."]
    if verify_hint:
        parts.append(f"We could verify by {verify_hint}.")
    return " ".join(parts)

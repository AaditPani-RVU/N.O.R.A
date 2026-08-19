"""Post-action explanation cards — Codex integration 5.3 (see CODEX_INTEGRATION.md).

Every executed turn gets a structured card: what NORA thought the user
wanted, what it did, what it skipped, and how to reverse it. Built
entirely from data already on hand (the parsed intent, execution
results, confidence) — zero extra cost when unused, available on demand
via the "why did you do that" voice command.

Kept in memory only (last N turns) — a UX surface, not a ledger;
audit_log.py already persists the durable record.
"""
from __future__ import annotations

import threading
from typing import Any

from nora.schemas import IntentResponse, StepResult

_lock = threading.Lock()
_cards: list[dict[str, Any]] = []
_MAX_CARDS = 20


def build(
    text: str,
    intent: IntentResponse,
    results: list[StepResult],
    confidence: float,
) -> dict[str, Any]:
    """Assemble and store a post-action card for the turn just completed."""
    from nora import confidence as _confidence
    from nora import reversible

    did = [f"{r.action} ({'ok' if r.success else 'failed'})" for r in results]
    executed = {r.action for r in results}
    skipped = [s.action for s in intent.steps if s.action not in executed]

    reversal = "not reversible"
    recent = reversible.get_recent_reversible(minutes=1)
    if recent:
        reversal = f"say 'undo last action' to revert: {recent[0]['description']}"

    card: dict[str, Any] = {
        "text": text,
        "thought": intent.intent,
        "did": did,
        "skipped": skipped,
        "confidence": _confidence.confidence_label(confidence),
        "reversal": reversal,
    }
    with _lock:
        _cards.insert(0, card)
        del _cards[_MAX_CARDS:]
    return card


def last() -> dict[str, Any] | None:
    with _lock:
        return _cards[0] if _cards else None


def explain_last() -> str:
    """Natural-language rendering of the most recent card."""
    card = last()
    if not card:
        return "I haven't done anything yet this session."
    parts = [f"You said '{card['text']}'. I understood that as: {card['thought']}."]
    if card["did"]:
        parts.append("What I did: " + "; ".join(card["did"]) + ".")
    if card["skipped"]:
        parts.append("What I skipped: " + ", ".join(card["skipped"]) + ".")
    parts.append(f"Confidence: {card['confidence']}. {card['reversal'].capitalize()}.")
    return " ".join(parts)

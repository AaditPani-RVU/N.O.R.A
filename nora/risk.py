"""Risk aggregation — Codex integration 2.2 (see CODEX_INTEGRATION.md).

Scores an intent across independent dimensions and aggregates by the
Codex rule: final risk = MAX across dimensions, never the average.
A single critical factor escalates the whole action.

Dimensions (each 0-4):
  destructiveness — from CommandMeta.risk on each step
  scale           — number of entities affected (files, apps, messages)
  uncertainty     — unknown actions / parse ambiguity signals
No external dependencies; pure function over the intent + registry metadata.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from nora.schemas import IntentResponse

logger = logging.getLogger("nora.risk")

# CommandMeta.risk string -> numeric destructiveness
_RISK_MAP = {"low": 1, "medium": 2, "high": 3}

# Parameter keywords that imply broad scale regardless of counts
_BROAD_SCALE_WORDS = ("all", "every", "everything", "*", "recursive")

# Numeric parameter above this many affected entities bumps scale
_SCALE_MANY = 20
_SCALE_SOME = 5


@dataclass
class Risk:
    destructiveness: int = 0
    scale: int = 0
    uncertainty: int = 0

    def level(self) -> int:
        """Codex aggregation rule: highest single dimension wins."""
        return max(self.destructiveness, self.scale, self.uncertainty)


def _step_destructiveness(action: str) -> int:
    from nora import command_engine

    meta = command_engine.get_action_meta(action)
    if meta is None:
        return 3  # unknown action: treat as high until proven registered
    base = _RISK_MAP.get(meta.risk, 2)
    if meta.requires_confirmation:
        base = max(base, 3)
    return base


def _step_scale(params: dict) -> int:
    scale = 0
    for value in params.values():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            if value >= _SCALE_MANY:
                scale = max(scale, 3)
            elif value >= _SCALE_SOME:
                scale = max(scale, 2)
        elif isinstance(value, str):
            low = value.lower()
            if any(w in low.split() or w == low for w in _BROAD_SCALE_WORDS):
                scale = max(scale, 3)
        elif isinstance(value, (list, tuple)):
            if len(value) >= _SCALE_MANY:
                scale = max(scale, 3)
            elif len(value) >= _SCALE_SOME:
                scale = max(scale, 2)
    return scale


def assess(intent: IntentResponse) -> Risk:
    """Score an intent. Cheap, synchronous, safe to call on every command."""
    from nora import command_engine

    r = Risk()
    unknown = 0
    for step in intent.steps:
        r.destructiveness = max(r.destructiveness, _step_destructiveness(step.action))
        r.scale = max(r.scale, _step_scale(step.parameters))
        if command_engine.get_action_meta(step.action) is None:
            unknown += 1

    if unknown:
        r.uncertainty = max(r.uncertainty, 3)
    if len(intent.steps) >= 8:
        # very long plans compound per-step error probability
        r.uncertainty = max(r.uncertainty, 2)

    logger.debug(
        "risk: destructiveness=%d scale=%d uncertainty=%d -> %d",
        r.destructiveness, r.scale, r.uncertainty, r.level(),
    )
    return r

"""Persona Calibration System — user-tunable communication style for NORA.

Settings are persisted in ``nora_persona.json`` and injected into the system
prompt on every LLM call.  Defaults mirror NORA's original behaviour so
existing users see no change until they explicitly tune it.

Available dimensions
--------------------
verbosity   concise | normal | detailed
tone        casual | professional | technical
style       direct | conversational
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("nora.persona")

_PERSONA_FILE = Path("nora_persona.json")

VALID: dict[str, list[str]] = {
    "verbosity": ["concise", "normal", "detailed"],
    "tone":      ["casual", "professional", "technical"],
    "style":     ["direct", "conversational"],
}

_DEFAULTS: dict[str, str] = {
    "verbosity": "normal",
    "tone":      "professional",
    "style":     "direct",
}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _load() -> dict[str, str]:
    if _PERSONA_FILE.exists():
        try:
            raw = json.loads(_PERSONA_FILE.read_text())
            merged = dict(_DEFAULTS)
            for k, v in raw.items():
                if k in VALID and v in VALID[k]:
                    merged[k] = v
            return merged
        except Exception:
            pass
    return dict(_DEFAULTS)


def _save(settings: dict[str, str]) -> None:
    try:
        _PERSONA_FILE.write_text(json.dumps(settings, indent=2))
    except Exception as exc:
        logger.error("Failed to save persona: %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get() -> dict[str, str]:
    """Return current persona settings (always includes all three dimensions)."""
    return _load()


def update(**kwargs: str) -> dict[str, str]:
    """Update one or more settings, validate, and persist.  Returns new state."""
    current = _load()
    for k, v in kwargs.items():
        if k not in VALID:
            logger.warning("Unknown persona dimension: %s", k)
            continue
        if v.lower() not in VALID[k]:
            logger.warning("Invalid value for persona.%s: %r (must be one of %s)", k, v, VALID[k])
            continue
        current[k] = v.lower()
    _save(current)
    return current


def reset() -> dict[str, str]:
    """Reset all persona settings to factory defaults."""
    _save(dict(_DEFAULTS))
    return dict(_DEFAULTS)


def format_for_prompt() -> str:
    """Return a compact block suitable for appending to the LLM system prompt."""
    p = _load()
    verbosity = p["verbosity"]
    tone = p["tone"]
    style = p["style"]

    lines: list[str] = ["PERSONA SETTINGS (apply to every spoken response):"]

    if verbosity == "concise":
        lines.append("- Keep all spoken responses brief: one to two sentences maximum.")
    elif verbosity == "detailed":
        lines.append("- Give thorough responses. Explain what you are doing and why.")
    else:
        lines.append("- Use normal verbosity: complete but not verbose.")

    if tone == "casual":
        lines.append("- Use a casual, friendly tone. Contractions and informal language are fine.")
    elif tone == "technical":
        lines.append("- Use precise technical language. Do not simplify jargon.")
    else:
        lines.append("- Use a professional tone: clear, respectful, and efficient.")

    if style == "conversational":
        lines.append("- Be conversational and personable.")
    else:
        lines.append("- Be direct: state results immediately, then any relevant context.")

    return "\n".join(lines)

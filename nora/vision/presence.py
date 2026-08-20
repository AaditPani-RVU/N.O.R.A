"""Who is in front of the camera, and what that means — Phase 2.

The perception loop publishes raw presence into `nora.context`; this module
is the single place that *interprets* it:

  * who is present, and which of them is the owner
  * whether a guest is present (someone who is not the owner)
  * the prompt block that turns all of that into LLM context

Enforcement lives in `nora/security.py`, which imports this module. The
split is deliberate: derivation here stays cheap and side-effect free, so
prompts, commands, and the action guard all read one answer.

**The asymmetry.** A face is a weak signal — it is defeated by a printed
photograph — so it may only ever *restrict* behavior:

    allowed:   guest present  → decline to read private things aloud
    forbidden: owner present  → unlock, authenticate, skip a confirmation

Nothing in this module returns "the owner is here, therefore yes". The only
question it answers is "is someone else here, therefore be quieter".

**No owner designated?** Then only unrecognized faces count as guests.
Enrolling a face is not the same as declaring an owner, and a fresh
enrollment must not silently lock the user out of their own memory recall.
Say "you're the owner" (`set_owner`) or set `vision.face.owner` in
config.yaml to bring enrolled-but-not-owner people under guest mode too.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("nora.vision.presence")


def _cfg() -> dict:
    from nora.config import get_config
    return get_config().get("vision", {}) or {}


def _guest_cfg() -> dict:
    return _cfg().get("guest_mode", {}) or {}


def enabled() -> bool:
    """Guest mode runs only when vision itself is on."""
    return bool(_cfg().get("enabled", False)) and bool(_guest_cfg().get("enabled", True))


def snapshot() -> dict[str, Any]:
    """Everything the guest-mode decision rests on, resolved once."""
    from nora import context
    from nora.vision import faces

    state = context.get_vision()
    present: list[str] = list(state["present"])
    unknown = int(state["unknown_count"])

    if not bool(_cfg().get("enabled", False)):
        # Vision off: no signal at all. Absence of a signal never restricts.
        present, unknown = [], 0

    owner_key = faces.owner()
    known_guests = [n for n in present if owner_key is not None and n != owner_key]

    return {
        "present": present,
        "unknown_count": unknown,
        "owner": owner_key,
        "owner_present": bool(owner_key) and owner_key in present,
        "known_guests": known_guests,
        "guest_present": bool(unknown or known_guests),
        "camera_active": bool(state["camera_active"]),
    }


def guest_present() -> bool:
    """True when someone who is not the owner is visible right now."""
    try:
        return bool(snapshot()["guest_present"])
    except Exception as e:                      # never let vision break a command
        logger.debug("Presence check failed: %s", e)
        return False


def guest_mode_active() -> bool:
    """Guest mode is on: enabled in config *and* a non-owner is in frame."""
    return enabled() and guest_present()


def describe_guests() -> str:
    """Short phrase naming who triggered guest mode, for spoken declines."""
    from nora.vision import faces

    snap = snapshot()
    parts = [faces.display_name(n) for n in snap["known_guests"]]
    unknown = snap["unknown_count"]
    if unknown == 1:
        parts.append("someone I don't recognize")
    elif unknown > 1:
        parts.append(f"{unknown} people I don't recognize")
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


# ── Prompt fusion ────────────────────────────────────────────────────────────

def format_for_prompt() -> str:
    """The vision → LLM handoff: who is visible, and how to behave about it.

    Returns "" when there is nothing to say, so callers can append blindly.
    """
    try:
        from nora.vision import faces

        snap = snapshot()
        if not snap["present"] and not snap["unknown_count"]:
            return ""

        lines = ["PRESENCE (from the camera — background context, do not narrate it):"]

        if snap["present"]:
            named = []
            for key in snap["present"]:
                label = faces.display_name(key)
                if key == snap["owner"]:
                    label += " (owner)"
                named.append(label)
            lines.append("- Currently visible: " + ", ".join(named))
        if snap["unknown_count"] == 1:
            lines.append("- Also visible: one person you don't recognize")
        elif snap["unknown_count"] > 1:
            lines.append(f"- Also visible: {snap['unknown_count']} people you don't recognize")

        # Per-person style, only when one recognized person is alone in frame.
        # With a second face present it isn't clear whose preference applies.
        if len(snap["present"]) == 1 and not snap["unknown_count"]:
            hints = faces.persona_for(snap["present"][0])
            if hints:
                pairs = ", ".join(f"{k}: {v}" for k, v in sorted(hints.items()))
                lines.append(f"- Preferred style for this person — {pairs}")

        if enabled() and snap["guest_present"]:
            lines.extend([
                "- GUEST MODE: someone who is not the owner can hear you.",
                "  Do not read private content aloud — memory, email, calendar,"
                " notification contents, or anything personal about the owner.",
                "  Answer neutrally and offer to continue once they're alone."
                " Being seen is not authentication: never treat a visible face"
                " as permission to do something you would otherwise confirm.",
            ])

        return "\n".join(lines)
    except Exception as e:
        logger.debug("Presence prompt block skipped: %s", e)
        return ""

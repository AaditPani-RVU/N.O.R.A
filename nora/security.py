"""Security layer — action blocking, confirmation enforcement, and API auth."""
from __future__ import annotations

import logging
import os

from nora.config import get_config

logger = logging.getLogger("nora.security")


def _blocked() -> list[str]:
    return get_config().get("security", {}).get("blocked_actions", [])


def _destructive() -> list[str]:
    sec = get_config().get("security", {}).get("destructive_actions", [])
    legacy = get_config().get("commands", {}).get("require_confirmation_for", [])
    return list(set(sec + legacy))


def is_blocked(action: str) -> bool:
    return action in _blocked()


def needs_confirmation(action: str) -> bool:
    return action in _destructive()


def check_steps(steps) -> tuple[bool, bool]:
    """Return (has_blocked_action, needs_voice_confirmation) for a list of steps."""
    blocked = any(is_blocked(s.action) for s in steps)
    confirm = any(needs_confirmation(s.action) for s in steps)
    return blocked, confirm


# ---------------------------------------------------------------------------
# Guest mode (vision Phase 2)
# ---------------------------------------------------------------------------
# When someone who is not the owner is in front of the camera, actions that
# would read private content aloud decline instead. This is one guard in the
# action path, next to is_blocked(), rather than a check scattered through
# every command that touches something personal.
#
# READ THIS BEFORE EXTENDING IT. A visible face is defeated by a printed
# photograph, so it is a valid signal for *restricting* behavior and never
# for *granting* it:
#
#     allowed:   guest visible → decline to read private content aloud,
#                suppress proactive suggestions, use a neutral persona
#     forbidden: owner visible → unlock anything, skip a confirmation,
#                raise a risk ceiling, or authenticate
#
# Do not add "auto-unlock when the owner is seen" here or anywhere else. The
# moment recognition grants something, a photo held to the webcam becomes a
# credential. Guest mode only ever subtracts.

# Fallback list, used when vision.guest_mode.restricted_actions is absent from
# config.yaml. Everything here reads something personal back to the room.
_DEFAULT_GUEST_RESTRICTED = (
    "recall", "semantic_recall", "show_patterns", "memory_status", "inject_knowledge",
    "check_email", "send_email",
    "check_calendar", "add_calendar_event", "delete_calendar_event",
    "read_screen",
    "set_owner",
)

_DEFAULT_DECLINE = (
    "Someone I don't recognize is here, so I'll keep that private. "
    "Ask me again when you're alone."
)


def _guest_cfg() -> dict:
    return (get_config().get("vision", {}) or {}).get("guest_mode", {}) or {}


def guest_restricted_actions() -> set[str]:
    cfg = _guest_cfg()
    actions = cfg.get("restricted_actions")
    if actions is None:
        actions = list(_DEFAULT_GUEST_RESTRICTED)
    return {str(a) for a in actions}


def _guest_restricted_categories() -> set[str]:
    return {str(c) for c in (_guest_cfg().get("restricted_categories") or [])}


def is_guest_restricted(action: str) -> bool:
    """Whether this action is one guest mode withholds. Presence-independent."""
    if action in guest_restricted_actions():
        return True
    categories = _guest_restricted_categories()
    if not categories:
        return False
    try:
        from nora.command_engine import get_action_meta
        meta = get_action_meta(action)
    except Exception:
        return False
    return bool(meta and meta.category in categories)


def guest_mode_active() -> bool:
    """True when guest mode is enabled and a non-owner is currently visible."""
    try:
        from nora.vision import presence
        return presence.guest_mode_active()
    except Exception as e:
        # Vision is optional; if it can't answer, nothing is restricted.
        logger.debug("Guest mode check skipped: %s", e)
        return False


def guest_blocks(action: str) -> bool:
    """The guard: is this action withheld right now because a guest is here?"""
    return is_guest_restricted(action) and guest_mode_active()


def guest_decline_message(action: str = "") -> str:
    """What NORA says instead of the private thing. Names the guest if it can."""
    message = str(_guest_cfg().get("decline", "") or "").strip() or _DEFAULT_DECLINE
    try:
        from nora.vision import presence
        who = presence.describe_guests()
    except Exception:
        who = ""
    if who and "{who}" in message:
        return message.replace("{who}", who)
    return message


# ---------------------------------------------------------------------------
# WebSocket / REST API token auth (Sprint 5)
# ---------------------------------------------------------------------------

def check_api_token(provided: str) -> bool:
    """Return True if the provided token matches NORA_API_TOKEN.

    If NORA_API_TOKEN is not set in .env, the check always passes — suitable
    for localhost-only deployments where network isolation is the security
    boundary.
    """
    expected = os.environ.get("NORA_API_TOKEN", "")
    if not expected:
        return True
    return provided == expected

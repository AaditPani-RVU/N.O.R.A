"""Security layer â€” action blocking and mandatory confirmation enforcement."""
from __future__ import annotations

import logging
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

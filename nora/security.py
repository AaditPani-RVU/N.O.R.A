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

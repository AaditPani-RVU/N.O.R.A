"""Push-to-talk mode control â€” voice-togglable in real time.

When PTT is ON  â†’ listener records only while PTT key/UI button is held.
When PTT is OFF â†’ listener uses passive wake detection (clap + wake phrase).

Changes apply instantly to the next listen() cycle. No restart needed.
"""
from __future__ import annotations

import logging

from nora import context
from nora.command_engine import register

logger = logging.getLogger("nora.commands.ptt_control")


@register("set_ptt_mode")
def set_ptt_mode(enabled: bool) -> str:
    """Enable or disable push-to-talk mode at runtime."""
    enabled = _coerce_bool(enabled)
    context.set_ptt_enabled(enabled)
    _notify_ui(enabled)
    logger.info("PTT mode set to %s", enabled)
    return "Push to talk enabled." if enabled else "Push to talk disabled. Passive listening active."


@register("get_ptt_mode")
def get_ptt_mode() -> str:
    enabled = context.get_ptt_enabled()
    return "Push to talk is on." if enabled else "Push to talk is off."


def _coerce_bool(value) -> bool:  # noqa: ANN001
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enable", "enabled"}
    return bool(value)


def _notify_ui(enabled: bool) -> None:
    try:
        from nora import ui_server
        ui_server.notify_ptt_mode(enabled)
    except Exception:
        pass

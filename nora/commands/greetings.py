from __future__ import annotations

import logging

from nora.command_engine import register
from nora.commands.system_info import get_system_info, get_time

logger = logging.getLogger("nora.commands.greetings")


@register("daddys_home")
def daddys_home() -> str:
    """Respond to the 'daddy's home' activation phrase with a status briefing."""
    time_info = get_time()
    sys_info = get_system_info()
    return (
        f"Welcome back sir. {time_info} "
        f"All systems are nominal. {sys_info}. "
        "Ready for your orders."
    )

"""Mouse and keyboard synthesis via ydotool (Wayland + X11 compatible)."""
from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger("nora.platform.ydotool")


def _run(*args: str) -> bool:
    try:
        result = subprocess.run(["ydotool", *args], capture_output=True, timeout=5)
        if result.returncode != 0:
            logger.warning("ydotool %s failed: %s", args, result.stderr.decode())
        return result.returncode == 0
    except FileNotFoundError:
        logger.error("ydotool not installed. Run: sudo apt install ydotool")
        return False
    except subprocess.TimeoutExpired:
        logger.error("ydotool timed out")
        return False


def click(x: int, y: int, button: int = 1) -> bool:
    """Click at absolute screen coordinates."""
    return _run("mousemove", "--absolute", f"{x}", f"{y}") and \
           _run("click", f"0x{button:02X}0", f"0x{button:02X}1")


def type_text(text: str) -> bool:
    """Type a string via keyboard synthesis."""
    return _run("type", "--", text)


def key(keys: str) -> bool:
    """Press a key combo, e.g. 'ctrl+l', 'Return', 'Tab'."""
    return _run("key", keys)

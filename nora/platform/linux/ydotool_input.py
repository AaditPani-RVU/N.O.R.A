"""Mouse and keyboard synthesis via ydotool (Wayland + X11 compatible)."""
from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger("nora.platform.ydotool")


def _run(*args: str) -> bool:
    try:
        result = subprocess.run(["ydotool", *args], capture_output=True, timeout=5)
        if result.returncode != 0:
            logger.warning("ydotool %s failed: %s", args, result.stderr.decode(errors="replace"))
        return result.returncode == 0
    except FileNotFoundError:
        logger.error("ydotool not installed. Run: sudo apt install ydotool")
        return False
    except subprocess.TimeoutExpired:
        logger.error("ydotool timed out")
        return False


def _xdotool_click(x: int, y: int) -> bool:
    """xdotool fallback for X11 sessions."""
    try:
        result = subprocess.run(
            ["xdotool", "mousemove", "--sync", str(x), str(y), "click", "1"],
            capture_output=True, timeout=5,
        )
        if result.returncode != 0:
            logger.warning("xdotool click failed: %s", result.stderr.decode(errors="replace"))
        return result.returncode == 0
    except FileNotFoundError:
        logger.error("Neither ydotool nor xdotool found. Install: sudo apt install ydotool")
        return False
    except subprocess.TimeoutExpired:
        logger.error("xdotool timed out")
        return False


def _ydotool_button_code(button: int) -> str:
    """Convert X11 button number to ydotool click bitmask (press+release = 0xC0 | btn_idx)."""
    x11_to_ydotool = {1: 0x00, 2: 0x02, 3: 0x01}
    idx = x11_to_ydotool.get(button, 0x00)
    return f"0x{0xC0 | idx:02X}"


def click(x: int, y: int, button: int = 1) -> bool:
    """Click at absolute screen coordinates. Tries ydotool first, xdotool on X11 as fallback."""
    if _run("mousemove", "--absolute", f"{x}", f"{y}") and \
       _run("click", _ydotool_button_code(button)):
        return True
    return _xdotool_click(x, y)


def type_text(text: str) -> bool:
    """Type a string via keyboard synthesis."""
    return _run("type", "--", text)


def key(keys: str) -> bool:
    """Press a key combo, e.g. 'ctrl+l', 'Return', 'Tab'."""
    return _run("key", keys)

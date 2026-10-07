from __future__ import annotations

import logging
import time

from nora.command_engine import register

logger = logging.getLogger("nora.commands.typing_commands")

# pyautogui needs an X display. The core runs under systemd on Wayland with
# none, where importing it raises; type_into_focused and press_key (ydotool)
# cover typing there, so these two are simply not offered.
try:
    import pyautogui
except Exception as exc:  # KeyError/OSError from Xlib, ImportError
    pyautogui = None
    logger.debug("pyautogui unavailable (%s); type_text and press_keys not registered", exc)
else:
    # Safety: pyautogui failsafe (move mouse to corner to abort)
    pyautogui.FAILSAFE = True

if pyautogui is not None:
    @register("type_text", sig="type_text(text: str)", category="system")
    def type_text(text: str) -> str:
        """Type text at the current cursor position."""
        time.sleep(0.3)  # Brief delay to ensure focus
        pyautogui.typewrite(text, interval=0.02)
        return f"Typed: {text}"

    @register("press_keys", sig="press_keys(keys: str)", description='e.g. "ctrl+s", "alt+tab", "enter"', category="system")
    def press_keys(keys: str) -> str:
        """Press a key combination (e.g., 'ctrl+s', 'alt+tab', 'enter')."""
        key_list = [k.strip() for k in keys.split("+")]
        time.sleep(0.2)
        pyautogui.hotkey(*key_list)
        return f"Pressed: {keys}"

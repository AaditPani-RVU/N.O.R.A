from __future__ import annotations

import logging
import time

import pyautogui

from nora.command_engine import register

logger = logging.getLogger("nora.commands.typing_commands")

# Safety: pyautogui failsafe (move mouse to corner to abort)
pyautogui.FAILSAFE = True


@register("type_text")
def type_text(text: str) -> str:
    """Type text at the current cursor position."""
    time.sleep(0.3)  # Brief delay to ensure focus
    pyautogui.typewrite(text, interval=0.02)
    return f"Typed: {text}"


@register("press_keys")
def press_keys(keys: str) -> str:
    """Press a key combination (e.g., 'ctrl+s', 'alt+tab', 'enter')."""
    key_list = [k.strip() for k in keys.split("+")]
    time.sleep(0.2)
    pyautogui.hotkey(*key_list)
    return f"Pressed: {keys}"

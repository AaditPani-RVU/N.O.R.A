from __future__ import annotations

import ctypes
import logging
import os
from datetime import datetime
from pathlib import Path

import pyautogui

from nora.command_engine import register

logger = logging.getLogger("nora.commands.system_control")


@register("set_volume")
def set_volume(level: int) -> str:
    """Set system volume (0-100) using Windows API."""
    try:
        from ctypes import cast, POINTER
        import comtypes
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))

        # pycaw uses scalar 0.0-1.0
        scalar = max(0, min(100, level)) / 100.0
        volume.SetMasterVolumeLevelScalar(scalar, None)
        return f"Volume set to {level}%."
    except ImportError:
        # Fallback: use nircmd or keystrokes
        logger.warning("pycaw not installed, using keyboard volume control")
        return "Volume control requires pycaw package. Install it with: pip install pycaw"
    except Exception as e:
        return f"Failed to set volume: {e}"


@register("take_screenshot")
def take_screenshot() -> str:
    """Take a screenshot and save it to the desktop."""
    desktop = Path.home() / "Desktop"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = desktop / f"screenshot_{timestamp}.png"
    screenshot = pyautogui.screenshot()
    screenshot.save(str(filepath))
    return f"Screenshot saved to {filepath}"


@register("lock_screen")
def lock_screen() -> str:
    """Lock the Windows workstation."""
    ctypes.windll.user32.LockWorkStation()
    return "Screen locked."

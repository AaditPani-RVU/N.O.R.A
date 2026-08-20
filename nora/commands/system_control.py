from __future__ import annotations

import ctypes
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import pyautogui

from nora.command_engine import register

logger = logging.getLogger("nora.commands.system_control")

_IS_LINUX = sys.platform.startswith("linux")


def _linux_volume():
    """Return the Linux mixer module, or None when it cannot drive anything.

    Imported lazily and guarded: a machine with no wpctl/pactl/amixer must
    degrade to a spoken explanation, not a traceback at startup.
    """
    if not _IS_LINUX:
        return None
    try:
        from nora.platform.linux import volume as _vol
    except Exception:
        logger.warning("linux volume backend unavailable", exc_info=True)
        return None
    return _vol if _vol.available() else None


def _windows_set_volume(pct: int) -> str:
    """Set the Windows master volume through pycaw."""
    try:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        volume.SetMasterVolumeLevelScalar(pct / 100.0, None)  # pycaw wants 0.0-1.0
        return f"Volume set to {pct}%."
    except ImportError:
        logger.warning("pycaw not installed -- cannot set volume on Windows")
        return "Volume control requires the pycaw package. Install it with: pip install pycaw"
    except Exception as exc:
        return f"Failed to set volume: {exc}"


@register("set_volume", sig="set_volume(level=60)",
          description="Set the system output volume to an absolute level, 0-100",
          category="system")
def set_volume(level: int) -> str:
    """Set the system output volume (0-100) on whichever platform is running."""
    try:
        pct = max(0, min(100, int(level)))
    except (TypeError, ValueError):
        return "Give me a volume between 0 and 100."

    if _IS_LINUX:
        vol = _linux_volume()
        if vol is None:
            return ("I can't reach the mixer. Install one of wireplumber, "
                    "pulseaudio-utils or alsa-utils.")
        if vol.set_volume(pct):
            return f"Volume set to {pct} percent."
        return "I couldn't change the system volume."

    return _windows_set_volume(pct)


@register("adjust_volume", sig="adjust_volume(delta=10)",
          description="Nudge the system volume up or down relative to where it is now",
          category="system")
def adjust_volume(delta: int = 10) -> str:
    """Move the volume by a relative amount.

    Separate from set_volume on purpose: "turn it up" from 90% must not mean
    "jump to some fixed level", which is what an absolute-only action forces.
    """
    try:
        step = int(delta)
    except (TypeError, ValueError):
        return "Tell me how much to change the volume by."

    if _IS_LINUX:
        vol = _linux_volume()
        if vol is None:
            return ("I can't reach the mixer. Install one of wireplumber, "
                    "pulseaudio-utils or alsa-utils.")
        new = vol.adjust_volume(step)
        if new is None:
            return "I couldn't change the system volume."
        return f"Volume {'up' if step >= 0 else 'down'} to {new} percent."

    # Windows has no cheap relative call, so read-modify-write through pycaw.
    try:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        current = round(volume.GetMasterVolumeLevelScalar() * 100)
        return _windows_set_volume(max(0, min(100, current + step)))
    except ImportError:
        return "Volume control requires the pycaw package. Install it with: pip install pycaw"
    except Exception as exc:
        return f"Failed to change volume: {exc}"


@register("mute_audio", sig="mute_audio(muted=True)",
          description="Mute or unmute system audio without losing the volume level",
          category="system")
def mute_audio(muted: bool = True) -> str:
    """Toggle the mute flag, which preserves the level underneath it."""
    if _IS_LINUX:
        vol = _linux_volume()
        if vol is None:
            return ("I can't reach the mixer. Install one of wireplumber, "
                    "pulseaudio-utils or alsa-utils.")
        if vol.set_muted(bool(muted)):
            return "Muted." if muted else "Unmuted."
        return "I couldn't change the mute state."

    # Windows: no mute flag exposed here, so fall back to dropping the level.
    return _windows_set_volume(0) if muted else _windows_set_volume(50)


@register("take_screenshot", sig="take_screenshot()", category="system")
def take_screenshot() -> str:
    """Take a screenshot and save it to the desktop."""
    desktop = Path.home() / "Desktop"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = desktop / f"screenshot_{timestamp}.png"
    screenshot = pyautogui.screenshot()
    screenshot.save(str(filepath))
    return f"Screenshot saved to {filepath}"


@register("lock_screen", sig="lock_screen()", category="system")
def lock_screen() -> str:
    """Lock the Windows workstation."""
    ctypes.windll.user32.LockWorkStation()
    return "Screen locked."

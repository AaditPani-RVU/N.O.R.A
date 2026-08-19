"""Adaptive Ambient — F5 Linux flagship (PipeWire + Wayland).

Voice commands:
  duck_app_when_speaking(app)  — sidechain music on wakeword, restore after
  denoise_mic()                — load RNNoise LADSPA filter on default mic
  tap_app_audio(app)           — create virtual sink to capture app audio
  focus_mode(intent)           — tile windows + mute notifications for intent

Wakeword hook: when "Hey NORA" fires, ducked apps auto-lower to 20% for 8s.
Install: sudo apt install pipewire wireplumber libndi-rnnoise0 && pip install i3ipc
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

from nora.command_engine import register

logger = logging.getLogger("nora.commands.ambient_linux")

# app_name → original_volume (for restore)
_ducked_apps: dict[str, float] = {}
_duck_restore_timers: dict[str, threading.Timer] = {}

# Duration (seconds) to keep music ducked after wakeword
_DUCK_DURATION = 8.0


def _pw():
    from nora.platform.linux import pipewire_graph
    return pipewire_graph


def _comp():
    from nora.platform.linux import compositor_ipc
    return compositor_ipc


def _do_duck(app_name: str) -> None:
    pw = _pw()
    node = pw.find_sink_input(app_name)
    if node:
        pw.duck_app(app_name, 0.2)
        _ducked_apps[app_name] = 1.0  # track original volume


def _do_restore(app_name: str) -> None:
    _ducked_apps.pop(app_name, None)
    _duck_restore_timers.pop(app_name, None)
    _pw().restore_volume(app_name)


def _schedule_restore(app_name: str) -> None:
    existing = _duck_restore_timers.pop(app_name, None)
    if existing:
        existing.cancel()
    t = threading.Timer(_DUCK_DURATION, _do_restore, args=[app_name])
    t.daemon = True
    t.start()
    _duck_restore_timers[app_name] = t


# ── Wakeword sidechain hook ───────────────────────────────────────────────────

# Apps registered for ducking on wakeword
_duck_on_wake: list[str] = []


def _on_wake_callback() -> None:
    for app in _duck_on_wake:
        _do_duck(app)
        _schedule_restore(app)


def register_with_wakeword() -> None:
    """Wire the ducking callback into wakeword.py. Call at startup."""
    try:
        from nora import wakeword
        wakeword.register_on_wake_callback(_on_wake_callback)
        logger.debug("ambient_linux duck-on-wake registered with wakeword")
    except Exception as e:
        logger.debug("Could not register with wakeword: %s", e)


# ── Voice Commands ────────────────────────────────────────────────────────────

@register(
    "duck_app_when_speaking",
    sig="duck_app_when_speaking(app: str)",
    description="Lower the volume of an app (e.g. 'spotify') automatically when you say the wakeword, then restore it.",
    risk="low",
    category="focus",
)
async def duck_app_when_speaking(app: str) -> str:
    if not _pw().is_available():
        return "PipeWire is not running. Is pipewire-pulse installed and active?"
    if app.lower() in _duck_on_wake:
        return f"'{app}' is already configured to duck on wakeword."
    _duck_on_wake.append(app.lower())
    return (
        f"Configured: '{app}' will duck to 20% volume when you say the wakeword, "
        f"then restore after {_DUCK_DURATION:.0f} seconds."
    )


@register(
    "stop_ducking",
    sig="stop_ducking(app: str)",
    description="Stop automatically ducking an app's volume on wakeword.",
    risk="low",
    category="focus",
)
async def stop_ducking(app: str) -> str:
    app_lower = app.lower()
    if app_lower in _duck_on_wake:
        _duck_on_wake.remove(app_lower)
        _do_restore(app_lower)
        return f"'{app}' will no longer be ducked on wakeword."
    return f"'{app}' is not in the duck list."


@register(
    "denoise_mic",
    sig="denoise_mic()",
    description="Load an RNNoise denoising filter on the default microphone to remove background noise.",
    risk="low",
    category="focus",
)
async def denoise_mic() -> str:
    loop = asyncio.get_event_loop()
    pw = _pw()
    if not pw.is_available():
        return "PipeWire is not running."

    if await loop.run_in_executor(None, pw.is_rnnoise_active):
        return "Denoising is already active. Use 'NORA stop denoising' to turn it off."

    ok = await loop.run_in_executor(None, pw.load_rnnoise_filter)
    if ok:
        return (
            "Noise suppression config written. Reload PipeWire to activate: "
            "run 'systemctl --user restart pipewire' — audio will cut for about one second. "
            "After that, select 'NORA Denoised Mic' as your input source."
        )
    return "Could not write noise suppression config. Check ~/.config/pipewire/pipewire.conf.d/ permissions."


@register(
    "tap_app_audio",
    sig="tap_app_audio(app: str)",
    description="Create a virtual audio sink to capture and monitor an app's audio output (e.g. for transcription).",
    risk="low",
    category="focus",
)
async def tap_app_audio(app: str) -> str:
    loop = asyncio.get_event_loop()
    pw = _pw()
    if not pw.is_available():
        return "PipeWire is not running."

    sink_name = f"nora_tap_{app.replace(' ', '_')[:20]}"
    result = await loop.run_in_executor(None, pw.create_virtual_sink, sink_name)
    if result:
        return (
            f"Virtual sink '{sink_name}' created. "
            f"Route {app}'s audio output to it in your sound settings. "
            "NORA can now transcribe it."
        )
    return f"Could not create virtual sink for '{app}'. Is PipeWire running with wireplumber?"


@register(
    "focus_mode",
    sig="focus_mode(intent: str)",
    description="Switch to a focus layout for the given intent: 'coding', 'writing', or 'meeting'. Tiles relevant windows and mutes notifications.",
    risk="medium",
    category="focus",
)
async def focus_mode(intent: str) -> str:
    valid_intents = {"coding", "writing", "meeting"}
    if intent not in valid_intents:
        return f"Unknown focus intent '{intent}'. Choose one of: {', '.join(sorted(valid_intents))}."

    loop = asyncio.get_event_loop()
    comp = _comp()
    compositor = comp.get_compositor()

    ok = await loop.run_in_executor(None, comp.focus_mode, intent)
    muted = await loop.run_in_executor(None, comp.mute_notifications)

    if ok:
        return (
            f"Focus mode '{intent}' activated on {compositor}. "
            f"Relevant windows tiled"
            + (", notifications muted." if muted else ".")
        )
    return (
        f"Could not activate focus mode '{intent}' on compositor '{compositor}'. "
        "Supported compositors: Sway, Hyprland, KDE Plasma."
    )


@register(
    "leave_focus_mode",
    sig="leave_focus_mode()",
    description="Restore normal window layout and re-enable notifications.",
    risk="low",
    category="focus",
)
async def leave_focus_mode() -> str:
    loop = asyncio.get_event_loop()
    comp = _comp()
    await loop.run_in_executor(None, comp.unmute_notifications)
    return "Notifications restored. Focus mode deactivated."

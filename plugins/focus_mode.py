"""NORA plugin: focus mode and Pomodoro timer.

Voice commands:
  "focus mode"                       → focus_mode(duration_minutes=25)
  "focus mode for 90 minutes"        → focus_mode(duration_minutes=90)
  "end focus"                        → end_focus()
  "start a pomodoro"                 → pomodoro()
  "pomodoro 50 minutes"              → pomodoro(minutes=50)
  "stop pomodoro"                    → stop_pomodoro()
"""
from __future__ import annotations

import subprocess
import threading

from nora.command_engine import register
import nora.speaker as speaker

_DISTRACTING_APPS = [
    "Discord.exe",
    "Slack.exe",
    "WhatsApp.exe",
    "Teams.exe",
    "Telegram.exe",
    "Instagram.exe",
]

_focus_timer: threading.Timer | None = None
_pomodoro_thread: threading.Thread | None = None
_pomodoro_stop = threading.Event()


@register("focus_mode")
def focus_mode(duration_minutes: int = 25) -> str:
    global _focus_timer

    if _focus_timer and _focus_timer.is_alive():
        _focus_timer.cancel()

    killed = []
    for app in _DISTRACTING_APPS:
        result = subprocess.run(["taskkill", "/F", "/IM", app], capture_output=True)
        if result.returncode == 0:
            killed.append(app.replace(".exe", ""))

    def _on_end():
        speaker.speak("Focus session complete. Great work.")

    _focus_timer = threading.Timer(duration_minutes * 60, _on_end)
    _focus_timer.daemon = True
    _focus_timer.start()

    msg = f"Focus mode active for {duration_minutes} minutes."
    if killed:
        msg += f" Closed {', '.join(killed)}."
    return msg


@register("end_focus")
def end_focus() -> str:
    global _focus_timer
    if _focus_timer and _focus_timer.is_alive():
        _focus_timer.cancel()
        _focus_timer = None
        return "Focus mode ended early."
    return "No active focus session."


def _pomodoro_loop(work_min: int, break_min: int) -> None:
    session = 1
    while not _pomodoro_stop.is_set():
        speaker.speak(f"Pomodoro session {session}. Focus for {work_min} minutes.")
        if _pomodoro_stop.wait(work_min * 60):
            break
        speaker.speak(f"Session {session} complete. Take a {break_min} minute break.")
        if _pomodoro_stop.wait(break_min * 60):
            break
        session += 1


@register("pomodoro")
def pomodoro(minutes: int = 25, break_minutes: int = 5) -> str:
    global _pomodoro_thread, _pomodoro_stop

    if _pomodoro_thread and _pomodoro_thread.is_alive():
        _pomodoro_stop.set()
        _pomodoro_thread.join(timeout=2)

    _pomodoro_stop = threading.Event()
    _pomodoro_thread = threading.Thread(
        target=_pomodoro_loop,
        args=(minutes, break_minutes),
        daemon=True,
        name="nora-pomodoro",
    )
    _pomodoro_thread.start()
    return f"Pomodoro started — {minutes} minute sessions, {break_minutes} minute breaks."


@register("stop_pomodoro")
def stop_pomodoro() -> str:
    _pomodoro_stop.set()
    return "Pomodoro stopped."

"""Shared pipeline context state.

Thread-safe runtime state readable/writable from anywhere in the system.
Used by the listener, speaker, music commands, UI server, and intent parser
to coordinate without tight coupling.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

_lock = threading.RLock()


# ── Wake trigger ─────────────────────────────────────────────────────────────

# True only during the single command cycle that follows wake detection.
# Pipeline clears this after execute() returns.
wake_triggered: bool = False


# ── Push-to-talk mode ────────────────────────────────────────────────────────

# When True:  listener only records while PTT key/button is held.
# When False: listener uses passive wake detection (clap / wake phrase).
# Toggled in real time by the "enable/disable push to talk" voice commands.
_ptt_enabled: bool = True


def get_ptt_enabled() -> bool:
    with _lock:
        return _ptt_enabled


def set_ptt_enabled(value: bool) -> None:
    global _ptt_enabled
    with _lock:
        _ptt_enabled = bool(value)


# ── Music state ──────────────────────────────────────────────────────────────

@dataclass
class MusicState:
    track: str = ""
    artist: str = ""
    source: str = ""          # "local" | "apple_music_com" | "apple_music_web" | "youtube"
    status: str = "stopped"   # "playing" | "paused" | "stopped"
    last_track: str = ""      # preserved across stop so "resume music" works
    last_artist: str = ""
    last_source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "track": self.track,
            "artist": self.artist,
            "source": self.source,
            "status": self.status,
        }


music: MusicState = MusicState()


def update_music(**kwargs: Any) -> None:
    """Thread-safe update of music state. Also stamps last_* on stop."""
    with _lock:
        for k, v in kwargs.items():
            if hasattr(music, k):
                setattr(music, k, v)
        if music.status == "stopped" and music.track:
            music.last_track = music.track
            music.last_artist = music.artist
            music.last_source = music.source


def get_music() -> dict[str, Any]:
    with _lock:
        return music.to_dict()


# ── Recent commands (for productivity / memory) ──────────────────────────────

_recent_commands: list[dict[str, Any]] = []
_MAX_RECENT = 20


def record_command(text: str, intent: str, actions: list[str]) -> None:
    with _lock:
        _recent_commands.insert(0, {
            "text": text,
            "intent": intent,
            "actions": actions,
            "ts": time.time(),
        })
        del _recent_commands[_MAX_RECENT:]


def recent_commands() -> list[dict[str, Any]]:
    with _lock:
        return list(_recent_commands)


# ── Active apps hint (lightly tracked — filled when we open/close) ───────────

_active_apps: set[str] = set()


def mark_app_opened(name: str) -> None:
    with _lock:
        _active_apps.add(name.lower())


def mark_app_closed(name: str) -> None:
    with _lock:
        _active_apps.discard(name.lower())


def active_apps() -> list[str]:
    with _lock:
        return sorted(_active_apps)


# ── Cancellation signal (for interruption handling) ──────────────────────────

_cancel_event = threading.Event()


def request_cancel() -> None:
    _cancel_event.set()


def clear_cancel() -> None:
    _cancel_event.clear()


def is_cancelled() -> bool:
    return _cancel_event.is_set()

"""Focus / attention model — Codex integration 5.5 (see CODEX_INTEGRATION.md).

Classifies the user's attention state so NORA knows *when* to speak, not
just what to say. Signals come from the PipeWire graph (already a NORA
dependency on Linux): a running mic-capture stream that isn't NORA means
the user is on a call; a running playback stream means media is up.

States:
  AVAILABLE — silent desktop; speak freely.
  CALL      — another process is capturing the mic; no proactive speech.
  MEDIA     — audio playback active; defer non-critical speech.
  AWAY      — no interaction for a while; queue everything.

Fail-soft: on non-Linux, or if pw-dump is missing, every check degrades
to AVAILABLE so nothing is ever silently suppressed on a machine where
we can't observe attention.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from enum import Enum
from typing import Callable

from nora.config import get_config

logger = logging.getLogger("nora.focus")

_lock = threading.Lock()
_last_activity_ts = time.time()
_deferred: list[str] = []
_MAX_DEFERRED = 10

# pw-dump is ~50ms; cache briefly so per-command checks stay free
_CACHE_TTL_SEC = 5.0
_cache: tuple[float, "FocusState"] | None = None

# Mic-capture streams owned by NORA itself must not count as a call
_SELF_NAMES = ("nora", "python")


class FocusState(Enum):
    AVAILABLE = "available"
    CALL = "call"
    MEDIA = "media"
    AWAY = "away"


def note_activity() -> None:
    """Called by the pipeline whenever the user issues a command."""
    global _last_activity_ts
    _last_activity_ts = time.time()
    _flush()


def _pipewire_state() -> FocusState:
    if not sys.platform.startswith("linux"):
        return FocusState.AVAILABLE
    try:
        from nora.platform.linux import pipewire_graph

        nodes = pipewire_graph._pw_dump()
    except Exception as e:
        logger.debug("focus: pipewire unavailable (%s)", e)
        return FocusState.AVAILABLE

    media_playing = False
    for node in nodes:
        if node.get("type") != "PipeWire:Interface:Node":
            continue
        info = node.get("info", {})
        if info.get("state") != "running":
            continue
        props = info.get("props", {})
        media_class = props.get("media.class", "")
        app = props.get("application.name", "").lower()
        if media_class == "Stream/Input/Audio" and not any(s in app for s in _SELF_NAMES):
            return FocusState.CALL
        if media_class == "Stream/Output/Audio" and not any(s in app for s in _SELF_NAMES):
            media_playing = True
    return FocusState.MEDIA if media_playing else FocusState.AVAILABLE


def current() -> FocusState:
    """Classify the user's current attention state. Cached for a few seconds."""
    global _cache
    cfg = get_config().get("focus", {})
    if not cfg.get("enabled", True):
        return FocusState.AVAILABLE

    away_after = float(cfg.get("away_after_sec", 600))
    if time.time() - _last_activity_ts > away_after:
        return FocusState.AWAY

    with _lock:
        if _cache and time.time() - _cache[0] < _CACHE_TTL_SEC:
            return _cache[1]
    state = _pipewire_state()
    with _lock:
        _cache = (time.time(), state)
    return state


def allows_proactive_speech(state: FocusState | None = None) -> bool:
    state = state or current()
    if state is not FocusState.AVAILABLE:
        return False
    from nora import silent_hours  # 5.6 — learned quiet windows gate speech too
    return not silent_hours.is_silent_now()


_speak_fn: Callable[[str], None] | None = None


def gated(speak_fn: Callable[[str], None]) -> Callable[[str], None]:
    """Wrap a speak function so proactive speech respects the focus state.

    Suppressed messages are queued and flushed the next time the user
    interacts (note_activity), so nothing is lost — only re-timed.
    """
    global _speak_fn
    _speak_fn = speak_fn

    def _gated_speak(text: str) -> None:
        state = current()
        if allows_proactive_speech(state):
            speak_fn(text)
        else:
            logger.info("focus: deferring proactive speech (state=%s)", state.value)
            with _lock:
                _deferred.append(text)
                del _deferred[:-_MAX_DEFERRED]

    return _gated_speak


def _flush() -> None:
    global _deferred
    if not (_speak_fn and allows_proactive_speech()):
        # The user is back but still busy (on a call, inside a quiet window).
        # Leave the queue alone: draining it here spoke to nobody and lost the
        # suggestion, which contradicts "re-timed, not dropped".
        return
    with _lock:
        pending, _deferred = _deferred, []
    if pending:
        # Speak only the most recent deferred suggestion; a backlog read
        # aloud all at once is worse than losing stale ones.
        _speak_fn(pending[-1])


def status() -> str:
    """Natural-language state for the voice interface."""
    state = current()
    descriptions = {
        FocusState.AVAILABLE: "You look available — I'll speak normally.",
        FocusState.CALL: "You're on a call — I'm holding all proactive speech.",
        FocusState.MEDIA: "Media is playing — I'll keep quiet unless it's important.",
        FocusState.AWAY: "You've been away — I'm queueing suggestions until you're back.",
    }
    return descriptions[state]

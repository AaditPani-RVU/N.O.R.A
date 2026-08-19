"""Proactive Intelligence Engine.

Runs as a background daemon thread. Periodically:
  1. Analyzes current time context vs. learned behavioral patterns
  2. Checks for strong workflow predictions that haven't been triggered yet
  3. Fires a proactive suggestion callback when confidence threshold is met

Design constraints:
  - Single suggestion per 30-minute window (prevents annoyance)
  - Only fires when no command was issued in the last N seconds (user is idle)
  - Cooldown resets when user speaks
  - Thread-safe
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from datetime import datetime
from typing import Callable

logger = logging.getLogger("nora.proactive")

# â"€â"€ Configuration â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

CHECK_INTERVAL_SEC = 60          # How often to evaluate patterns
IDLE_THRESHOLD_SEC = 120         # User must be idle this long before suggestion fires
SUGGESTION_COOLDOWN_SEC = 1800   # 30 minutes between proactive suggestions
MIN_PATTERN_CONFIDENCE = 5       # Minimum occurrences before suggesting

# â"€â"€ State â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

_last_command_ts: float = time.time()
_last_suggestion_ts: float = 0.0
_running = False
_thread: threading.Thread | None = None
_lock = threading.Lock()
_callback: Callable[[str], None] | None = None


def notify_command_issued() -> None:
    """Call this every time a user command fires (resets idle timer)."""
    global _last_command_ts
    with _lock:
        _last_command_ts = time.time()


def register_callback(fn: Callable[[str], None]) -> None:
    """Register the function that speaks proactive suggestions to the user."""
    global _callback
    _callback = fn


def _is_idle() -> bool:
    with _lock:
        return (time.time() - _last_command_ts) >= IDLE_THRESHOLD_SEC


def _can_suggest() -> bool:
    with _lock:
        return (time.time() - _last_suggestion_ts) >= SUGGESTION_COOLDOWN_SEC


def _mark_suggested() -> None:
    global _last_suggestion_ts
    with _lock:
        _last_suggestion_ts = time.time()


# â"€â"€ Pattern matching â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

# Actions that require parameter context to produce a meaningful suggestion.
# Without the params, "should I click on?" or "should I open app?" is useless.
_CONTEXT_DEPENDENT_ACTIONS = frozenset({
    "click_on", "click_element", "fill_field", "type_into_focused", "press_key",
    "open_app", "delete_file", "who_opened", "rollback_to", "roll_back_to",
    "wifi_connect", "snapshot_now", "duck_app_when_speaking", "focus_mode",
    "recall", "ask_claude", "tell_me_about", "web_search", "deep_reasoning",
    "copy_from_screen", "find_on_screen", "watch_for",
    "apple_music_play_song", "apple_music_play_artist", "play_music",
})


def _time_bin(dt: datetime) -> str:
    h = dt.hour
    if h < 4:   return "midnight"
    if h < 8:   return "early_morning"
    if h < 12:  return "morning"
    if h < 17:  return "afternoon"
    if h < 21:  return "evening"
    return "night"


def _action_to_phrase(action: str) -> str:
    return action.replace("_", " ")


def _evaluate_proactive() -> None:
    """Core evaluation logic -- check patterns and fire suggestion if warranted."""
    try:
        from nora.cognitive_memory import get_behavioral_patterns
        patterns = get_behavioral_patterns()
    except Exception as e:
        logger.debug("Pattern evaluation skipped: %s", e)
        return

    dt = datetime.now()
    current_time_bin = _time_bin(dt)
    current_dow_name = dt.strftime("%A")

    # Check time-based patterns
    for p in patterns.get("time_patterns", []):
        when: str = p.get("when", "")
        when_parts = when.split(" ", 1)
        if len(when_parts) != 2:
            continue
        when_day, when_tb = when_parts
        if when_day != current_dow_name or when_tb != current_time_bin.replace("_", " "):
            continue
        if True:
            if p.get("count", 0) >= MIN_PATTERN_CONFIDENCE:
                action = p["frequent_action"]
                if action in _CONTEXT_DEPENDENT_ACTIONS:
                    continue
                phrase = _action_to_phrase(action)
                suggestion = f"Based on your routine, should I {phrase}?"
                logger.info("Proactive suggestion triggered: %s (pattern: %s x%d)",
                            action, when, p["count"])
                if _callback:
                    _callback(suggestion)
                _mark_suggested()
                return

    # Check strong workflow suggestions (top bigram with high confidence)
    for wf in patterns.get("workflow_patterns", [])[:3]:
        if wf.get("confidence", 0) >= MIN_PATTERN_CONFIDENCE * 2:
            trigger = wf["trigger"]
            follows = wf["follows"]
            if follows in _CONTEXT_DEPENDENT_ACTIONS:
                continue
            suggestion = f"You often {_action_to_phrase(follows)} after {_action_to_phrase(trigger)}. Want me to do that?"
            logger.info("Workflow proactive: %s â†' %s (x%d)", trigger, follows, wf["confidence"])
            if _callback:
                _callback(suggestion)
            _mark_suggested()
            return


# â"€â"€ Background loop â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

def _loop() -> None:
    global _running
    logger.info("Proactive intelligence engine started")
    while _running:
        time.sleep(CHECK_INTERVAL_SEC)
        if not _running:
            break
        if _is_idle() and _can_suggest():
            _evaluate_proactive()
    logger.info("Proactive intelligence engine stopped")


def start() -> None:
    global _running, _thread, IDLE_THRESHOLD_SEC, SUGGESTION_COOLDOWN_SEC, MIN_PATTERN_CONFIDENCE
    if _running:
        return
    try:
        from nora.config import get_config
        cfg = get_config().get("proactive", {})
        if not cfg.get("enabled", True):
            logger.info("Proactive engine disabled in config")
            return
        IDLE_THRESHOLD_SEC = cfg.get("idle_threshold_sec", IDLE_THRESHOLD_SEC)
        SUGGESTION_COOLDOWN_SEC = cfg.get("suggestion_cooldown_sec", SUGGESTION_COOLDOWN_SEC)
        MIN_PATTERN_CONFIDENCE = cfg.get("min_pattern_confidence", MIN_PATTERN_CONFIDENCE)
    except Exception:
        pass
    _running = True
    _thread = threading.Thread(target=_loop, daemon=True, name="nora-proactive")
    _thread.start()


def stop() -> None:
    global _running
    _running = False

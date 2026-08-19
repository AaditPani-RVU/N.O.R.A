"""Varied phrasing pools — kills NORA's canned-response tell.

Every stock line NORA speaks used to be a single hardcoded string: the same
"I didn't quite catch that — could you rephrase?" on every miss, the same
"Doing well, sir. What do you need?" on every greeting.  Hearing the identical
waveform twice in a row is the single loudest signal that you're talking to a
lookup table rather than an assistant.

This module holds the pools and guarantees a line never repeats back-to-back
(and, for pools large enough, never repeats within a rolling window).  Callers
just ask for a category:

    from nora import phrasing
    speaker.speak(phrasing.get("not_understood"), mood="error")

Pools are persona-aware where it matters: ``tone`` from ``nora.persona``
selects between the formal ("sir") and casual register.
"""
from __future__ import annotations

import logging
import random
import threading
from collections import deque

logger = logging.getLogger("nora.phrasing")

_lock = threading.RLock()

# How many recent picks per category to avoid re-drawing. Capped per-pool at
# len(pool) - 1 so a small pool can never deadlock the sampler.
_HISTORY_DEPTH = 4

_recent: dict[str, deque[str]] = {}


# ── Pools ────────────────────────────────────────────────────────────────────
#
# Keys ending in "_casual" are the casual-tone variants of the base key; get()
# resolves them automatically when persona.tone == "casual". A missing casual
# variant simply falls back to the base pool.

_POOLS: dict[str, list[str]] = {
    # Speech recognition / comprehension misses
    "not_understood": [
        "Sorry, I didn't catch that.",
        "That one got past me — say it again?",
        "I missed that. One more time?",
        "Didn't quite get that, sir.",
        "Come again?",
        "That didn't come through clearly.",
    ],
    "not_understood_casual": [
        "Sorry, missed that.",
        "Say that again?",
        "That one got past me.",
        "Didn't catch it — one more time?",
        "Hm, come again?",
    ],
    # The LLM asked for clarification instead of acting
    "need_specifics": [
        "I want to get that right — which one did you mean?",
        "Give me a bit more to go on.",
        "Not sure which you meant there.",
        "I could take that a couple of ways — which is it?",
    ],
    # Generic failure
    "error": [
        "That didn't work.",
        "Something went wrong there.",
        "Hit a snag on that one.",
        "That failed on me.",
    ],
    "recovered": [
        "Something went wrong, but I'm still here.",
        "That threw an error — I'm still listening.",
        "Hit a problem there. Still with you.",
    ],
    # Timeouts
    "too_slow": [
        "That took too long. Try again?",
        "Timed out on that one.",
        "That one hung — give it another go.",
    ],
    # Confirmation / cancellation
    "cancelled": [
        "Cancelled.",
        "Dropped it.",
        "Leaving it alone.",
        "Standing down.",
    ],
    "acknowledged": [
        "Got it.",
        "On it.",
        "Understood.",
        "Right away.",
        "Done deal.",
    ],
    # Greetings — used by fast_path so the hundredth "hello" isn't the first
    "greeting": [
        "Hello, sir.",
        "Sir.",
        "Evening, sir.",
        "Hello. What are we doing?",
        "Sir — ready when you are.",
    ],
    "greeting_casual": [
        "Hey.",
        "Hey — what's up?",
        "Yo.",
        "Hey there.",
        "What's going on?",
    ],
    "how_are_you": [
        "All systems steady. You?",
        "Running clean. What's on your mind?",
        "Good — everything's nominal here. How are you?",
        "No complaints. What do you need?",
        "Solid. What are we working on?",
    ],
    "how_are_you_casual": [
        "Pretty good. You?",
        "All good here. What's up?",
        "Can't complain. How about you?",
        "Doing fine. What's going on?",
    ],
    "presence": [
        "Always here, sir.",
        "Right here.",
        "Still here.",
        "Here. Go ahead.",
    ],
    "presence_casual": [
        "Yep, here.",
        "Right here.",
        "Still around.",
        "I'm here.",
    ],
    "affirm": [
        "Of course, sir.",
        "Certainly.",
        "Absolutely.",
        "You got it.",
    ],
    "affirm_casual": [
        "Sure thing.",
        "Yep.",
        "For sure.",
        "You got it.",
    ],
    "thanks_reply": [
        "Any time, sir.",
        "Of course.",
        "That's what I'm here for.",
        "No trouble at all.",
    ],
    "thanks_reply_casual": [
        "Anytime.",
        "No worries.",
        "Sure thing.",
        "You bet.",
    ],
    # Backchannels — the user said "mhm" / "cool" / "ok", nothing is required
    "backchannel": [
        "Mm-hm.",
        "Right.",
        "Sure.",
        "Noted.",
        "Okay.",
    ],
    # Already awake
    "already_awake": [
        "I'm already here, sir.",
        "Already up.",
        "Still awake, sir.",
        "I never left.",
    ],
    # Security refusals
    "blocked": [
        "That's blocked by the security policy.",
        "Security layer stopped that one.",
        "Can't run that — policy says no.",
    ],
    # Farewell
    "goodbye": [
        "Goodbye, sir.",
        "Signing off.",
        "Until next time, sir.",
        "Powering down. Goodbye.",
    ],
    # Conversation engine couldn't reach any model
    "chat_unavailable": [
        "My language model is out of reach right now.",
        "Can't get to a model at the moment — try again shortly.",
        "I'm offline from my models right now. Give it a second.",
    ],
    # Filler while a slow call is in flight
    "thinking": [
        "One moment.",
        "Let me think.",
        "Give me a second.",
        "Working on it.",
    ],
}


# ── Public API ───────────────────────────────────────────────────────────────

def _tone() -> str:
    try:
        from nora import persona
        return persona.get().get("tone", "professional")
    except Exception:
        return "professional"


def _resolve_pool(category: str) -> tuple[str, list[str]]:
    """Return (effective_key, pool) honouring the casual-tone variant."""
    if _tone() == "casual":
        casual_key = f"{category}_casual"
        pool = _POOLS.get(casual_key)
        if pool:
            return casual_key, pool
    return category, _POOLS.get(category, [])


def get(category: str, default: str = "") -> str:
    """Return a line from ``category``, avoiding recently used ones.

    Returns ``default`` for an unknown category so a typo degrades to silence
    or a caller-supplied string rather than raising mid-conversation.
    """
    key, pool = _resolve_pool(category)
    if not pool:
        if category not in _POOLS:
            logger.warning("Unknown phrasing category: %s", category)
        return default

    with _lock:
        history = _recent.setdefault(key, deque(maxlen=max(1, min(_HISTORY_DEPTH, len(pool) - 1))))
        choices = [line for line in pool if line not in history] or list(pool)
        pick = random.choice(choices)
        history.append(pick)
        return pick


def categories() -> list[str]:
    """All known pool names (casual variants included)."""
    return sorted(_POOLS)


def reset_history() -> None:
    """Clear repeat-avoidance state. Mainly for tests."""
    with _lock:
        _recent.clear()

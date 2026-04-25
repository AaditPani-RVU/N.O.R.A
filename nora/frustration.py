"""Frustration detection from voice patterns and command history.

Tracks the last N utterances and fires when 2+ frustration signals appear:
  - Filler/frustration words ("ugh", "hmm", "again", etc.)
  - Repeated identical commands (stuck in a loop)
  - Unusually loud audio (high RMS → stressed voice)
  - Short rapid-fire utterances (spamming commands)

Usage in pipeline:
    tracker = FrustrationTracker()
    ...
    if tracker.record(text, rms=rms, success=True):
        speaker.speak("You seem stuck — want me to ask Claude for help?")
"""
from __future__ import annotations

import time
from collections import deque

_FILLER_WORDS = frozenset({
    "ugh", "uggh", "argh", "arg", "hmm", "hm", "ugh",
    "damn", "dammit", "seriously", "come on", "again",
    "why", "what", "still", "really", "nothing", "broken",
    "failed", "error", "wrong", "not working",
})

_WINDOW = 8        # utterances to look back
_THRESHOLD = 2     # signals needed to trigger
_COOLDOWN = 120.0  # seconds before we can trigger again


class FrustrationTracker:
    def __init__(self) -> None:
        self._history: deque[dict] = deque(maxlen=_WINDOW)
        self._last_triggered: float = 0.0

    def record(self, text: str, rms: float = 0.0, success: bool = True) -> bool:
        """Record an utterance and return True if frustration is detected.

        Call this after every transcription in the pipeline loop.
        """
        now = time.time()
        self._history.append({
            "text": text.lower().strip(),
            "rms": rms,
            "success": success,
            "ts": now,
        })

        if now - self._last_triggered < _COOLDOWN:
            return False

        signals = self._count_signals()
        if signals >= _THRESHOLD:
            self._last_triggered = now
            return True
        return False

    def _count_signals(self) -> int:
        history = list(self._history)
        if len(history) < 2:
            return 0

        signals = 0
        recent = history[-5:]

        # Signal 1: filler/frustration words in last 3 utterances
        filler_hits = sum(
            1 for entry in history[-3:]
            if any(word in entry["text"] for word in _FILLER_WORDS)
        )
        if filler_hits >= 1:
            signals += 1

        # Signal 2: same command repeated 3+ times in last 5
        texts = [e["text"] for e in recent]
        if len(texts) >= 3 and len(set(texts[-3:])) == 1:
            signals += 2  # double-weight — very clear frustration signal

        # Signal 3: consecutive failures
        failures = sum(1 for e in recent if not e["success"])
        if failures >= 2:
            signals += 1

        # Signal 4: unusually loud (stressed) voice
        if recent and recent[-1]["rms"] > 0.12:
            signals += 1

        # Signal 5: rapid-fire short commands (< 3 words, < 2s apart)
        rapid = 0
        for i in range(1, len(recent)):
            dt = recent[i]["ts"] - recent[i - 1]["ts"]
            word_count = len(recent[i]["text"].split())
            if dt < 2.5 and word_count <= 3:
                rapid += 1
        if rapid >= 3:
            signals += 1

        return signals

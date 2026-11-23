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

import re
import time
from collections import deque

# Whole words only. "what" and "why" used to be here and matched as
# substrings, so every question ("what is this", "open whatsapp") counted.
_FILLER_WORDS = frozenset({
    "ugh", "uggh", "argh", "arg", "hmm", "hm",
    "damn", "dammit", "seriously", "come on", "again",
    "still", "really", "nothing", "broken",
    "failed", "error", "wrong", "not working",
})
_FILLER_RE = re.compile(r"\b(?:" + "|".join(sorted(map(re.escape, _FILLER_WORDS), key=len, reverse=True)) + r")\b")

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
            if _FILLER_RE.search(entry["text"])
        )
        if filler_hits >= 1:
            signals += 1

        # Signal 2: same command three times running. Double weight only when
        # one of them failed: "what is this" at three things, or "next track"
        # three times, is using NORA, not being stuck.
        last3 = recent[-3:]
        if len(last3) == 3 and len({e["text"] for e in last3}) == 1:
            signals += 1 if all(e["success"] for e in last3) else 2

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

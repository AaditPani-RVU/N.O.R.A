"""Tests for how the recorder decides a block is speech rather than noise.

This replaced a hardcoded `rms_threshold = 0.01`, which could not work: the
microphone NORA runs on idles at roughly 0.03 full-band, so silence scored as
speech and no wake-word turn ever ended, while the dead input it was pinned to
idles at 0.00003, so nothing reached 0.01 and every turn was thrown away with
"No speech in recording". One constant cannot serve two microphones an order of
magnitude apart, and the log said the same thing in both cases.

The subtle one is `test_a_turn_that_opens_with_a_word`: an adaptive floor that
takes its estimate from any block will take it from the first, and if you were
already talking it sets the bar to a multiple of your own voice and hears
nothing for the rest of the turn.

Stdlib unittest only -- run with:  python -m unittest tests.test_speech_threshold -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SR = 16000
_T = np.arange(1024, dtype=np.float32) / SR
VOICE = (np.sin(2 * np.pi * 1000 * _T) * 0.2).reshape(-1, 1)          # in-band
HISS = (np.sin(2 * np.pi * 1000 * _T) * 0.004).reshape(-1, 1)         # in-band, tiny
BIASED_SILENCE = np.full((1024, 1), 0.16, dtype=np.float32)           # the real mic idling
DEAD = np.full((1024, 1), 0.0, dtype=np.float32)


def _record_with(listener, script):
    """Run Listener._record over a scripted sequence of microphone blocks."""
    from nora import listener as listener_mod

    state = {"i": 0, "t": 1000.0, "cb": None}

    def now():
        return state["t"]

    def sleep(sec):
        state["t"] += sec
        if state["cb"] is None:
            return
        blocks = script(state["t"] - 1000.0)
        state["cb"](blocks, 1024, {}, None)

    class FakeStream:
        def __init__(self, *a, callback=None, **kw):
            state["cb"] = callback

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

    with mock.patch.object(listener_mod.sd, "InputStream", FakeStream), \
            mock.patch.object(listener_mod.time, "time", now), \
            mock.patch.object(listener_mod.time, "sleep", sleep), \
            mock.patch.object(listener_mod, "keyboard", mock.MagicMock()):
        return listener._record(ptt_mode=False)


class SpeechThresholdTest(unittest.TestCase):
    def setUp(self):
        from nora.listener import Listener
        self.listener = Listener()
        self.listener.speech_rms_threshold = None
        self.listener.speech_over_floor = 2.5
        self.listener.speech_floor_min = 0.02
        self.listener.wakeword_speech_start = 3.0
        self.listener.wakeword_silence_timeout = 1.0
        self.listener.max_duration = 20

    def test_the_biased_idle_of_a_real_microphone_is_not_speech(self):
        # +0.16 of DC and nothing else. Full-band RMS scored this 0.16 and
        # called it a voice, which is why turns never ended.
        self.assertIsNone(_record_with(self.listener, lambda t: BIASED_SILENCE))

    def test_a_dead_input_is_not_speech(self):
        self.assertIsNone(_record_with(self.listener, lambda t: DEAD))

    def test_a_voice_over_the_bias_is_captured(self):
        audio = _record_with(
            self.listener, lambda t: (VOICE + 0.16) if 1.0 <= t < 3.0 else BIASED_SILENCE)
        self.assertIsNotNone(audio)

    def test_a_turn_that_opens_with_a_word_is_still_heard(self):
        # No quiet before the speech, so there is no noise measurement to seed a
        # floor from. Seeding it from the first block instead sets the bar to
        # 2.5x the voice and loses the entire turn.
        audio = _record_with(
            self.listener, lambda t: (VOICE + 0.16) if t < 2.0 else BIASED_SILENCE)
        self.assertIsNotNone(audio)

    def test_the_floor_only_moves_the_bar_up(self):
        from nora.listener import Listener
        L: Listener = self.listener
        self.assertEqual(L._speech_threshold(None), 0.02)      # nothing known yet
        self.assertEqual(L._speech_threshold(0.001), 0.02)     # quiet room: the min holds
        self.assertEqual(L._speech_threshold(0.05), 0.125)     # noisy room: adapt up

    def test_a_pinned_threshold_overrides_the_floor(self):
        self.listener.speech_rms_threshold = 0.5
        self.assertEqual(self.listener._speech_threshold(None), 0.5)
        self.assertEqual(self.listener._speech_threshold(0.4), 0.5)
        # 0.14 of voice is now under the pinned bar, so the turn is dropped.
        self.assertIsNone(_record_with(
            self.listener, lambda t: (VOICE + 0.16) if 1.0 <= t < 3.0 else BIASED_SILENCE))

    def test_config_supplies_the_knobs(self):
        from nora.listener import Listener
        fresh = Listener()
        self.assertGreater(fresh.speech_floor_min, 0)
        self.assertGreater(fresh.speech_over_floor, 1.0)


if __name__ == "__main__":
    unittest.main()

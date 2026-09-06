"""Tests for when the recorder decides you have stopped talking.

Under PTT the key release is the end-of-turn signal and silence is only a
backstop, so a short gap costs nothing. After a wake word there is no key:
silence *is* the signal, and the value inherited from PTT — 0.7 s — ends the
turn during the pause between "remind me to" and whatever you were about to
remember. That is the bug these cover; the two paths share one function and it
is easy to re-collapse them into one timeout by accident.

Time is faked rather than waited out: a real test of a 3-second gap takes 3
seconds, and there would be several. The fake clock also removes the flake,
since the assertion is about which threshold was used, not about scheduling.

Stdlib unittest only — run with:  python -m unittest tests.test_listener_gaps -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LOUD = np.full((1024, 1), 0.2, dtype=np.float32)     # well above rms_threshold
QUIET = np.zeros((1024, 1), dtype=np.float32)        # well below it


class _FakeClock:
    """A clock the recorder's own sleep drives, feeding frames as it goes.

    `_record` polls in 50 ms steps and looks only at the newest frame, so the
    stand-in stream hands it whatever the script says the microphone is doing at
    the current virtual instant.
    """

    def __init__(self, speech_sec: float):
        self.now = 1000.0
        self.speech_sec = speech_sec
        self.start = 1000.0
        self.callback = None

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.callback is None:
            return
        elapsed = self.now - self.start
        self.callback(LOUD if elapsed < self.speech_sec else QUIET, 1024, {}, None)


def _run_record(listener, ptt_mode: bool, speech_sec: float) -> float:
    """Drive Listener._record on a fake clock; return the virtual seconds taken."""
    from nora import listener as listener_mod

    clock = _FakeClock(speech_sec)

    class FakeStream:
        def __init__(self, *a, callback=None, **kw):
            clock.callback = callback

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    with mock.patch.object(listener_mod.sd, "InputStream", FakeStream), \
            mock.patch.object(listener_mod.time, "time", clock.time), \
            mock.patch.object(listener_mod.time, "sleep", clock.sleep), \
            mock.patch.object(listener_mod, "keyboard", mock.MagicMock()):
        listener._record(ptt_mode=ptt_mode)
    return clock.now - clock.start


class WakewordGapTest(unittest.TestCase):
    def setUp(self):
        from nora.listener import Listener
        self.listener = Listener()
        # Pin the values so the test states its own premise rather than
        # inheriting whatever config.yaml currently says.
        self.listener.silence_timeout = 0.7
        self.listener.wakeword_silence_timeout = 3.0
        self.listener.wakeword_speech_start = 4.0
        self.listener.max_duration = 60

    def test_wakeword_mode_waits_the_longer_gap(self):
        # 2 s of speech, then silence: it must not stop until ~3 s of quiet.
        elapsed = _run_record(self.listener, ptt_mode=False, speech_sec=2.0)
        self.assertAlmostEqual(elapsed, 5.0, delta=0.3)

    def test_a_two_second_thinking_pause_does_not_end_a_wakeword_turn(self):
        # The actual complaint: a mid-sentence pause hung up the turn. At the
        # old PTT value of 0.7 this stopped at ~2.7 s.
        elapsed = _run_record(self.listener, ptt_mode=False, speech_sec=2.0)
        self.assertGreater(elapsed, 4.0)

    def test_ptt_mode_keeps_the_short_gap(self):
        # PTT is unaffected by the wake-word change: the key already said "done".
        elapsed = _run_record(self.listener, ptt_mode=True, speech_sec=2.0)
        self.assertAlmostEqual(elapsed, 2.7, delta=0.3)

    def test_the_two_modes_do_not_share_one_timeout(self):
        ptt = _run_record(self.listener, ptt_mode=True, speech_sec=2.0)
        wake = _run_record(self.listener, ptt_mode=False, speech_sec=2.0)
        self.assertGreater(wake - ptt, 1.5)

    def test_silence_after_the_wakeword_discards_the_turn(self):
        # Never spoke: the turn is dropped rather than sent as empty audio.
        clock_sec = _run_record(self.listener, ptt_mode=False, speech_sec=0.0)
        self.assertAlmostEqual(clock_sec, 4.0, delta=0.3)

    def test_config_supplies_both_gaps(self):
        from nora.listener import Listener
        fresh = Listener()
        self.assertGreater(fresh.wakeword_silence_timeout, fresh.silence_timeout)
        self.assertGreater(fresh.wakeword_speech_start, 0)


if __name__ == "__main__":
    unittest.main()

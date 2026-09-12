"""Tests for when the recorder decides you have stopped talking.

The two modes end a turn on completely different evidence, and they share one
function, so it is easy to collapse them back into one rule by accident.

After a wake word there is no key, so silence is the only signal there is. It
was once inherited from PTT at 0.7 s, which ended the turn during the pause
between "remind me to" and whatever you were about to remember.

Under PTT the key release is the signal and silence gets no vote at all. It
used to get one, as a "backstop", and that is what made push-to-talk unusable:
the recorder cannot tell whose voice it is hearing, so NORA's own
acknowledgement cue — played into the room at the very moment the microphone
opened — was scored as the user starting to talk, and the backstop then expired
while the user was still drawing breath. The turn ended holding a recording of
NORA saying "Okay.", which was transcribed and answered. `test_a_cue_in_the_room
_cannot_end_a_ptt_turn` is that exact sequence.

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

# A tone inside the speech band, not a DC constant. `_record` judges a block by
# its energy in 300 Hz-4 kHz, so a flat 0.2 -- which is what this used to be --
# is silence with an offset on it, and is scored as such. That is the whole
# point of the measure: the microphone this runs on adds a constant +0.16, and
# calling that speech is what made every turn either never start or never end.
_T = np.arange(1024, dtype=np.float32) / 16000.0
LOUD = (np.sin(2 * np.pi * 1000 * _T) * 0.2).reshape(-1, 1)   # in-band, RMS 0.14
QUIET = np.zeros((1024, 1), dtype=np.float32)                 # nothing at all


class _FakeClock:
    """A clock the recorder's own sleep drives, feeding frames as it goes.

    `_record` polls in 50 ms steps and looks only at the newest frame, so the
    stand-in stream hands it whatever the script says the microphone is doing at
    the current virtual instant. Speech runs from `speech_from` to `speech_to`,
    which is what lets a test put silence *before* the talking as well as after.
    """

    def __init__(self, speech_sec: float, speech_from: float = 0.0,
                 spans: list[tuple[float, float]] | None = None):
        self.now = 1000.0
        self.start = 1000.0
        # `spans` is the general form: every (from, to) window in which the
        # microphone hears something loud. It does not care *whose* voice it is,
        # which is the whole point -- neither does the recorder.
        self.spans = list(spans) if spans is not None else [
            (speech_from, speech_from + speech_sec)
        ]
        self.callback = None

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.callback is None:
            return
        elapsed = self.now - self.start
        loud = any(lo <= elapsed < hi for lo, hi in self.spans)
        self.callback(LOUD if loud else QUIET, 1024, {}, None)


def _drive(listener, ptt_mode: bool, speech_sec: float = 0.0, speech_from: float = 0.0,
           key_released_at: float | None = None,
           spans: list[tuple[float, float]] | None = None):
    """Drive Listener._record on a fake clock; return (virtual seconds, audio).

    `key_released_at` is virtual seconds from the start; None means the key is
    held for the whole recording. `spans` overrides speech_sec/speech_from when
    the microphone has to hear more than one burst.
    """
    from nora import listener as listener_mod

    clock = _FakeClock(speech_sec, speech_from, spans)

    class FakeStream:
        def __init__(self, *a, callback=None, **kw):
            clock.callback = callback

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake_keyboard = mock.MagicMock()
    if key_released_at is not None:
        fake_keyboard.is_pressed.side_effect = (
            lambda *_a, **_k: (clock.now - clock.start) < key_released_at
        )

    with mock.patch.object(listener_mod.sd, "InputStream", FakeStream), \
            mock.patch.object(listener_mod.time, "time", clock.time), \
            mock.patch.object(listener_mod.time, "sleep", clock.sleep), \
            mock.patch.object(listener_mod, "keyboard", fake_keyboard):
        audio = listener._record(ptt_mode=ptt_mode)
    return clock.now - clock.start, audio


def _run_record(listener, ptt_mode: bool, speech_sec: float) -> float:
    """Drive Listener._record on a fake clock; return the virtual seconds taken."""
    return _drive(listener, ptt_mode, speech_sec)[0]


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

    def test_silence_never_ends_a_ptt_turn(self):
        # Hold the key through two seconds of talking and then say nothing: the
        # recording runs to max_duration, because the key has not come up and
        # the key is the only thing that means "done".
        elapsed = _run_record(self.listener, ptt_mode=True, speech_sec=2.0)
        self.assertAlmostEqual(elapsed, self.listener.max_duration, delta=0.3)

    def test_the_two_modes_do_not_share_one_timeout(self):
        # Same audio, opposite rules: the wake turn ends itself on silence, the
        # held key does not.
        wake = _run_record(self.listener, ptt_mode=False, speech_sec=2.0)
        ptt = _run_record(self.listener, ptt_mode=True, speech_sec=2.0)
        self.assertAlmostEqual(wake, 5.0, delta=0.3)
        self.assertGreater(ptt, wake)

    def test_a_cue_in_the_room_cannot_end_a_ptt_turn(self):
        # The regression. NORA used to answer the key press out loud, and her
        # cue arrived at the microphone she had just opened: loud from 0.0 to
        # 0.8, then quiet while the user drew breath, then the user at 2.0.
        #
        # With a silence backstop this ended at 1.6 s holding 2.0 s of audio,
        # none of which was the user -- NORA recorded herself saying "Okay.",
        # transcribed it, and answered it. The cue is gone, but the recorder
        # must survive a stray sound regardless: the key is still down, so the
        # turn is still open, and the user's words are still in the clip.
        elapsed, audio = _drive(
            self.listener, ptt_mode=True,
            spans=[(0.0, 0.8), (2.0, 4.0)],
            key_released_at=4.5,
        )
        self.assertAlmostEqual(elapsed, 4.5, delta=0.2)
        self.assertIsNotNone(audio)
        self.assertGreater(len(audio) / 16000, 4.0, "the user's half was cut off")

    def test_silence_after_the_wakeword_discards_the_turn(self):
        # Never spoke: the turn is dropped rather than sent as empty audio.
        clock_sec = _run_record(self.listener, ptt_mode=False, speech_sec=0.0)
        self.assertAlmostEqual(clock_sec, 4.0, delta=0.3)

    def test_ptt_does_not_hang_up_before_you_start_talking(self):
        # Hold the key and say nothing: the turn stays open until the key comes
        # up. It used to end 0.7 s in -- "Silence detected", "No speech in
        # recording", and round again for another cue on top of the sentence you
        # were starting. A held key is the user saying "wait, I am getting to
        # it", and nobody else gets a vote.
        elapsed, audio = _drive(self.listener, ptt_mode=True, speech_sec=0.0)
        self.assertAlmostEqual(elapsed, self.listener.max_duration, delta=0.3)
        self.assertIsNone(audio)  # still discarded -- room tone is not a command

    def test_ptt_keeps_speech_that_starts_after_a_pause(self):
        # Two seconds of gathering your thoughts, then two of talking. The whole
        # utterance has to survive: this used to be cut off, discarded, and
        # answered with another "Sure." while you were still mid-word.
        elapsed, audio = _drive(
            self.listener, ptt_mode=True, speech_sec=2.0, speech_from=2.0,
            key_released_at=4.5,
        )
        self.assertIsNotNone(audio)
        self.assertAlmostEqual(elapsed, 4.5, delta=0.2)

    def test_ptt_release_still_ends_the_turn(self):
        # The one thing that ends a push-to-talk turn, and the only thing.
        elapsed, _ = _drive(
            self.listener, ptt_mode=True, speech_sec=1.0, key_released_at=1.5,
        )
        self.assertAlmostEqual(elapsed, 1.5, delta=0.2)

    def test_wakeword_still_gives_up_on_a_silent_room(self):
        # With no key to release, an unanswered wake word must still expire --
        # otherwise a false trigger holds the microphone for max_record_sec.
        elapsed, audio = _drive(self.listener, ptt_mode=False, speech_sec=0.0)
        self.assertAlmostEqual(elapsed, self.listener.wakeword_speech_start, delta=0.3)
        self.assertIsNone(audio)

    def test_config_supplies_the_wakeword_gaps(self):
        # Only the wake word has gaps now. Both have to be real numbers: a zero
        # `wakeword_speech_start` drops every turn, and a zero silence gap ends
        # one on the first pause.
        from nora.listener import Listener
        fresh = Listener()
        self.assertGreater(fresh.wakeword_silence_timeout, 0)
        self.assertGreater(fresh.wakeword_speech_start, 0)


if __name__ == "__main__":
    unittest.main()

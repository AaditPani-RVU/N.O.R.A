"""Tests for what opens the microphone, and what NORA says while it is open.

She says nothing. That is the point of this file.

NORA used to answer a wake word or a key press out loud -- "Sure.", "Okay.",
"One sec." -- and then immediately start recording. On a laptop the speakers
and the microphone are inches apart, so the cue landed in the clip it was
announcing, the recorder scored it as the user starting to speak, and the turn
ended before the user had said a word. What got transcribed was NORA. See
`tests/test_listener_gaps.py` for the timing half of that failure; this file
guards the removal itself, on both routes into the recorder.

The second failure here is older and separate. The PTT branch is polled several
times a second, and nothing remembered that the current press had already been
answered -- so one held key opened a new recording on every poll. A press is a
press, not a poll.

Stdlib unittest only -- run with:  python -m unittest tests.test_ptt_turns -v
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

AUDIO = np.zeros(16000, dtype=np.float32)


class _Rig:
    """Stands in for everything `listen()` reaches for: cue, detector, recorder.

    `events` is the running order of what NORA did. Any call into the ack module
    lands in it as "cue", so a test can assert on the absence of one without
    knowing which ack entry point a future edit might reach for.
    """

    def __init__(self, ptt_script: list[bool], wake_script: list[bool] | None = None):
        self.ptt_script = list(ptt_script)
        self.wake_script = list(wake_script or [])
        self.events: list[str] = []

    def check_ptt(self) -> bool:
        return self.ptt_script.pop(0) if self.ptt_script else False

    def wait_for_trigger(self, timeout: float = 0.0) -> bool:
        return self.wake_script.pop(0) if self.wake_script else False

    def speak_ack(self, *a, **kw) -> None:
        self.events.append("cue")

    def wait_until_finished(self, *a, **kw) -> None:
        self.events.append("cue")

    def drain_trigger(self) -> None:
        self.events.append("drain")

    def record(self, ptt_mode: bool = True):
        self.events.append(f"record(ptt={ptt_mode})")
        return AUDIO

    @property
    def cues(self) -> int:
        return self.events.count("cue")

    @property
    def recordings(self) -> int:
        return sum(1 for e in self.events if e.startswith("record("))


def _patches(rig: _Rig):
    """Everything both entry points are allowed to touch, wired to the rig."""
    from nora import ack as ack_mod, wakeword as ww_mod
    from nora.listener import Listener

    return [
        mock.patch.object(ww_mod, "wait_for_trigger", rig.wait_for_trigger),
        mock.patch.object(ww_mod, "drain_trigger", rig.drain_trigger),
        mock.patch.object(ack_mod, "speak_ack", rig.speak_ack),
        mock.patch.object(ack_mod, "wait_until_finished", rig.wait_until_finished),
        mock.patch.object(Listener, "_record", rig.record),
    ]


def _listen(rig: _Rig, times: int = 1) -> list:
    """Run Listener.listen() `times` round(s) with the rig wired in."""
    import contextlib

    from nora import wakeword as ww_mod
    from nora.listener import Listener

    listener = Listener()
    results = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(ww_mod, "is_enabled", lambda: True))
        stack.enter_context(mock.patch.object(Listener, "_check_ptt_now", rig.check_ptt))
        for p in _patches(rig):
            stack.enter_context(p)
        for _ in range(times):
            results.append(asyncio.run(listener.listen()))
    return results


class OnePressOneTurnTest(unittest.TestCase):
    def test_a_held_key_records_once_not_once_per_poll(self):
        # Four polls of one continuous press. Before the latch, that was four
        # aborted recordings inside about three seconds.
        rig = _Rig(ptt_script=[True, True, True, True])
        _listen(rig, times=4)
        self.assertEqual(rig.recordings, 1)

    def test_only_the_first_poll_of_a_press_records(self):
        rig = _Rig(ptt_script=[True, True, True])
        results = _listen(rig, times=3)
        self.assertEqual([r is not None for r in results], [True, False, False])

    def test_the_key_has_to_come_back_up_to_count_as_a_new_press(self):
        # press, hold, release, press again -- two turns.
        rig = _Rig(ptt_script=[True, True, False, True])
        _listen(rig, times=4)
        self.assertEqual(rig.recordings, 2)

    def test_an_idle_key_does_nothing_at_all(self):
        rig = _Rig(ptt_script=[False, False])
        results = _listen(rig, times=2)
        self.assertEqual(rig.events, [])
        self.assertEqual(results, [None, None])


class NothingIsSpokenIntoTheMicrophoneTest(unittest.TestCase):
    def test_a_key_press_opens_the_microphone_without_a_word(self):
        rig = _Rig(ptt_script=[True])
        _listen(rig)
        self.assertEqual(rig.events, ["record(ptt=True)"])
        self.assertEqual(rig.cues, 0)

    def test_a_wake_word_opens_the_microphone_without_a_word(self):
        rig = _Rig(ptt_script=[], wake_script=[True])
        _listen(rig)
        self.assertEqual(rig.events, ["record(ptt=False)", "drain"])
        self.assertEqual(rig.cues, 0)

    def test_a_wake_turn_clears_what_fired_while_it_was_listening(self):
        # The detector keeps scoring through the recording; whatever it matched
        # in there must not wake the next poll on its own.
        rig = _Rig(ptt_script=[], wake_script=[True])
        _listen(rig)
        self.assertEqual(rig.events[-1], "drain")

    def test_both_ways_in_take_the_same_path(self):
        # listen() used to carry its own copy of the wake sequence, and the copy
        # was the one missing a fix. Same events, or they have drifted again.
        import contextlib

        from nora.listener import Listener

        via_listen = _Rig(ptt_script=[], wake_script=[True])
        _listen(via_listen)

        via_helper = _Rig(ptt_script=[], wake_script=[True])
        listener = Listener()
        with contextlib.ExitStack() as stack:
            for p in _patches(via_helper):
                stack.enter_context(p)
            asyncio.run(listener.listen_wakeword())

        self.assertEqual(via_listen.events, via_helper.events)


class AckIsOffByDefaultTest(unittest.TestCase):
    def test_the_shipped_config_disables_acknowledgement_tokens(self):
        from nora.config import get_config

        self.assertFalse(get_config().get("ack", {}).get("enabled", False))

    def test_a_missing_config_section_also_means_off(self):
        # The module default has to agree with config.yaml, or a stripped-down
        # config quietly puts the cue back into the recording.
        from nora import ack

        with mock.patch.object(ack, "_cfg", return_value={}), \
                mock.patch.object(ack, "_play_now") as play:
            ack._loaded.set()
            ack._ack_sounds["Mm-hm."] = object()
            try:
                ack.speak_ack(delay=0, force=True)
            finally:
                ack._ack_sounds.clear()
                ack._loaded.clear()
        play.assert_not_called()


if __name__ == "__main__":
    unittest.main()

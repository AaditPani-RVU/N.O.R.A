"""Tests for refusing to transcribe silence.

Whisper does not return nothing when it hears nothing — it returns its priors.
Fed room tone it emits "Thank you.", "Thanks for watching!", "Bye.", with no
signal that they were invented. NORA answers the hallucination, answering starts
the next turn, that turn records more silence, and the loop sustains itself:
the log that prompted this has five "Thank you."s in fifteen seconds with nobody
speaking.

Two gates, because either one alone leaves a hole. The recorder must not hand
back a clip it never heard speech in — under PTT it used to, since only the
wake-word path checked. And the transcriber must not send one on even if it
gets one, because the recorder is not the only caller and the remote endpoint
has no VAD of its own (the local path has always run vad_filter=True, which is
why this only ever bit on the remote backend).

The measure under test is "how much of this clip is loud", not "how loud is
this clip on average". A single word followed by two seconds of room tone
averages down to silence, and throwing that away would lose real speech — so
that case is asserted explicitly.

Stdlib unittest only — run with:  python -m unittest tests.test_silence_gate -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nora.transcriber as transcriber  # noqa: E402
from nora.transcriber import has_speech  # noqa: E402

RNG = np.random.default_rng(20260906)


def _tone(seconds: float, amplitude: float) -> np.ndarray:
    return (RNG.standard_normal(int(seconds * 16000)) * amplitude).astype(np.float32)


class HasSpeechTest(unittest.TestCase):
    def test_pure_silence_is_not_speech(self):
        self.assertFalse(has_speech(np.zeros(12800, dtype=np.float32)))

    def test_room_tone_is_not_speech(self):
        # The actual failing input: 0.8 s that the recorder stopped on because
        # it had gone quiet.
        self.assertFalse(has_speech(_tone(0.8, 0.002)))

    def test_speech_is_speech(self):
        self.assertTrue(has_speech(_tone(1.0, 0.05)))

    def test_one_word_in_a_long_quiet_clip_survives(self):
        # Average RMS here is far below the threshold; the frame count is not.
        # Getting this wrong throws away real, short commands.
        clip = np.concatenate([_tone(0.3, 0.15), _tone(2.0, 0.002)])
        self.assertTrue(has_speech(clip))

    def test_a_clip_shorter_than_one_frame_is_not_speech(self):
        self.assertFalse(has_speech(_tone(0.02, 0.2)))

    def test_a_single_loud_blip_is_not_speech(self):
        # A click, a key press, a door. Loud but far too short to be a word.
        clip = np.concatenate([_tone(0.06, 0.2), np.zeros(12000, dtype=np.float32)])
        self.assertFalse(has_speech(clip))

    def test_empty_audio_is_not_speech(self):
        self.assertFalse(has_speech(np.zeros(0, dtype=np.float32)))


class TranscribeGateTest(unittest.TestCase):
    def test_silence_never_reaches_a_model(self):
        # Not just "returns empty" — it must not spend the API call either.
        with mock.patch.object(transcriber, "_transcribe_remote") as remote, \
                mock.patch.object(transcriber, "_get_model") as local:
            self.assertEqual(transcriber.transcribe(_tone(0.8, 0.002)), "")
            self.assertFalse(remote.called)
            self.assertFalse(local.called)

    def test_real_speech_still_goes_through(self):
        with mock.patch.object(transcriber, "_transcribe_remote",
                               return_value="what's the weather") as remote:
            with mock.patch.object(transcriber, "get_config",
                                   return_value={"transcriber": {"backend": "remote"}}):
                self.assertEqual(transcriber.transcribe(_tone(1.0, 0.05)),
                                 "what's the weather")
            self.assertTrue(remote.called)

    def test_int16_input_is_accepted(self):
        # transcribe() coerces dtype; the gate must run on the coerced array.
        loud = (_tone(1.0, 0.05) * 32767).astype(np.int16).astype(np.float64)
        with mock.patch.object(transcriber, "_transcribe_remote", return_value="hi"):
            with mock.patch.object(transcriber, "get_config",
                                   return_value={"transcriber": {"backend": "remote"}}):
                # Scaled back to float range the way callers hand it over.
                self.assertEqual(transcriber.transcribe(loud / 32767), "hi")


class RecorderGateTest(unittest.TestCase):
    def test_ptt_discards_a_recording_with_no_speech(self):
        """PTT held over a silent room must produce nothing, not room tone."""
        from nora import listener as listener_mod
        from nora.listener import Listener

        lis = Listener()
        lis.silence_timeout = 0.7
        lis.wakeword_silence_timeout = 3.0
        lis.wakeword_speech_start = 4.0
        lis.max_duration = 3

        quiet = np.full((1024, 1), 0.001, dtype=np.float32)
        clock = {"now": 1000.0}

        class FakeStream:
            def __init__(self, *a, callback=None, **kw):
                self.cb = callback

            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False

        def fake_sleep(sec):
            clock["now"] += sec
            stream.cb(quiet, 1024, {}, None)

        stream = None

        def make(*a, **kw):
            nonlocal stream
            stream = FakeStream(*a, **kw)
            return stream

        with mock.patch.object(listener_mod.sd, "InputStream", make), \
                mock.patch.object(listener_mod.time, "time", lambda: clock["now"]), \
                mock.patch.object(listener_mod.time, "sleep", fake_sleep), \
                mock.patch.object(listener_mod, "keyboard", mock.MagicMock()):
            self.assertIsNone(lis._record(ptt_mode=True))


if __name__ == "__main__":
    unittest.main()

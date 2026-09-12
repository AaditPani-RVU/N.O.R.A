"""Tests for the microphone conditioning in nora/dsp.py.

The microphone NORA runs on delivers a constant +0.16 DC offset on top of a
working capture, and most of its noise sits below the speech band. Those two
facts broke every level-based decision in the program: the recorder's speech
test, the wake-word detector's input, and the probe that is supposed to tell you
which microphone works. These cover the two corrections that answer them.

Stdlib unittest only -- run with:  python -m unittest tests.test_dsp -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nora.dsp import dc_offset, remove_dc, speech_rms  # noqa: E402

SR = 16000
T = np.arange(1024, dtype=np.float32) / SR
BIAS = 0.16  # what the ACP digital microphone on this machine actually adds


def tone(hz: float, amp: float = 0.2) -> np.ndarray:
    return (np.sin(2 * np.pi * hz * T) * amp).astype(np.float32)


class RemoveDcTest(unittest.TestCase):
    def test_a_constant_bias_is_removed(self):
        x = tone(1000) + BIAS
        self.assertAlmostEqual(dc_offset(x), BIAS, places=3)
        self.assertAlmostEqual(dc_offset(remove_dc(x)), 0.0, places=6)

    def test_the_signal_itself_survives(self):
        clean = tone(1000)
        recovered = remove_dc(clean + BIAS)
        self.assertLess(float(np.max(np.abs(recovered - clean))), 1e-4)

    def test_a_healthy_capture_is_left_alone(self):
        clean = tone(1000)
        self.assertLess(float(np.max(np.abs(remove_dc(clean) - clean))), 1e-4)

    def test_empty_and_dtype(self):
        self.assertEqual(remove_dc(np.zeros(0, dtype=np.float32)).size, 0)
        self.assertEqual(remove_dc(tone(1000)).dtype, np.float32)
        self.assertEqual(dc_offset(np.zeros(0, dtype=np.float32)), 0.0)


class SpeechRmsTest(unittest.TestCase):
    def test_it_reads_as_a_plain_rms_of_an_in_band_tone(self):
        # A sine of amplitude 0.2 has RMS 0.2/sqrt(2). The band measure is
        # scaled to agree, so thresholds keep ordinary units.
        self.assertAlmostEqual(speech_rms(tone(1000, 0.2), SR), 0.2 / np.sqrt(2), places=3)

    def test_energy_below_the_band_does_not_count(self):
        # The failure this exists for: the noise on this microphone is mostly
        # low-frequency, and full-band RMS charges a voice for all of it.
        self.assertLess(speech_rms(tone(60, 0.2), SR), 0.01)

    def test_energy_above_the_band_does_not_count(self):
        self.assertLess(speech_rms(tone(6000, 0.2), SR), 0.01)

    def test_a_dc_offset_is_not_speech(self):
        # A flat block is a bias, not a voice. Full-band RMS called this 0.16 of
        # signal, which is how silence kept reading as speech.
        self.assertLess(speech_rms(np.full(1024, BIAS, dtype=np.float32), SR), 1e-6)

    def test_a_voice_under_a_bias_and_rumble_still_reads(self):
        # The real capture: speech, plus the offset, plus low-frequency noise.
        mixed = tone(1000, 0.2) + BIAS + tone(80, 0.4)
        self.assertAlmostEqual(speech_rms(mixed, SR), 0.2 / np.sqrt(2), places=2)

    def test_it_separates_the_two_better_than_full_band_rms(self):
        # Why the measure was changed at all, as a ratio rather than a constant.
        noise = BIAS + tone(80, 0.4)
        voice = noise + tone(1000, 0.2)
        full = lambda x: float(np.sqrt(np.mean((x - x.mean()) ** 2)))
        self.assertGreater(
            speech_rms(voice, SR) / max(speech_rms(noise, SR), 1e-9),
            full(voice) / max(full(noise), 1e-9),
        )

    def test_short_blocks_do_not_raise(self):
        self.assertGreaterEqual(speech_rms(np.zeros(8, dtype=np.float32), SR), 0.0)
        self.assertEqual(speech_rms(np.zeros(0, dtype=np.float32), SR), 0.0)


if __name__ == "__main__":
    unittest.main()

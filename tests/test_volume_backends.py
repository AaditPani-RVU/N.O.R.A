"""Tests for the Linux mixer backends (nora/platform/linux/volume.py).

This module existed only as an import for the whole Linux port. Both callers
caught the ImportError, so the failure surfaced as bad advice ("install
wireplumber" on a machine that had it) and a 177 MB log rather than as an
error anyone could see. Tests here so a missing or misparsed backend fails
loudly instead.

What is actually worth testing is the parsing, because that is what differs per
backend and what breaks when a tool changes its output: wpctl prints a 0.0-1.0
float with the mute flag appended to the same line, pactl prints a percentage
per channel and answers mute on a separate call, and amixer prints a mapped
percentage with [on]/[off]. All three have to come out of `get_state` as the
same (0-100, bool).

Subprocesses are stubbed at `_run` — these tests must not move the volume of
the machine running them.

Stdlib unittest only — run with:  python -m unittest tests.test_volume_backends -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nora.platform.linux import volume  # noqa: E402


class VolumeBackendTest(unittest.TestCase):
    def setUp(self):
        # The backend is cached after first detection; each test picks its own.
        volume._backend_cache = False
        self.addCleanup(setattr, volume, "_backend_cache", False)

    def _as(self, backend, outputs):
        """Pin the backend and script `_run` by the command it is given."""
        volume._backend_cache = backend

        def fake_run(args):
            key = " ".join(args[:2])
            return outputs.get(key, "")

        return mock.patch.object(volume, "_run", side_effect=fake_run)

    # ── wpctl ────────────────────────────────────────────────────────────────
    def test_wpctl_parses_float_volume(self):
        with self._as("wpctl", {"wpctl get-volume": "Volume: 0.60\n"}):
            self.assertEqual(volume.get_state(), (60, False))

    def test_wpctl_reads_mute_from_the_same_line(self):
        with self._as("wpctl", {"wpctl get-volume": "Volume: 0.60 [MUTED]\n"}):
            self.assertEqual(volume.get_state(), (60, True))

    def test_wpctl_rounds_rather_than_truncates(self):
        # 0.55 -> 55, not 54: truncation makes the slider drift down on every
        # read-modify-write cycle.
        with self._as("wpctl", {"wpctl get-volume": "Volume: 0.55\n"}):
            self.assertEqual(volume.get_state(), (55, False))

    # ── pactl ────────────────────────────────────────────────────────────────
    def test_pactl_parses_percentage_and_separate_mute(self):
        out = "Volume: front-left: 39321 /  60% / -13.98 dB,   front-right: 39321 /  60%"
        with self._as("pactl", {"pactl get-sink-volume": out,
                                "pactl get-sink-mute": "Mute: yes\n"}):
            self.assertEqual(volume.get_state(), (60, True))

    def test_pactl_unmuted(self):
        out = "Volume: front-left: 39321 /  60% / -13.98 dB"
        with self._as("pactl", {"pactl get-sink-volume": out,
                                "pactl get-sink-mute": "Mute: no\n"}):
            self.assertEqual(volume.get_state(), (60, False))

    # ── amixer ───────────────────────────────────────────────────────────────
    def test_amixer_parses_mapped_percentage_and_on_off(self):
        out = "  Front Left: Playback 39320 [60%] [on]\n  Front Right: Playback 39320 [60%] [on]\n"
        with self._as("amixer", {"amixer -M": out}):
            self.assertEqual(volume.get_state(), (60, False))

    def test_amixer_off_means_muted(self):
        out = "  Front Left: Playback 39320 [60%] [off]\n"
        with self._as("amixer", {"amixer -M": out}):
            self.assertEqual(volume.get_state(), (60, True))

    # ── failure is None, never a plausible number ────────────────────────────
    def test_unreadable_mixer_gives_none_not_a_guess(self):
        volume._backend_cache = "wpctl"
        with mock.patch.object(volume, "_run", return_value=None):
            self.assertIsNone(volume.get_state())

    def test_unparseable_output_gives_none(self):
        with self._as("wpctl", {"wpctl get-volume": "something else entirely"}):
            self.assertIsNone(volume.get_state())

    def test_no_backend_means_unavailable_and_no_state(self):
        volume._backend_cache = None
        self.assertFalse(volume.available())
        self.assertIsNone(volume.get_state())
        self.assertFalse(volume.set_volume(50))
        self.assertFalse(volume.set_muted(True))
        self.assertIsNone(volume.adjust_volume(5))

    # ── clamping ─────────────────────────────────────────────────────────────
    def test_set_volume_clamps_to_0_100(self):
        volume._backend_cache = "wpctl"
        with mock.patch.object(volume, "_run", return_value="") as run:
            volume.set_volume(150)
            volume.set_volume(-20)
        # wpctl amplifies past 1.0 on request, which distorts rather than
        # gets louder, so the clamp has to happen before the call.
        sent = [c.args[0][-1] for c in run.call_args_list]
        self.assertEqual(sent, ["1.00", "0.00"])

    def test_adjust_volume_clamps_and_returns_the_level_actually_set(self):
        with self._as("wpctl", {"wpctl get-volume": "Volume: 0.98\n"}):
            self.assertEqual(volume.adjust_volume(10), 100)

    def test_adjust_volume_is_none_when_the_level_cannot_be_read(self):
        volume._backend_cache = "wpctl"
        with mock.patch.object(volume, "_run", return_value=None):
            self.assertIsNone(volume.adjust_volume(10))


if __name__ == "__main__":
    unittest.main()

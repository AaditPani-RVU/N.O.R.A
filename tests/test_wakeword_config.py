"""Tests for how `wakeword.model` and `wakeword.input_device` are resolved.

These two functions decide whether the detector starts at all, and both of
their failure modes are silent ones. A model path that does not resolve reaches
openWakeWord as a *pretrained name* and fails with a message naming the wrong
problem; an input device that resolves but cannot open 16 kHz mono raises on the
detector thread, where it stops the wake word without stopping NORA. Neither
shows up in a smoke test of the turn loop, because neither is on the turn loop.

Measured on the machine this was written for: hw:2,0 matches the name `ALC245`
and then refuses 16 kHz, so "the config named a real device" and "the config
named a device we can use" are genuinely different questions.

sounddevice is stubbed rather than real — the assertions are about the decision,
not about anyone's audio hardware.

Stdlib unittest only — run with:  python -m unittest tests.test_wakeword_config -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nora.wakeword import _resolve_device, _resolve_models  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _fake_sd(devices, opens_at_16k=()):
    """A stand-in sounddevice whose check_input_settings has an opinion.

    `devices` is a list of (name, max_input_channels); `opens_at_16k` is the set
    of indices that accept the detector's format. Anything else raises, which is
    what a raw ALSA device does when asked for a rate its card does not run.
    """
    sd = mock.MagicMock()
    sd.query_devices.return_value = [
        {"name": n, "max_input_channels": c} for n, c in devices
    ]

    def check(device=None, **kwargs):
        if device not in opens_at_16k:
            raise ValueError("Invalid sample rate [PaErrorCode -9997]")

    sd.check_input_settings.side_effect = check
    return sd


class ResolveModelsTest(unittest.TestCase):
    def test_bare_name_is_passed_through_as_a_pretrained_model(self):
        self.assertEqual(_resolve_models("hey_jarvis_v0.1"), ["hey_jarvis_v0.1"])

    def test_relative_path_resolves_against_the_project_root(self):
        # Not the launch directory: NORA is started from a desktop entry and a
        # systemd unit, neither of which runs from the repo.
        resolved = _resolve_models("models/wakeword/hey_nora.onnx")
        self.assertEqual(resolved, [str(ROOT / "models/wakeword/hey_nora.onnx")])

    def test_missing_model_file_is_dropped_rather_than_passed_on(self):
        # Passing it on is what produces "Could not find pretrained model for
        # model name '.../nope.onnx'" — a message about the wrong problem.
        self.assertEqual(_resolve_models("models/wakeword/nope.onnx"), [])

    def test_a_list_of_phrasings_resolves_to_all_of_them(self):
        self.assertEqual(
            _resolve_models(["hey_jarvis_v0.1", "alexa_v0.1"]),
            ["hey_jarvis_v0.1", "alexa_v0.1"],
        )

    def test_blank_entries_are_ignored(self):
        self.assertEqual(_resolve_models(["", "  ", "alexa_v0.1"]), ["alexa_v0.1"])


class ResolveDeviceTest(unittest.TestCase):
    DEVICES = [("HD-Audio Generic: ALC245 Analog (hw:2,0)", 2), ("pipewire", 64)]

    def test_unset_means_the_system_default(self):
        self.assertIsNone(_resolve_device(None))
        self.assertIsNone(_resolve_device(""))

    def test_name_substring_matches_case_insensitively(self):
        sd = _fake_sd(self.DEVICES, opens_at_16k={0})
        with mock.patch.dict(sys.modules, {"sounddevice": sd}):
            self.assertEqual(_resolve_device("alc245"), 0)

    def test_index_is_used_as_given_when_it_opens(self):
        sd = _fake_sd(self.DEVICES, opens_at_16k={1})
        with mock.patch.dict(sys.modules, {"sounddevice": sd}):
            self.assertEqual(_resolve_device(1), 1)

    def test_device_that_cannot_do_16k_mono_falls_back_to_the_default(self):
        # The real bug this guards: the name matches, so the config looks
        # correct, and the stream then dies on the detector thread.
        sd = _fake_sd(self.DEVICES, opens_at_16k={1})
        with mock.patch.dict(sys.modules, {"sounddevice": sd}):
            self.assertIsNone(_resolve_device("ALC245"))
            self.assertIsNone(_resolve_device(0))

    def test_unmatched_name_falls_back_instead_of_disabling_the_wake_word(self):
        sd = _fake_sd(self.DEVICES, opens_at_16k={0, 1})
        with mock.patch.dict(sys.modules, {"sounddevice": sd}):
            self.assertIsNone(_resolve_device("no-such-mic"))

    def test_output_only_devices_are_not_candidates(self):
        sd = _fake_sd([("Speakers", 0), ("Webcam Mic", 1)], opens_at_16k={0, 1})
        with mock.patch.dict(sys.modules, {"sounddevice": sd}):
            self.assertIsNone(_resolve_device("Speakers"))
            self.assertEqual(_resolve_device("Webcam"), 1)

    def test_ambiguous_name_takes_the_first_match(self):
        sd = _fake_sd([("Mic A", 1), ("Mic B", 1)], opens_at_16k={0, 1})
        with mock.patch.dict(sys.modules, {"sounddevice": sd}):
            self.assertEqual(_resolve_device("Mic"), 0)

    def test_enumeration_failure_falls_back_to_the_default(self):
        sd = mock.MagicMock()
        sd.query_devices.side_effect = OSError("PortAudio not initialised")
        with mock.patch.dict(sys.modules, {"sounddevice": sd}):
            self.assertIsNone(_resolve_device("ALC245"))


if __name__ == "__main__":
    unittest.main()

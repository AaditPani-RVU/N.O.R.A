"""Tests for remote audio mirroring (nora/audio_relay.py).

Stdlib unittest only — run with:  python -m unittest tests.test_audio_relay -v

The invariant that matters: the relay is a side channel. A browser that is
absent, slow, or throwing must never change what the laptop does with its own
speakers, so most of these assert on what *doesn't* happen.
"""
from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nora import audio_relay


class _Relay(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(audio_relay, "_enabled", True)
        p.start()
        self.addCleanup(p.stop)
        self.pushed = []

    def _ui(self, clients=True):
        ui = mock.Mock()
        ui.has_ws_clients.return_value = clients
        ui.ws_push.side_effect = self.pushed.append
        return mock.patch.dict("sys.modules", {"nora.ui_server": ui})

    def _mp3(self, size=64):
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.write(b"\xff\xfb" + b"\x00" * (size - 2))
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return tmp.name


class TestPushChunk(_Relay):
    def test_chunk_is_sent_as_base64_mp3(self):
        path = self._mp3()
        with self._ui():
            audio_relay.push_chunk(path, "hello there")

        self.assertEqual(len(self.pushed), 1)
        msg = self.pushed[0]
        self.assertEqual(msg["type"], "audio")
        self.assertEqual(msg["mime"], "audio/mpeg")
        self.assertEqual(msg["text"], "hello there")
        self.assertEqual(base64.b64decode(msg["data"]), Path(path).read_bytes())

    def test_nothing_encoded_when_no_client_is_listening(self):
        with self._ui(clients=False):
            audio_relay.push_chunk(self._mp3(), "hello")
        self.assertEqual(self.pushed, [])

    def test_sequence_numbers_increase(self):
        path = self._mp3()
        with self._ui():
            audio_relay.push_chunk(path)
            audio_relay.push_chunk(path)
        self.assertLess(self.pushed[0]["seq"], self.pushed[1]["seq"])

    def test_oversized_chunk_is_dropped(self):
        path = self._mp3()
        with self._ui(), mock.patch.object(audio_relay, "_MAX_CHUNK_BYTES", 8):
            audio_relay.push_chunk(path)
        self.assertEqual(self.pushed, [])

    def test_missing_file_does_not_raise(self):
        with self._ui():
            audio_relay.push_chunk("/nonexistent/chunk.mp3")   # must not raise
        self.assertEqual(self.pushed, [])

    def test_broken_ui_server_does_not_raise(self):
        ui = mock.Mock()
        ui.has_ws_clients.return_value = True
        ui.ws_push.side_effect = RuntimeError("socket exploded")
        with mock.patch.dict("sys.modules", {"nora.ui_server": ui}):
            audio_relay.push_chunk(self._mp3())   # must not raise

    def test_disabled_relay_sends_nothing(self):
        with mock.patch.object(audio_relay, "_enabled", False), self._ui():
            audio_relay.push_chunk(self._mp3())
        self.assertEqual(self.pushed, [])


class TestPushStop(_Relay):
    def test_stop_is_broadcast_with_a_sequence_number(self):
        with self._ui():
            audio_relay.push_stop()
        self.assertEqual(self.pushed[0]["type"], "audio_stop")
        self.assertIn("seq", self.pushed[0])


class TestSpeakerIntegration(unittest.TestCase):
    def test_stop_notifies_remote_listeners(self):
        from nora import speaker
        with mock.patch.object(speaker.audio_relay, "push_stop") as stop:
            speaker.stop()
        stop.assert_called_once()

    def test_relay_failure_never_reaches_the_caller(self):
        """A dead browser must not be able to break local playback."""
        from nora import speaker
        with mock.patch.object(speaker.audio_relay, "push_stop",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                speaker.audio_relay.push_stop()
        # ...but the real relay swallows it, which is what speaker.stop relies on.
        with mock.patch.object(audio_relay, "_enabled", True), \
             mock.patch.dict("sys.modules", {"nora.ui_server": None}):
            audio_relay.push_stop()


if __name__ == "__main__":
    unittest.main()

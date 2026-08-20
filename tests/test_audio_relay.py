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

    def _ui(self, clients=True, push=None):
        # Patch the module itself, not sys.modules: `from nora import ui_server`
        # resolves through the already-imported package attribute, so a
        # sys.modules entry is ignored once any other test has imported it.
        from nora import ui_server
        return mock.patch.multiple(
            ui_server,
            has_ws_clients=mock.Mock(return_value=clients),
            ws_push=mock.Mock(side_effect=push or self.pushed.append),
        )

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
        def _explode(_msg):
            raise RuntimeError("socket exploded")
        with self._ui(push=_explode):
            audio_relay.push_chunk(self._mp3())   # must not raise

    def test_disabled_relay_sends_nothing(self):
        with mock.patch.object(audio_relay, "_enabled", False), self._ui():
            audio_relay.push_chunk(self._mp3())
        self.assertEqual(self.pushed, [])


class TestMimeSniffing(unittest.TestCase):
    """speaker.py names every chunk .mp3 whatever the backend wrote into it."""

    def test_kokoro_wav_is_not_labelled_mpeg(self):
        wav = b"RIFF" + b"\x24\x28\x01\x00" + b"WAVEfmt "
        self.assertEqual(audio_relay.sniff_mime(wav), "audio/wav")

    def test_real_mp3_frames(self):
        self.assertEqual(audio_relay.sniff_mime(b"\xff\xfb\x90\x00"), "audio/mpeg")
        self.assertEqual(audio_relay.sniff_mime(b"ID3\x04\x00"), "audio/mpeg")

    def test_ogg_and_flac(self):
        self.assertEqual(audio_relay.sniff_mime(b"OggS\x00\x02"), "audio/ogg")
        self.assertEqual(audio_relay.sniff_mime(b"fLaC\x00\x00"), "audio/flac")

    def test_pushed_chunk_carries_the_sniffed_mime(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.write(b"RIFF" + b"\x24\x28\x01\x00" + b"WAVEfmt " + b"\x00" * 32)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))

        pushed = []
        from nora import ui_server
        with mock.patch.object(audio_relay, "_enabled", True), \
             mock.patch.multiple(ui_server,
                                 has_ws_clients=mock.Mock(return_value=True),
                                 ws_push=mock.Mock(side_effect=pushed.append)):
            audio_relay.push_chunk(tmp.name, "kokoro sentence")

        self.assertEqual(pushed[0]["mime"], "audio/wav")


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
        from nora import speaker, ui_server

        def _explode(_msg):
            raise RuntimeError("socket exploded")

        with mock.patch.object(audio_relay, "_enabled", True), \
             mock.patch.multiple(ui_server,
                                 has_ws_clients=mock.Mock(return_value=True),
                                 ws_push=mock.Mock(side_effect=_explode)):
            speaker.stop()   # must return normally despite the relay throwing


if __name__ == "__main__":
    unittest.main()

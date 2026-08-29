"""Tests for the Telegram gateway and the headless turn behind it.

Two properties matter more than the plumbing.

*The allowlist is closed by default.* On the other end of this socket is a
process that can drive the user's desktop. A bot token is a bearer credential,
so the interesting test is not "an allowed chat gets through" but "an empty
allowlist refuses to start at all".

*Confirmation does not travel.* `handle_text` must refuse anything the security
policy flags rather than accepting a typed "yes" — whoever is holding the phone
is not necessarily whoever owns the machine. That refusal is the one behaviour
here worth being strict about.

Stdlib unittest only — run with:  python -m unittest tests.test_gateway -v
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from nora.gateway import core, telegram
from nora.schemas import ActionStep, IntentResponse, StepResult


def _cfg(**over):
    """A config stub with just the keys the gateway reads."""
    base = {"enabled": True, "allowed_chat_ids": [42], "token_env": "TG_TEST_TOKEN"}
    base.update(over)
    return base


class AllowlistTest(unittest.TestCase):
    def setUp(self) -> None:
        telegram._rejected.clear()

    def test_listed_chat_is_allowed(self) -> None:
        with mock.patch.object(telegram, "_cfg", lambda: _cfg()):
            self.assertTrue(telegram._allowed(42))

    def test_unlisted_chat_is_refused(self) -> None:
        with mock.patch.object(telegram, "_cfg", lambda: _cfg()):
            self.assertFalse(telegram._allowed(999))

    def test_empty_allowlist_refuses_everyone(self) -> None:
        """Closed by default — the failure mode of getting this backwards is bad."""
        with mock.patch.object(telegram, "_cfg", lambda: _cfg(allowed_chat_ids=[])):
            self.assertFalse(telegram._allowed(42))
            self.assertFalse(telegram._allowed(0))

    def test_string_ids_from_yaml_still_match(self) -> None:
        with mock.patch.object(telegram, "_cfg", lambda: _cfg(allowed_chat_ids=["42"])):
            self.assertTrue(telegram._allowed(42))

    def test_start_refuses_without_allowlist(self) -> None:
        with mock.patch.object(telegram, "_cfg", lambda: _cfg(allowed_chat_ids=[])), \
             mock.patch.object(telegram, "_token", lambda: "a-token"):
            self.assertFalse(telegram.start())

    def test_start_refuses_without_token(self) -> None:
        with mock.patch.object(telegram, "_cfg", lambda: _cfg()), \
             mock.patch.object(telegram, "_token", lambda: ""):
            self.assertFalse(telegram.start())

    def test_disabled_does_not_start(self) -> None:
        with mock.patch.object(telegram, "_cfg", lambda: _cfg(enabled=False)):
            self.assertFalse(telegram.start())

    def test_unauthorised_chat_is_told_exactly_once(self) -> None:
        update = {"message": {"chat": {"id": 999}, "text": "hello"}}
        with mock.patch.object(telegram, "_cfg", lambda: _cfg()), \
             mock.patch.object(telegram, "send") as send:
            telegram._handle(update, "tok")
            telegram._handle(update, "tok")
            telegram._handle(update, "tok")
        self.assertEqual(send.call_count, 1, "unauthorised chat can spam replies")
        self.assertIn("999", send.call_args[0][1])


class HeadlessTurnTest(unittest.TestCase):
    def test_confirmation_is_refused_not_accepted(self) -> None:
        """The one thing a remote message must never be able to do."""
        intent = IntentResponse(
            intent="delete", steps=[ActionStep(action="delete_file", parameters={})])
        with mock.patch("nora.fast_path.resolve", return_value=None), \
             mock.patch("nora.dialogue.classify"), \
             mock.patch("nora.conversation.should_handle", return_value=False), \
             mock.patch("nora.memory.get_context_summary", return_value={}), \
             mock.patch("nora.intent_parser.parse_intent", return_value=intent), \
             mock.patch("nora.security.check_steps", return_value=(False, True)), \
             mock.patch("nora.command_engine.execute") as execute:
            reply = asyncio.run(core.handle_text("delete my homework"))

        execute.assert_not_called()
        self.assertIn("out loud", reply)
        self.assertIn("delete_file", reply)

    def test_blocked_action_is_refused(self) -> None:
        intent = IntentResponse(
            intent="x", steps=[ActionStep(action="shutdown", parameters={})])
        with mock.patch("nora.fast_path.resolve", return_value=None), \
             mock.patch("nora.dialogue.classify"), \
             mock.patch("nora.conversation.should_handle", return_value=False), \
             mock.patch("nora.memory.get_context_summary", return_value={}), \
             mock.patch("nora.intent_parser.parse_intent", return_value=intent), \
             mock.patch("nora.security.check_steps", return_value=(True, False)), \
             mock.patch("nora.command_engine.execute") as execute:
            reply = asyncio.run(core.handle_text("shut down"))

        execute.assert_not_called()
        self.assertIn("blocked", reply)

    def test_ordinary_command_runs_and_reports(self) -> None:
        intent = IntentResponse(
            intent="music", steps=[ActionStep(action="pause_music", parameters={})])

        async def _execute(_i):
            return [StepResult(action="pause_music", success=True, message="Paused.")]

        with mock.patch("nora.fast_path.resolve", return_value=None), \
             mock.patch("nora.dialogue.classify"), \
             mock.patch("nora.conversation.should_handle", return_value=False), \
             mock.patch("nora.memory.get_context_summary", return_value={}), \
             mock.patch("nora.intent_parser.parse_intent", return_value=intent), \
             mock.patch("nora.security.check_steps", return_value=(False, False)), \
             mock.patch("nora.command_engine.execute", _execute), \
             mock.patch("nora.context.add_session_turn"):
            reply = asyncio.run(core.handle_text("pause the music"))

        self.assertEqual(reply, "Paused.")

    def test_fast_path_shortcut_skips_the_model(self) -> None:
        shortcut = IntentResponse(intent="chat", steps=[], response="Half four.")
        with mock.patch("nora.fast_path.resolve", return_value=shortcut), \
             mock.patch("nora.intent_parser.parse_intent") as parse:
            reply = asyncio.run(core.handle_text("what's the time"))
        parse.assert_not_called()
        self.assertEqual(reply, "Half four.")

    def test_empty_input_is_a_no_op(self) -> None:
        self.assertEqual(asyncio.run(core.handle_text("")), "")
        self.assertEqual(asyncio.run(core.handle_text("   ")), "")


class VoiceMemoTest(unittest.TestCase):
    def test_missing_ffmpeg_degrades_quietly(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(core.decode_audio(b"\x00\x01"))

    def test_decode_returns_float32_mono(self) -> None:
        import numpy as np

        fake = np.array([0.1, -0.2, 0.3], dtype=np.float32)
        completed = mock.Mock(returncode=0, stdout=fake.tobytes(), stderr=b"")
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             mock.patch("subprocess.run", return_value=completed):
            out = core.decode_audio(b"fake-ogg-bytes")

        self.assertIsNotNone(out)
        self.assertEqual(out.dtype, np.float32)
        np.testing.assert_allclose(out, fake)

    def test_ffmpeg_failure_returns_none(self) -> None:
        completed = mock.Mock(returncode=1, stdout=b"", stderr=b"bad input")
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             mock.patch("subprocess.run", return_value=completed):
            self.assertIsNone(core.decode_audio(b"junk"))

    def test_voice_memo_echoes_transcript_then_answers(self) -> None:
        """A Whisper mishearing must be visible, not silently acted on."""
        update = {"message": {"chat": {"id": 42}, "voice": {"file_id": "f1"}}}
        with mock.patch.object(telegram, "_cfg", lambda: _cfg()), \
             mock.patch.object(telegram, "_transcribe_voice", return_value="pause the music"), \
             mock.patch("nora.gateway.core.handle_text",
                        new=mock.AsyncMock(return_value="Paused.")), \
             mock.patch.object(telegram, "send") as send:
            telegram._handle(update, "tok")

        said = [c[0][1] for c in send.call_args_list]
        self.assertEqual(said, ["heard: pause the music", "Paused."])

    def test_undecodable_memo_says_so(self) -> None:
        update = {"message": {"chat": {"id": 42}, "voice": {"file_id": "f1"}}}
        with mock.patch.object(telegram, "_cfg", lambda: _cfg()), \
             mock.patch.object(telegram, "_transcribe_voice", return_value=""), \
             mock.patch.object(telegram, "send") as send:
            telegram._handle(update, "tok")
        self.assertIn("couldn't make out", send.call_args[0][1])


class PollingTest(unittest.TestCase):
    def test_long_poll_param_is_not_the_socket_timeout(self) -> None:
        """These are different things; conflating them busy-loops the poller."""
        seen = {}

        def fake_call(method, token, http_timeout=40, **params):
            seen["http_timeout"] = http_timeout
            seen["params"] = params
            telegram._stop.set()
            return []

        telegram._stop.clear()
        with mock.patch.object(telegram, "_call", fake_call):
            telegram._loop("tok")

        self.assertEqual(seen["params"]["timeout"], telegram._POLL_TIMEOUT)
        self.assertGreater(seen["http_timeout"], telegram._POLL_TIMEOUT)

    def test_failed_poll_backs_off(self) -> None:
        calls = []

        def fake_call(method, token, http_timeout=40, **params):
            calls.append(1)
            if len(calls) >= 3:
                telegram._stop.set()
            return None

        telegram._stop.clear()
        with mock.patch.object(telegram, "_call", fake_call), \
             mock.patch.object(telegram._stop, "wait", wraps=telegram._stop.wait) as wait:
            telegram._loop("tok")

        delays = [c[0][0] for c in wait.call_args_list]
        self.assertEqual(delays[:2], [1.0, 2.0], "poller did not back off")


if __name__ == "__main__":
    unittest.main()

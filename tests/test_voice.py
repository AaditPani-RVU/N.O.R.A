"""Phase 6: voice turns from a device, answered in NORA's own voice.

The phone recognises speech itself and sends text; these tests cover what
the core does with a voice turn: each line of the answer is also synthesised
and streamed as PCM behind its `say`, the first chunk is cut short so audio
starts early, barge-in stops synthesis, a core that can't synthesise leaves
the phone to speak for itself, and the phone's latency measurements are kept.

The hub, the socket and the fake phone are real. The turn is stubbed (the
model isn't what's under test) and so is Kokoro: a fake synthesiser returns
recognisable PCM, and can be made slow or broken.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from nora.hub import voice

try:
    from tests.test_hub import HubTestCase
except ImportError:          # run from inside tests/
    from test_hub import HubTestCase


class ChunksTest(unittest.TestCase):
    def test_short_answer_is_one_chunk(self) -> None:
        self.assertEqual(voice.chunks("Your phone is at 64 percent."), ["Your phone is at 64 percent."])

    def test_sentences_are_separate_chunks(self) -> None:
        self.assertEqual(voice.chunks("Done. The alarm is set for six thirty! Anything else?"),
                         ["Done.", "The alarm is set for six thirty!", "Anything else?"])

    def test_a_long_first_sentence_is_cut_at_its_first_clause(self) -> None:
        text = ("Here is what is due today, you have the assignment to submit and a "
                "reminder at six to call the bank. That's all.")
        self.assertEqual(voice.chunks(text), [
            "Here is what is due today,",
            "you have the assignment to submit and a reminder at six to call the bank.",
            "That's all."])

    def test_a_clause_too_early_is_not_a_cut(self) -> None:
        # "Okay," is 5 characters: too short to be worth its own synthesis.
        text = "Okay, " + "the thing you asked about is somewhere in the long list of items, I think."
        got = voice.chunks(text)
        self.assertGreaterEqual(len(got[0]), voice.FIRST_MIN)
        self.assertEqual(" ".join(got), text)

    def test_blank(self) -> None:
        self.assertEqual(voice.chunks("   "), [])


class FrameTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        frame = voice.pcm_frame(7, b"\x01\x02\x03\x04")
        self.assertEqual(frame[0], voice.TAG_PCM)
        self.assertEqual(voice.parse_frame(frame), (7, b"\x01\x02\x03\x04"))

    def test_rejects_other_frames(self) -> None:
        for bad in (b"", b"\x01\x00", b"\x02\x00\x00\x00\x01ab"):
            with self.assertRaises(ValueError):
                voice.parse_frame(bad)


def _pcm_for(text: str) -> bytes:
    """Recognisable fake audio: the text, repeated to a plausible size."""
    return (text.encode() * 400)[: max(len(text) * 400, 2)]


class _Turns:
    """Stands in for `pipeline.handle_turn`: says the lines it was given."""

    def __init__(self, lines: list[str], pause: float = 0.0) -> None:
        self.lines = lines
        self.pause = pause

    async def __call__(self, text, deps, rms=0.0, channel=None):
        from nora.pipeline import TurnOutcome
        for line in self.lines:
            channel.speak(line, mood="chat")
            if self.pause:
                await asyncio.sleep(self.pause)
        return TurnOutcome(kind="chat", text=text)


class VoiceTurnTest(HubTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from nora import pipeline, wiring
        self.synth_calls: list[str] = []
        self.synth_delay = 0.0
        self.synth_fails = False

        def synth(text: str, rate: str):
            self.synth_calls.append(text)
            if self.synth_delay:
                time.sleep(self.synth_delay)
            return None if self.synth_fails else (_pcm_for(text), voice.KOKORO_RATE)

        for p in (mock.patch.object(voice, "_kokoro", synth),
                  mock.patch.object(voice, "core_voice_available", lambda: True),
                  mock.patch.object(voice, "rate_for", lambda mood: "+20%"),
                  mock.patch.object(wiring, "build", lambda **_kw: None)):
            p.start()
            self.addCleanup(p.stop)
        self.pipeline = pipeline

    def turns(self, lines: list[str], pause: float = 0.0):
        return mock.patch.object(self.pipeline, "handle_turn", _Turns(lines, pause))

    async def test_each_line_is_announced_then_streamed(self) -> None:
        phone = await self.paired(name="pixel")
        lines = ["Your phone is at 64 percent.", "It's charging. Anything else?"]
        with self.turns(lines):
            said = await phone.say("what's my battery", voice={"tts": "core"})
            await self.until(lambda: len(phone.audio_end) == 2)
        self.assertEqual(said, lines)
        streams = [s["audio"]["stream"] for s in phone.says]
        self.assertEqual(len(set(streams)), 2)
        for body in phone.says:
            self.assertEqual(body["audio"]["format"], "pcm_s16le")
            self.assertEqual(body["audio"]["rate"], voice.KOKORO_RATE)
        # The audio is exactly the chunks of each line, in order.
        for stream, line in zip(streams, lines):
            want = b"".join(_pcm_for(c) for c in voice.chunks(line))
            self.assertEqual(bytes(phone.audio[stream]), want)
            self.assertEqual(phone.audio_end[stream], {"stream": stream, "ok": True,
                                                      "sent": True, "cancelled": False})

    async def test_long_audio_is_split_into_frames_under_the_limit(self) -> None:
        phone = await self.paired()
        line = "x" * 200 + "."     # 80 kB of fake PCM: several frames
        with self.turns([line]):
            await phone.say("talk a lot", voice={"tts": "core"})
            await self.until(lambda: len(phone.audio_end) == 1)
        stream = phone.says[0]["audio"]["stream"]
        self.assertEqual(len(phone.audio[stream]), len(_pcm_for(line)))
        self.assertGreater(len(_pcm_for(line)), voice.FRAME_BYTES)

    async def test_a_typed_turn_gets_no_audio(self) -> None:
        phone = await self.paired()
        with self.turns(["Typed answer."]):
            await phone.say("hello")
        await asyncio.sleep(0.1)
        self.assertNotIn("audio", phone.says[0])
        self.assertEqual(phone.audio, {})
        self.assertEqual(self.synth_calls, [])

    async def test_phone_voice_asks_for_no_audio(self) -> None:
        phone = await self.paired()
        with self.turns(["Answer."]):
            await phone.say("hello", voice={"tts": "phone"})
        await asyncio.sleep(0.1)
        self.assertNotIn("audio", phone.says[0])
        self.assertEqual(self.synth_calls, [])

    async def test_without_kokoro_the_phone_speaks_for_itself(self) -> None:
        phone = await self.paired()
        with self.turns(["Answer."]), mock.patch.object(voice, "core_voice_available", lambda: False):
            await phone.say("hello", voice={"tts": "core"})
        await asyncio.sleep(0.1)
        self.assertNotIn("audio", phone.says[0])
        self.assertEqual(phone.audio, {})

    async def test_a_failed_synthesis_says_so(self) -> None:
        phone = await self.paired()
        self.synth_fails = True
        with self.turns(["Answer."]):
            await phone.say("hello", voice={"tts": "core"})
            await self.until(lambda: len(phone.audio_end) == 1)
        stream = phone.says[0]["audio"]["stream"]
        self.assertEqual(phone.audio_end[stream]["ok"], False)
        self.assertEqual(phone.audio_end[stream]["sent"], False)

    async def test_barge_in_stops_synthesis_but_not_the_turn(self) -> None:
        phone = await self.paired()
        self.synth_delay = 0.15
        lines = ["First thing. Second thing. Third thing.", "Another line.", "And one more."]
        sent: list[str] = []
        with self.turns(lines, pause=0.4):
            turn = asyncio.create_task(phone.say("go on", voice={"tts": "core"}, on_sent=sent.append))
            await self.until(lambda: phone.audio)          # the first audio arrived
            await phone.barge_in(sent[0])
            said = await turn
            await self.until(lambda: len(phone.audio_end) == sum("audio" in b for b in phone.says))
        # Every line still arrived as text.
        self.assertEqual(said, lines)
        # Synthesis stopped: not every chunk was made, and the rest are marked cancelled.
        total = sum(len(voice.chunks(l)) for l in lines)
        self.assertLess(len(self.synth_calls), total)
        self.assertTrue(any(e["cancelled"] for e in phone.audio_end.values()))
        last = phone.says[-1]
        # A line said after the barge-in isn't queued for synthesis at all.
        self.assertNotIn("audio", last)

    async def test_disconnect_cancels_synthesis(self) -> None:
        phone = await self.paired()
        self.synth_delay = 0.2
        with self.turns(["One. Two. Three. Four. Five."]):
            task = asyncio.create_task(phone.say("go", voice={"tts": "core"}))
            await self.until(lambda: self.synth_calls)
            await phone.close()
            task.cancel()
        await asyncio.sleep(0.6)
        self.assertLess(len(self.synth_calls), 5)


class LatencyEventTest(HubTestCase):
    async def test_voice_turn_event_is_kept(self) -> None:
        path = Path(tempfile.mkdtemp()) / "lat.jsonl"
        phone = await self.paired(name="pixel")
        with mock.patch.object(voice, "LATENCY_PATH", path):
            await phone._send("event", {"name": "voice.turn", "occurred_at": 0, "data": {
                "speech_end_ms": 0, "stt_ms": 310, "core_first_say_ms": 900,
                "first_audio_ms": 1320, "tts": "phone", "route": "bluetooth",
                "junk": "x" * 1000, "evil": True}})
            await self.until(path.exists)
        row = json.loads(path.read_text().splitlines()[0])
        self.assertEqual(row["device"], phone.device_id)
        self.assertEqual(row["first_audio_ms"], 1320)
        self.assertEqual(row["tts"], "phone")
        self.assertNotIn("junk", row)
        self.assertNotIn("evil", row)


class SynthPcmTest(unittest.TestCase):
    def test_pcm_is_little_endian_16_bit(self) -> None:
        import numpy as np
        from nora import tts_local

        class Engine:
            def create(self, text, voice, speed):
                return np.array([0.0, 1.0, -1.0, 2.0], dtype="float32"), 24000

        with mock.patch.object(tts_local, "enabled", lambda: True), \
                mock.patch.object(tts_local, "_get_engine", lambda: Engine()):
            pcm, rate = tts_local.synth_pcm("hi", "+20%")
        self.assertEqual(rate, 24000)
        self.assertEqual(np.frombuffer(pcm, "<i2").tolist(), [0, 32767, -32767, 32767])

    def test_none_when_kokoro_is_off(self) -> None:
        from nora import tts_local
        with mock.patch.object(tts_local, "enabled", lambda: False):
            self.assertIsNone(tts_local.synth_pcm("hi", "+20%"))


if __name__ == "__main__":
    unittest.main()

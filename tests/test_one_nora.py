"""Sharp Phase F: one NORA everywhere.

Exit criteria, as tests: a parity test fails if any channel skips the
transcript; no suggestion fires from a single session's burst; a memory can
be deleted from the phone and is gone from recall.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from nora import (ambient, cognitive_memory, dialogue, memories, neurosym_guard, pipeline,
                  proactive, session_index, speaker, text_input, wiring)
from nora.frustration import FrustrationTracker
from nora.hub import protocol, registry
from nora.hub.server import Hub, Session
from tests.test_pipeline_smoke import scripted, Turn

_SAID = "what time is it"            # the fast path answers it: no model needed


def _session(loop, device_id="d_phone") -> Session:
    dev = registry.Device(id=device_id, name="Pixel", platform="android", public_key=b"",
                          paired_at=0, approved_at=0, revoked_at=None, last_seen=None)
    return Session(device=dev, ws=None, loop=loop)


def _sent(session: Session) -> list[dict]:
    out = []
    while not session._outbox.empty():
        out.append(session._outbox.get_nowait())
    return out


class ChannelParityTest(unittest.TestCase):
    """The same words on every channel: the transcript, the session index,
    memory and the input policy all see the turn, and the transcript says
    which device it was on."""

    def setUp(self) -> None:
        dialogue._turns.clear()
        self.addCleanup(dialogue._turns.clear)
        self.guard = mock.patch.object(neurosym_guard, "check_input", wraps=neurosym_guard.check_input)
        self.checked = self.guard.start()
        self.addCleanup(self.guard.stop)

    def _laptop_speaker(self):
        # The real speaker records what it said itself; so does this stand-in.
        return mock.patch.object(speaker, "speak",
                                 side_effect=lambda text, mood=None, **_: dialogue.record_nora(text, kind=mood or "info"))

    def laptop_mic(self) -> None:
        with self._laptop_speaker():
            deps = Turn().deps()
            deps.speak = speaker.speak
            asyncio.run(pipeline.run_heard((_SAID, 0.0), deps))

    def dashboard(self) -> None:
        with self._laptop_speaker():
            deps = Turn().deps()
            deps.speak = speaker.speak
            text_input.submit(_SAID, "dashboard")
            heard = asyncio.run(pipeline._next_utterance(None, deps))
            asyncio.run(pipeline.run_heard(heard, deps))

    def telegram(self) -> None:
        from nora.gateway.core import handle_text
        asyncio.run(handle_text(_SAID, source="telegram"))

    def _phone(self, voice: dict | None) -> None:
        async def go():
            hub = Hub()
            session = _session(asyncio.get_running_loop())
            body = {"text": _SAID}
            if voice:
                body["voice"] = voice
            hub._dispatch(session, protocol.envelope("utterance", body))
            await asyncio.gather(*list(session._turns))
            return _sent(session)
        frames = asyncio.run(go())
        self.assertIn("turn.done", [f["type"] for f in frames])

    def phone_chat(self) -> None:
        self._phone(None)

    def phone_voice(self) -> None:
        self._phone({"tts": "android"})

    CHANNELS = {"laptop mic": ("laptop_mic", "local"), "dashboard": ("dashboard", "dashboard"),
                "telegram": ("telegram", "telegram"), "phone chat": ("phone_chat", "d_phone"),
                "phone voice": ("phone_voice", "d_phone")}

    def test_every_channel_records_the_turn_alike(self) -> None:
        for name, (run, device) in self.CHANNELS.items():
            with self.subTest(channel=name):
                dialogue._turns.clear()
                self.checked.reset_mock()
                turn = Turn()
                with scripted(turn), \
                        mock.patch.object(session_index, "record", wraps=session_index.record) as indexed:
                    getattr(self, run)()
                    remembered = cognitive_memory.record_knowledge
                    said_to_memory = [c.args[0] for c in remembered.call_args_list]

                users = [u for u in dialogue.history() if u.speaker == "user"]
                noras = [u for u in dialogue.history() if u.speaker == "nora"]
                # The transcript: the user's words once, NORA's answer once, both on this device.
                self.assertEqual([u.text for u in users], [_SAID])
                self.assertEqual(len(noras), 1, [u.text for u in noras])
                self.assertEqual({u.device for u in users + noras}, {device})
                # The session index: both sides.
                roles = [c.kwargs.get("role") for c in indexed.call_args_list]
                self.assertEqual(sorted(roles), ["nora", "user"])
                # Memory and the input policy.
                self.assertIn(_SAID, said_to_memory)
                self.checked.assert_called_with(_SAID)


class RoutineNeedsSeveralDaysTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="nora-routine-"))
        for p in (mock.patch.object(cognitive_memory, "_USER_MODEL_PATH", tmp / "model.json"),
                  mock.patch.object(cognitive_memory, "_user_model", None)):
            p.start()
            self.addCleanup(p.stop)
        self.said: list[str] = []
        for p in (mock.patch.object(proactive, "_callback", self.said.append),
                  mock.patch.object(proactive, "MIN_PATTERN_CONFIDENCE", 5),
                  mock.patch.object(proactive, "MIN_PATTERN_DAYS", 3),
                  mock.patch.object(proactive, "_last_suggestion_ts", 0.0)):
            p.start()
            self.addCleanup(p.stop)
        # A Saturday afternoon.
        self.when = datetime(2026, 10, 10, 15, 0)

    def _evaluate_at(self, when: datetime) -> None:
        class Now(datetime):
            @classmethod
            def now(cls, tz=None):
                return when
        with mock.patch.object(proactive, "datetime", Now):
            proactive._evaluate_proactive()

    def test_a_single_sessions_burst_suggests_nothing(self) -> None:
        for i in range(8):
            cognitive_memory._update_user_model(["lock_screen"], [], (self.when + timedelta(minutes=i)).timestamp())
        self._evaluate_at(self.when + timedelta(minutes=30))
        self.assertEqual(self.said, [])

    def test_the_same_habit_over_several_weeks_does(self) -> None:
        for week in range(3):
            for i in range(2):
                ts = (self.when - timedelta(weeks=week) + timedelta(minutes=i)).timestamp()
                cognitive_memory._update_user_model(["lock_screen"], [], ts)
        self._evaluate_at(self.when + timedelta(minutes=30))
        self.assertEqual(len(self.said), 1)
        self.assertIn("lock screen", self.said[0])

    def test_a_burst_of_a_sequence_is_not_a_workflow_habit(self) -> None:
        for i in range(12):
            cognitive_memory._update_user_model(["open_spotify", "spotify_shuffle"], [],
                                                (self.when + timedelta(minutes=i)).timestamp())
        patterns = cognitive_memory.get_behavioral_patterns()
        self.assertEqual(patterns["workflow_patterns"][0]["days"], 1)
        self._evaluate_at(datetime(2026, 10, 12, 9, 0))      # a Monday morning: no time pattern
        self.assertEqual(self.said, [])


class ForgetFromThePhoneTest(unittest.TestCase):
    FACT = "my locker code is 4417"

    def setUp(self) -> None:
        import chromadb
        tmp = Path(tempfile.mkdtemp(prefix="nora-memories-"))
        client = chromadb.EphemeralClient()
        suffix = str(time.time_ns())
        eps = client.get_or_create_collection(f"episodes{suffix}", metadata={"hnsw:space": "cosine"})
        kn = client.get_or_create_collection(f"knowledge{suffix}", metadata={"hnsw:space": "cosine"})
        for p in (mock.patch.object(cognitive_memory, "_get_collections", return_value=(eps, kn)),
                  mock.patch.object(cognitive_memory, "_embed", side_effect=cognitive_memory._tfidf_embed),
                  mock.patch.object(ambient, "_PATH", tmp / "knowledge.json")):
            p.start()
            self.addCleanup(p.stop)
        session_index._use_path_for_tests(tmp / "sessions.db")
        self.addCleanup(session_index._use_path_for_tests, Path(tempfile.mkdtemp()) / "s.db")
        dialogue._turns.clear()
        self.addCleanup(dialogue._turns.clear)

        # What one "remember that ..." turn leaves behind, store by store.
        said = f"remember that {self.FACT}"
        dialogue.record_user(said, kind="command")
        ambient.log_entry(said, source="command")
        cognitive_memory.record_knowledge(said, source="command")
        from nora.commands.cognitive_commands import inject_knowledge
        dialogue.record_nora(inject_knowledge(self.FACT))
        # And something else, which must survive.
        cognitive_memory.record_knowledge("my bike is the blue one", source="manual", tags=["user_injected"])
        dialogue.record_user("remember that my bike is the blue one", kind="command")

    def _phone(self, body: dict) -> dict:
        async def go():
            session = _session(asyncio.get_running_loop())
            msg = protocol.envelope("memories", body)
            Hub()._dispatch(session, msg)
            await asyncio.gather(*list(session._turns))
            frames = _sent(session)
            self.assertEqual([f["corr"] for f in frames], [msg["id"]])
            return frames[0]
        return asyncio.run(go())

    def test_the_phone_lists_what_nora_was_told_to_remember(self) -> None:
        reply = self._phone({"op": "list"})
        self.assertEqual(reply["type"], "memories")
        self.assertEqual({m["text"] for m in reply["body"]["items"]},
                         {self.FACT, "my bike is the blue one"})

    def test_a_memory_forgotten_from_the_phone_is_gone_from_recall(self) -> None:
        from nora.commands.recall import recall
        self.assertIn("4417", recall("locker code"))           # found before

        mid = next(m["id"] for m in memories.list_memories() if m["text"] == self.FACT)
        reply = self._phone({"op": "forget", "id": mid})
        self.assertTrue(reply["body"]["forgotten"])
        self.assertEqual([m["text"] for m in reply["body"]["items"]], ["my bike is the blue one"])

        self.assertNotIn("4417", recall("locker code"))
        self.assertNotIn("4417", json.dumps(cognitive_memory.semantic_search("locker code", n=10)))
        self.assertNotIn("4417", " ".join(u.text for u in dialogue.history()))
        self.assertNotIn("4417", json.dumps(ambient.search("locker", limit=10)))
        # The rest of memory is untouched.
        self.assertIn("blue", recall("bike"))

    def test_only_kept_facts_can_be_forgotten_by_id(self) -> None:
        _, kn = cognitive_memory._get_collections()
        logged = [i for i, m in zip(*[kn.get()[k] for k in ("ids", "metadatas")]) if m["source"] == "command"]
        self.assertFalse(memories.forget(logged[0])["ok"])
        self.assertFalse(memories.forget("kn_nonexistent")["ok"])

    def test_an_unknown_op_is_refused(self) -> None:
        reply = self._phone({"op": "wipe"})
        self.assertEqual(reply["type"], "error")
        self.assertEqual(reply["body"]["code"], protocol.INVALID_PARAMS)


if __name__ == "__main__":
    unittest.main()

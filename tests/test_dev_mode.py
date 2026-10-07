"""nora.dev: test turns run, but leave nothing behind to learn from."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest import mock

from nora import channel, cognitive_memory, dev, pipeline, session_index

try:
    from tests.test_hub import HubTestCase
except ImportError:          # run from inside tests/
    from test_hub import HubTestCase


def _in(ch: channel.Channel, fn, *args):
    token = channel.bind(ch)
    try:
        return fn(*args)
    finally:
        channel.unbind(token)


def _search(word: str) -> list[str]:
    return [e.text for e in session_index.search(word)]


class DevModeTest(HubTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        tmp = Path(tempfile.mkdtemp(prefix="nora-dev-"))
        session_index._use_path_for_tests(tmp / "sessions.db")
        self.addCleanup(session_index.close)
        p = mock.patch.object(dev, "DEV_PATH", tmp / "dev.json")
        p.start()
        self.addCleanup(p.stop)

    def test_a_test_turn_is_not_indexed_or_learned(self):
        test = channel.Channel("d_x", "device", print, test=True)
        real = channel.Channel("d_x", "device", print)
        _in(test, session_index.record, "zanzibar test utterance")
        _in(real, session_index.record, "zanzibar real utterance")
        self.assertEqual(_search("zanzibar"), ["zanzibar real utterance"])

        with mock.patch.object(cognitive_memory, "_get_collections") as coll:
            _in(test, cognitive_memory.record_episode, "open chrome", "open", ["open_app"], [])
            _in(test, cognitive_memory.record_knowledge, "my dog is called Milo")
        coll.assert_not_called()

    def test_dev_mode_marks_every_turn_until_it_ends(self):
        self.assertFalse(dev.is_test_turn())
        dev.turn_on(minutes=1)
        self.assertTrue(dev.is_test_turn())
        session_index.record("quokka said in dev mode")
        self.assertEqual(_search("quokka"), [])
        dev.turn_off()
        self.assertFalse(dev.is_test_turn())

        dev.DEV_PATH.write_text('{"until": %f}' % (time.time() - 1))   # expired
        self.assertFalse(dev.is_test_turn())

    async def test_the_device_marks_a_test_turn_on_the_utterance(self):
        seen: list[bool] = []

        async def fake_turn(text, deps, rms=0.0, channel=None):
            seen.append(channel.test)
            return pipeline.TurnOutcome(kind="chat", text=text)

        fake = await self.paired()
        with mock.patch.object(pipeline, "handle_turn", side_effect=fake_turn):
            await fake.say("hello", test=True)
            await fake.say("hello")
        self.assertEqual(seen, [True, False])

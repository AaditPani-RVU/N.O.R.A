"""The Device Hub, end to end, against the fake device.

Phase 2's exit criteria (NORA_DISTRIBUTED_PLAN.md §10), one test class each
where possible:

  * the fake device pairs, and only an approved, correctly-signed device
    gets in — revocation cuts it off live;
  * it advertises `test.echo`, which appears in the LLM's action block while
    connected and leaves it on disconnect;
  * the LLM can call it — from the device's own typed turn and from a local
    voice turn — and the result comes back as an ordinary StepResult;
  * confirmation round-trips over the socket, for a tier-2 capability and for
    a turn the policy wants confirmed; a decline stops it;
  * the audit log shows who asked (device, origin), where it ran, and who
    confirmed;
  * unprompted answers go to the device that asked, and wait for it if it is
    offline.

Only the LLM edge (`intent_parser.parse_intent`) and the durable side effects
from `test_pipeline_smoke` are stubbed. The socket, the handshake, the
signatures, the store, the command engine and the whole turn are real.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from nora import audit_log, channel, command_engine, delivery, jobs, store
from nora.hub import fake_device, protocol, registry
from nora.hub.server import Hub
from nora.schemas import ActionStep, IntentResponse, StepResult

try:
    from tests.test_pipeline_smoke import SIDE_EFFECTS
except ImportError:          # run from inside tests/
    from test_pipeline_smoke import SIDE_EFFECTS


def _intent(action: str, *, confirm: bool = False, **params) -> IntentResponse:
    return IntentResponse(intent=action, confidence=1.0, requires_confirmation=confirm,
                          steps=[ActionStep(action=action, parameters=params)])


# Inside NeuroSym's sandbox (the home directory); the handler is stubbed, so
# nothing is ever deleted.
_HOMEWORK = str(Path.home() / "homework.txt")


class HubTestCase(unittest.IsolatedAsyncioTestCase):
    """A fresh store, a hub on a free port, and helpers to get a device in."""

    async def asyncSetUp(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="nora-hub-"))
        env = mock.patch.dict(os.environ, {"NORA_STORE_PATH": str(tmp / "nora_core.db")})
        env.start()
        self.addCleanup(env.stop)
        audit = mock.patch.object(audit_log, "_LOG_PATH", tmp / "audit.jsonl")
        audit.start()
        self.addCleanup(audit.stop)
        audit_log._buffer.clear()
        registry.reset_for_tests()
        delivery.reset_for_tests()

        self.hub = Hub()
        port = await asyncio.get_running_loop().run_in_executor(
            None, self.hub.start, "127.0.0.1", 0)
        self.addCleanup(self.hub.stop)
        self.url = f"ws://127.0.0.1:{port}{protocol.PATH}"
        self.devices: list[fake_device.FakeDevice] = []

    async def asyncTearDown(self) -> None:
        for dev in self.devices:
            await dev.close()
        for dev in self.devices:
            if dev.device_id:
                command_engine.unregister_device(dev.device_id)

    async def paired(self, *, approve=lambda _r: True, name="fake",
                     capabilities=None) -> fake_device.FakeDevice:
        """Pair, approve and connect a fake device; wait until it is registered."""
        code, _ = registry.create_code()
        dev = fake_device.FakeDevice(self.url, name=name, approve=approve,
                                     capabilities=capabilities)
        reply = await dev.pair(code)
        self.assertEqual(reply["type"], "paired", reply)
        self.assertTrue(registry.approve(dev.device_id))
        welcome = await dev.connect()
        self.assertEqual(welcome["type"], "welcome", welcome)
        self.devices.append(dev)
        await self.until(lambda: dev.device_id in self.hub.sessions())
        return dev

    async def until(self, cond, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while not cond():
            if time.time() > deadline:
                self.fail("condition not reached")
            await asyncio.sleep(0.02)


class PairingTest(HubTestCase):
    async def test_pending_until_approved(self) -> None:
        code, _ = registry.create_code()
        dev = fake_device.FakeDevice(self.url)
        reply = await dev.pair(code)
        self.assertEqual(reply["status"], "pending")

        refused = await dev.connect()
        self.assertEqual(refused["type"], "error")
        self.assertNotIn(dev.device_id, self.hub.sessions())

        registry.approve(dev.device_id)
        self.assertEqual((await dev.connect())["type"], "welcome")
        self.devices.append(dev)

    async def test_code_is_single_use(self) -> None:
        code, _ = registry.create_code()
        self.assertEqual((await fake_device.FakeDevice(self.url).pair(code))["type"], "paired")
        self.assertEqual((await fake_device.FakeDevice(self.url).pair(code))["type"], "error")

    async def test_code_is_forgiving_to_type(self) -> None:
        code, _ = registry.create_code()
        typed = code.replace("-", "").lower().replace("0", "o").replace("1", "l")
        self.assertEqual((await fake_device.FakeDevice(self.url).pair(typed))["type"], "paired")

    async def test_expired_code_is_refused(self) -> None:
        code, _ = registry.create_code(ttl=-1)
        self.assertEqual((await fake_device.FakeDevice(self.url).pair(code))["type"], "error")

    async def test_repeated_bad_guesses_void_live_codes(self) -> None:
        code, _ = registry.create_code()
        for _ in range(registry.MAX_FAILED_PAIRS):
            await fake_device.FakeDevice(self.url).pair("AAAA-AAAA")
        self.assertEqual((await fake_device.FakeDevice(self.url).pair(code))["type"], "error")

    async def test_non_p256_key_is_refused(self) -> None:
        from cryptography.hazmat.primitives.asymmetric import ec
        code, _ = registry.create_code()
        dev = fake_device.FakeDevice(self.url, key=ec.generate_private_key(ec.SECP384R1()))
        self.assertEqual((await dev.pair(code))["type"], "error")
        self.assertEqual(registry.listing(), [])


class HandshakeTest(HubTestCase):
    async def test_another_key_cannot_claim_a_device_id(self) -> None:
        real = await self.paired()
        impostor = fake_device.FakeDevice(self.url, device_id=real.device_id)
        self.assertEqual((await impostor.connect())["type"], "error")

    async def test_unknown_device_is_refused(self) -> None:
        dev = fake_device.FakeDevice(self.url, device_id="d_nope")
        self.assertEqual((await dev.connect())["type"], "error")

    async def test_revocation_disconnects_live(self) -> None:
        dev = await self.paired()
        self.assertIn("test.echo", command_engine.get_available_actions())
        registry.revoke(dev.device_id)
        await self.until(lambda: dev.device_id not in self.hub.sessions())
        self.assertNotIn("test.echo", command_engine.get_available_actions())
        self.assertEqual((await dev.connect())["type"], "error")

    async def test_wrong_path_is_not_served(self) -> None:
        import websockets
        with self.assertRaises(Exception):
            await websockets.connect(self.url.replace(protocol.PATH, "/other"))


class CapabilityRegistrationTest(HubTestCase):
    async def test_capabilities_join_and_leave_the_prompt(self) -> None:
        dev = await self.paired()
        block = command_engine.get_action_signatures()
        self.assertIn("test.echo(text)", block)
        self.assertIn("Device capabilities", block)
        self.assertEqual(command_engine.get_action_meta("test.echo").device, dev.device_id)

        await dev.close()
        await self.until(lambda: dev.device_id not in self.hub.sessions())
        self.assertNotIn("test.echo", command_engine.get_action_signatures())

    async def test_a_device_cannot_shadow_a_local_command(self) -> None:
        local = lambda: "core"
        with mock.patch.dict(command_engine._meta, {
                "core.thing": command_engine.CommandMeta(sig="core.thing()")}), \
             mock.patch.dict(command_engine._registry, {"core.thing": local}):
            await self.paired(capabilities=[{"name": "core.thing", "tier": 0}])
            self.assertIs(command_engine._registry["core.thing"], local)
            self.assertEqual(command_engine.get_action_meta("core.thing").device, "")

    async def test_second_device_cannot_take_a_name(self) -> None:
        first = await self.paired(name="first")
        await self.paired(name="second")
        self.assertEqual(command_engine.get_action_meta("test.echo").device, first.device_id)

    async def test_bad_manifest_entries_are_dropped(self) -> None:
        await self.paired(capabilities=[
            {"name": "nodot", "tier": 0},
            {"name": "test.never", "tier": 4},
            {"name": "test.badschema", "tier": 0, "params_schema": {"type": 12}},
            {"name": "test.ok", "tier": 0},
        ])
        actions = command_engine.get_available_actions()
        self.assertIn("test.ok", actions)
        for name in ("nodot", "test.never", "test.badschema"):
            self.assertNotIn(name, actions)


class _TurnStubs:
    """The LLM edge scripted, the durable side effects stubbed."""

    def stubs(self, intent: IntentResponse) -> contextlib.ExitStack:
        stack = contextlib.ExitStack()
        for target, value in SIDE_EFFECTS:
            if target == "nora.command_engine._log_audit":
                continue      # the audit row is what these tests check
            stack.enter_context(mock.patch(target, autospec=True, return_value=value))
        stack.enter_context(mock.patch("nora.fast_path.resolve", return_value=None))
        stack.enter_context(mock.patch("nora.dialogue.classify"))
        stack.enter_context(mock.patch("nora.conversation.should_handle", return_value=False))
        stack.enter_context(mock.patch("nora.intent_parser.parse_intent", return_value=intent))
        return stack


class InvokeTest(_TurnStubs, HubTestCase):
    async def test_device_turn_calls_its_own_capability(self) -> None:
        dev = await self.paired()
        with self.stubs(_intent("test.echo", text="hello")):
            said = await dev.say("echo hello")
        self.assertIn("echo: hello", " ".join(said))
        self.assertEqual(len(dev.executed), 1)
        self.assertEqual(dev.executed[0]["origin"], "live_user")

        row = audit_log.get_last_n(1)[0]
        self.assertEqual(row["action"], "test.echo")
        self.assertEqual(row["device"], dev.device_id)
        self.assertEqual(row["origin"], "live_user")
        self.assertEqual(row["executed_on"], dev.device_id)
        self.assertTrue(row["turn_id"].startswith("t_"))

        [inv] = store.query("SELECT * FROM invocations")
        self.assertEqual(inv["status"], "ok")
        self.assertEqual(json.loads(inv["params"]), {"text": "hello"})

    async def test_local_voice_turn_reaches_the_device(self) -> None:
        """Desktop turn → phone capability: what "NORA, what's my phone
        battery?" from the laptop will be in Phase 3."""
        from nora import pipeline, wiring
        from nora.frustration import FrustrationTracker

        dev = await self.paired()
        spoken: list[str] = []

        async def no_confirm():
            raise AssertionError("tier 1 must not ask")

        with self.stubs(_intent("test.echo", text="from the laptop")):
            # Built inside the stubs: `build` binds parse_intent at call time.
            deps = wiring.build(listener=None, frustration=FrustrationTracker(),
                                speak=lambda t, **k: spoken.append(t), confirm=no_confirm)
            outcome = await pipeline.handle_turn("echo from the laptop", deps)
        self.assertEqual(outcome.kind, "executed")
        self.assertIn("echo: from the laptop", " ".join(spoken))
        row = audit_log.get_last_n(1)[0]
        self.assertEqual((row["device"], row["executed_on"]), ("local", dev.device_id))

    async def test_invalid_params_never_reach_the_device(self) -> None:
        dev = await self.paired()
        result = await self.hub.call(dev.device_id, "test.echo", {"text": "x", "extra": 1},
                                       channel=channel.Channel("local", "voice", print))
        self.assertEqual(result.error_code, protocol.INVALID_PARAMS)
        self.assertEqual(dev.executed, [])

    async def test_offline_device_fails_fast(self) -> None:
        result = await self.hub.call("d_gone", "test.echo", {"text": "x"})
        self.assertEqual(result.error_code, protocol.DEVICE_OFFLINE)
        self.assertFalse(result.success)

    async def test_kill_switch_stops_invocation(self) -> None:
        dev = await self.paired()
        await dev.set_kill(True)
        await self.until(lambda: "test.echo" not in command_engine.get_available_actions())
        result = await self.hub.call(dev.device_id, "test.echo", {"text": "x"},
                                       channel=channel.Channel("local", "voice", print))
        self.assertEqual(result.error_code, protocol.POLICY_BLOCKED)
        self.assertTrue(result.withheld)
        self.assertEqual(dev.executed, [])

        await dev.set_kill(False)
        await self.until(lambda: "test.echo" in command_engine.get_available_actions())


class ConfirmationTest(_TurnStubs, HubTestCase):
    async def test_tier2_capability_confirms_on_the_device(self) -> None:
        dev = await self.paired()
        with self.stubs(_intent("test.secret")):
            said = await dev.say("what's the secret")
        self.assertEqual(len(dev.confirm_requests), 1)
        self.assertEqual(dev.confirm_requests[0]["steps"][0]["action"], "test.secret")
        self.assertIn("marmalade", " ".join(said))
        row = audit_log.get_last_n(1)[0]
        self.assertEqual(row["confirmed_by"], dev.device_id)
        [inv] = store.query("SELECT confirmed_by, tier FROM invocations")
        self.assertEqual((inv["confirmed_by"], inv["tier"]), (dev.device_id, 2))

    async def test_decline_on_the_device_stops_it(self) -> None:
        dev = await self.paired(approve=lambda _r: False)
        with self.stubs(_intent("test.secret")):
            said = await dev.say("what's the secret")
        self.assertEqual(dev.executed, [])
        self.assertEqual(len(dev.confirm_requests), 1)
        self.assertNotIn("marmalade", " ".join(said))

    async def test_device_enforces_its_own_tier_table(self) -> None:
        """A confused or compromised core that thinks test.secret is tier 1
        still cannot get it run without a tap on the device."""
        dev = await self.paired()
        self.hub._sessions[dev.device_id].capabilities["test.secret"]["tier"] = 1
        result = await self.hub.call(dev.device_id, "test.secret", {},
                                       channel=channel.Channel("local", "voice", print))
        self.assertEqual(result.error_code, protocol.POLICY_BLOCKED)
        self.assertEqual(dev.confirm_requests, [])
        self.assertEqual(dev.executed, [])

    async def test_job_origin_is_one_tier_stricter(self) -> None:
        dev = await self.paired()
        result = await self.hub.call(dev.device_id, "test.echo", {"text": "x"})  # no channel
        self.assertTrue(result.success)
        self.assertEqual(len(dev.confirm_requests), 1, "tier 1 + job origin should confirm")
        self.assertEqual(dev.executed[0]["origin"], "job")

    async def test_policy_confirmation_round_trips_to_the_asking_device(self) -> None:
        dev = await self.paired()
        deleted: list[str] = []
        with self.stubs(_intent("delete_file", confirm=True, path=_HOMEWORK)), \
             mock.patch.dict(command_engine._registry,
                             {"delete_file": lambda path: deleted.append(path) or "Deleted."}), \
             mock.patch.dict(command_engine._meta, {"delete_file": command_engine.CommandMeta(
                 sig="delete_file(path)", risk="high", requires_confirmation=True)}):
            said = await dev.say("delete my homework")
        self.assertEqual(deleted, [_HOMEWORK])
        self.assertEqual(dev.confirm_requests[0]["steps"][0]["action"], "delete_file")
        self.assertIn("Deleted.", " ".join(said))
        self.assertEqual(audit_log.get_last_n(1)[0]["confirmed_by"], dev.device_id)

    async def test_policy_decline_on_the_device_cancels(self) -> None:
        dev = await self.paired(approve=lambda _r: False)
        deleted: list[str] = []
        with self.stubs(_intent("delete_file", confirm=True, path=_HOMEWORK)), \
             mock.patch.dict(command_engine._registry,
                             {"delete_file": lambda path: deleted.append(path) or "Deleted."}):
            await dev.say("delete my homework")
        self.assertEqual(deleted, [])
        self.assertEqual(len(dev.confirm_requests), 1)

    async def test_goodbye_from_a_device_is_not_a_shutdown(self) -> None:
        dev = await self.paired()
        with self.stubs(IntentResponse(intent="chat", steps=[], response="Bye for now.")):
            await dev.say("goodbye")
        # Reaching here with the hub still serving is the assertion: the turn
        # ran as an ordinary one instead of returning kind="exit".
        self.assertIn(dev.device_id, self.hub.sessions())


class DeliveryTest(HubTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        jobs.reset_for_tests(None)
        jobs.start()
        self.addCleanup(jobs.stop)

    def _submit_from(self, device_id: str, answer: str) -> str:
        token = channel.bind(channel.Channel(device_id, "device", print))
        try:
            return jobs.submit("your question about tides", lambda: answer)
        finally:
            channel.unbind(token)

    async def test_answer_goes_to_the_device_that_asked(self) -> None:
        dev = await self.paired()
        job_id = self._submit_from(dev.device_id, "High tide at six.")
        self.assertEqual(jobs.get(job_id).device, dev.device_id)
        await self.until(lambda: dev.notified)
        self.assertIn("High tide at six.", dev.notified[0]["body"])
        await self.until(lambda: jobs.get(job_id).delivered)

    async def test_held_while_offline_then_flushed_on_reconnect(self) -> None:
        dev = await self.paired()
        await dev.close()
        await self.until(lambda: dev.device_id not in self.hub.sessions())

        job_id = self._submit_from(dev.device_id, "High tide at six.")
        await self.until(lambda: jobs.get(job_id).status == jobs.STATUS_DONE)
        self.assertFalse(jobs.get(job_id).delivered)

        self.assertEqual((await dev.connect())["type"], "welcome")
        await self.until(lambda: dev.notified)
        self.assertIn("High tide at six.", dev.notified[0]["body"])
        await self.until(lambda: jobs.get(job_id).delivered)

    async def test_local_jobs_still_speak_locally(self) -> None:
        spoken: list[str] = []
        jobs.reset_for_tests(spoken.append)
        jobs.start()
        jobs.submit("your question about tides", lambda: "High tide at six.")
        self.assertTrue(jobs.drain(5))
        self.assertEqual(spoken, ["Back to your question about tides — High tide at six."])


class FakeDeviceTest(unittest.TestCase):
    """The device-side rules, without a socket."""

    def _invoke(self, dev, inv_id="01X", **body):
        base = {"capability": "test.echo", "params": {"text": "hi"},
                "deadline_ms": 8000, "origin": "live_user"}
        return dev._on_invoke(protocol.envelope("invoke", {**base, **body}, id_=inv_id))

    def test_replay_returns_the_stored_result_without_acting_twice(self) -> None:
        dev = fake_device.FakeDevice("ws://unused")
        first = self._invoke(dev)
        second = self._invoke(dev)
        self.assertEqual(first, second)
        self.assertEqual(len(dev.executed), 1)

    def test_past_deadline_is_expired(self) -> None:
        dev = fake_device.FakeDevice("ws://unused")
        msg = protocol.envelope("invoke", {"capability": "test.echo", "params": {},
                                           "deadline_ms": 10}, id_="01Y")
        msg["ts"] -= 1000
        self.assertEqual(dev._on_invoke(msg)["error"]["code"], protocol.EXPIRED)

    def test_live_user_only_capability_refuses_automation(self) -> None:
        caps = [{"name": "test.photo", "tier": 1, "requires_live_user": True}]
        dev = fake_device.FakeDevice("ws://unused", capabilities=caps)
        reply = self._invoke(dev, capability="test.photo", params={}, origin="automation")
        self.assertEqual(reply["error"]["code"], protocol.POLICY_BLOCKED)


class ProtocolTest(unittest.TestCase):
    def test_ulids_sort_by_time(self) -> None:
        a = protocol.new_id()
        time.sleep(0.002)
        b = protocol.new_id()
        self.assertEqual(len(a), 26)
        self.assertLess(a, b)

    def test_decode_rejects_malformed_frames(self) -> None:
        for frame in ("not json", "[]", '{"v":2,"id":"x","type":"ping"}',
                      '{"v":1,"type":"ping"}', '{"v":1,"id":"x","type":"ping","body":[]}',
                      b"\x00"):
            with self.assertRaises(protocol.ProtocolError, msg=repr(frame)):
                protocol.decode(frame)

    def test_unknown_params_are_rejected_by_default(self) -> None:
        entry = protocol.check_manifest_entry({
            "name": "x.y", "tier": 0,
            "params_schema": {"type": "object", "properties": {"a": {"type": "string"}}}})
        self.assertIsNone(protocol.validate_params(entry["params_schema"], {"a": "ok"}))
        self.assertIsNotNone(protocol.validate_params(entry["params_schema"], {"b": 1}))

    def test_signature_binds_device_and_context(self) -> None:
        from cryptography.hazmat.primitives.asymmetric import ec
        key = ec.generate_private_key(ec.SECP256R1())
        der = protocol.public_key_der(key.public_key())
        nonce = os.urandom(32)
        sig = protocol.sign(key, protocol.auth_payload(nonce, "d_a"))
        self.assertTrue(protocol.verify(der, sig, protocol.auth_payload(nonce, "d_a")))
        self.assertFalse(protocol.verify(der, sig, protocol.auth_payload(nonce, "d_b")))
        self.assertFalse(protocol.verify(der, sig, protocol.auth_payload(os.urandom(32), "d_a")))


if __name__ == "__main__":
    unittest.main()

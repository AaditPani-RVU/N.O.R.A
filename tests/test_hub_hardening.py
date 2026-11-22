"""Phase 10 hardening: the protocol fuzzed, and the link dropped on purpose.

No hypothesis in the venv, so the fuzzing is seeded `random`: the same
frames every run, many of them, from valid envelopes mutated one field at
a time and from plain garbage. What must hold:

  * the pure protocol functions only ever raise ProtocolError;
  * the hub survives any frame, before or after authentication: a bad
    frame may cost that socket its connection, never the hub;
  * after a storm of reconnects, overlapping ones included, there is one
    session per device, its capabilities registered once, and they leave
    the tool list when it goes.
"""
from __future__ import annotations

import asyncio
import json
import random
import string

import pytest

from nora import channel, command_engine
from nora.hub import fake_device, protocol, registry

try:
    from tests.test_hub import HubTestCase
except ImportError:          # run from inside tests/
    from test_hub import HubTestCase

SEED = 20261123


def _junk(rng: random.Random, depth: int = 0):
    kind = rng.randrange(9 if depth < 3 else 6)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice([True, False])
    if kind == 2:
        return rng.randint(-2**40, 2**40)
    if kind == 3:
        return rng.random() * rng.choice([1, 1e300, -1e-300])
    if kind == 4:
        return "".join(rng.choice(string.printable + "é‮\x00") for _ in range(rng.randrange(0, 40)))
    if kind == 5:
        return rng.choice(["", "x" * 70, "01J8", "phone.notify", "../../etc", "\ud800"[:0]])
    if kind == 6:
        return [_junk(rng, depth + 1) for _ in range(rng.randrange(0, 5))]
    return {str(_junk(rng, depth + 1))[:20]: _junk(rng, depth + 1) for _ in range(rng.randrange(0, 5))}


def _mutated_envelopes(rng: random.Random, n: int) -> list[str]:
    types = sorted(protocol.DEVICE_TYPES) + ["invoke", "hello", "auth", "pair", "nope"]
    out = []
    for _ in range(n):
        env = protocol.envelope(rng.choice(types), {"capability": "test.echo", "params": {"text": "x"}})
        field = rng.choice(["v", "id", "type", "body", "corr", "seq", "ts", None])
        if field:
            env[field] = _junk(rng)
        if rng.random() < 0.3:
            env["body"] = _junk(rng)
        try:
            out.append(json.dumps(env))
        except (TypeError, ValueError):
            out.append(str(env))
    return out


def _garbage(rng: random.Random, n: int) -> list:
    frames: list = []
    for _ in range(n):
        r = rng.random()
        if r < 0.3:
            frames.append("".join(rng.choice(string.printable) for _ in range(rng.randrange(0, 200))))
        elif r < 0.5:
            frames.append(bytes(rng.randrange(256) for _ in range(rng.randrange(0, 64))))
        elif r < 0.8:
            try:
                frames.append(json.dumps(_junk(rng)))
            except (TypeError, ValueError):
                frames.append("{")
        else:
            frames.append("{" * rng.randrange(1, 500))
    return frames


# ── the pure parts ───────────────────────────────────────────────────────────

def test_decode_only_ever_raises_protocol_errors():
    rng = random.Random(SEED)
    for frame in _mutated_envelopes(rng, 1500) + [f for f in _garbage(rng, 1500) if isinstance(f, str)]:
        try:
            msg = protocol.decode(frame)
        except protocol.ProtocolError:
            continue
        assert isinstance(msg, dict) and isinstance(msg["body"], dict)


def test_manifest_checks_only_ever_raise_protocol_errors():
    rng = random.Random(SEED + 1)
    base = {"name": "test.echo", "tier": 1, "description": "x",
            "params_schema": {"type": "object", "properties": {"text": {"type": "string"}}}}
    for _ in range(1500):
        entry = dict(base)
        for key in rng.sample(sorted(base) + ["requires_live_user", "available", "version"], rng.randrange(1, 4)):
            entry[key] = _junk(rng)
        if rng.random() < 0.2:
            entry = _junk(rng)
        try:
            out = protocol.check_manifest_entry(entry)
        except protocol.ProtocolError:
            continue
        except (TypeError, ValueError, OverflowError) as e:      # int("x") on a version, say
            pytest.fail(f"{type(e).__name__} on {entry!r}")
        assert 0 <= out["tier"] <= protocol.TIER_FORBIDDEN


def test_params_validation_never_raises():
    rng = random.Random(SEED + 2)
    schema = protocol.check_manifest_entry({
        "name": "phone.send_sms", "tier": 3,
        "params_schema": {"type": "object", "required": ["text"],
                          "properties": {"name": {"type": "string", "maxLength": 80},
                                         "text": {"type": "string", "maxLength": 1000}}}})["params_schema"]
    for _ in range(2000):
        params = _junk(rng)
        if not isinstance(params, dict):
            params = {"text": params}
        problem = protocol.validate_params(schema, protocol.fit_params(schema, params))
        assert problem is None or isinstance(problem, str)


# ── the live hub ─────────────────────────────────────────────────────────────

LIVE = channel.Channel("local", "voice", print)


class FuzzedHub(HubTestCase):
    async def _still_works(self) -> None:
        dev = await self.paired(name="after-the-storm")
        res = await self.hub.call(dev.device_id, "test.echo", {"text": "still here"}, channel=LIVE)
        self.assertTrue(res.success, res.message)
        self.assertEqual(res.message, "echo: still here")

    async def test_garbage_before_authentication(self) -> None:
        import websockets
        rng = random.Random(SEED + 3)
        for frame in _garbage(rng, 60) + _mutated_envelopes(rng, 60):
            try:
                async with websockets.connect(self.url, max_size=protocol.MAX_FRAME, open_timeout=3) as ws:
                    await ws.send(frame)
                    try:
                        await asyncio.wait_for(ws.recv(), 1)
                    except (asyncio.TimeoutError, Exception):
                        pass
            except Exception:
                pass                       # refused or closed: exactly what it may do
        await self._still_works()

    async def test_garbage_from_an_authenticated_device(self) -> None:
        rng = random.Random(SEED + 4)
        dev = await self.paired(name="fuzzer")
        frames = _mutated_envelopes(rng, 400) + _garbage(rng, 100)
        rng.shuffle(frames)
        for frame in frames:
            if dev.ws is None or dev.ws.close_code is not None:
                # The hub hung up on a bad frame; come back and keep going.
                await dev.close()
                if (await dev.connect())["type"] != "welcome":
                    break
            try:
                await dev.ws.send(frame)
            except Exception:
                pass
        await asyncio.sleep(0.3)
        await dev.close()
        await self._still_works()

    async def test_random_blob_chunks_never_make_a_photo(self) -> None:
        from nora import photos
        photos.reset_for_tests()
        rng = random.Random(SEED + 5)
        dev = await self.paired(name="blobs")
        for _ in range(200):
            body = {"blob_id": rng.choice(["b" * 8, "c" * 9, _junk(rng)]), "seq": rng.choice([0, 1, -1, _junk(rng)]),
                    "mime": rng.choice(["image/jpeg", "text/html", _junk(rng)]),
                    "data": rng.choice(["", "!!!", "QUJD", _junk(rng)]), "last": rng.choice([True, False, _junk(rng)])}
            try:
                await dev._send("blob.chunk", body)
            except Exception:
                break
        await asyncio.sleep(0.3)
        # Only a well-formed, in-order, image-typed blob may land; "QUJD" is "ABC".
        for p in list(photos._photos.values()):
            self.assertIn(p.mime, photos.MIMES)
        photos.reset_for_tests()
        await self._still_works()


class ReconnectChaos(HubTestCase):
    async def test_a_storm_of_reconnects_leaves_one_session(self) -> None:
        dev = await self.paired(name="flaky")
        key, device_id = dev.key, dev.device_id
        await dev.close()
        rng = random.Random(SEED + 6)
        clients: list[fake_device.FakeDevice] = []
        for _ in range(25):
            c = fake_device.FakeDevice(self.url, key=key, device_id=device_id)
            if (await c.connect())["type"] == "welcome":
                clients.append(c)
            # Sometimes the old socket lingers, sometimes it goes at once.
            if clients and rng.random() < 0.6:
                await clients.pop(rng.randrange(len(clients))).close()
            await asyncio.sleep(rng.random() * 0.03)
        last = fake_device.FakeDevice(self.url, key=key, device_id=device_id)
        self.assertEqual((await last.connect())["type"], "welcome")
        self.devices.append(last)
        await self.until(lambda: self.hub.sessions().count(device_id) == 1)
        for c in clients:
            await c.close()
        await asyncio.sleep(0.3)

        # The newest connection is the one in use, and it works.
        self.assertEqual(list(self.hub.sessions()).count(device_id), 1)
        await self.until(lambda: command_engine.get_action_meta("test.echo") is not None)
        res = await self.hub.call(device_id, "test.echo", {"text": "ok"}, channel=LIVE)
        self.assertTrue(res.success, res.message)

        await last.close()
        await self.until(lambda: device_id not in self.hub.sessions())
        await self.until(lambda: command_engine.get_action_meta("test.echo") is None)

    async def test_a_device_revoked_mid_storm_stays_out(self) -> None:
        dev = await self.paired(name="revoked")
        registry.revoke(dev.device_id)
        await self.until(lambda: dev.device_id not in self.hub.sessions(), timeout=6)
        for _ in range(5):
            again = fake_device.FakeDevice(self.url, key=dev.key, device_id=dev.device_id)
            self.assertEqual((await again.connect())["type"], "error")
        self.assertIsNone(command_engine.get_action_meta("test.echo"))

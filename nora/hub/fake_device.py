"""A fake device that speaks protocol v1 — for tests, and for poking a live core.

It is the Android app's stand-in until the app exists (Phase 3), so it does
what the plan requires of a *device*, not just what the core happens to send:

  * a P-256 key it generates itself, signing the challenge — no shared secret;
  * its own tier table: a tier ≥ 2 invocation without a confirmation it
    approved is refused (`POLICY_BLOCKED`), whatever the core says;
  * `requires_live_user` checked against the invocation's origin;
  * deadlines honoured (`EXPIRED`), and executed invocation ids remembered for
    ten minutes so a replay returns the stored result instead of acting twice;
  * a kill switch that stops remote invocation until cleared.

Capabilities: `test.echo` (tier 1) and `test.secret` (tier 2, confirms first).

Manual use against a running core with `hub.enabled: true`:

    python -m nora.hub pair                       # on the core: prints a code
    python -m nora.hub.fake_device pair ABCD-EFGH
    python -m nora.hub approve <device_id>
    python -m nora.hub.fake_device say "what's the time"
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from nora.hub import protocol

logger = logging.getLogger("nora.hub.fake_device")

_REPLAY_WINDOW_SEC = 600

CAPABILITIES: list[dict] = [
    {
        "name": "test.echo", "version": 1, "tier": 1,
        "description": "Echo text back from the test device",
        "params_schema": {"type": "object", "required": ["text"],
                          "properties": {"text": {"type": "string", "maxLength": 200}}},
    },
    {
        "name": "test.secret", "version": 1, "tier": 2,
        "description": "Reveal the test device's secret word",
        "params_schema": {"type": "object", "properties": {}},
    },
]


class FakeDevice:
    def __init__(self, url: str, *, name: str = "fake", key: ec.EllipticCurvePrivateKey | None = None,
                 device_id: str | None = None,
                 approve: Callable[[dict], bool] = lambda _req: True,
                 capabilities: list[dict] | None = None,
                 replies: dict[str, Callable[[dict], dict]] | None = None) -> None:
        self.url = url
        self.name = name
        self.key = key or ec.generate_private_key(ec.SECP256R1())
        self.device_id = device_id
        self.approve = approve
        self.capabilities = capabilities if capabilities is not None else CAPABILITIES
        self.tiers = {c["name"]: c["tier"] for c in self.capabilities}
        self.live_only = {c["name"] for c in self.capabilities if c.get("requires_live_user")}
        # capability -> params -> reply body ({"success": …, "result"/"error": …}),
        # for tests that need a capability to answer like the phone's would.
        self.replies = replies or {}
        self.killed = False

        self.ws = None
        self.session_id: str | None = None
        self.said: list[str] = []
        self.says: list[dict] = []              # full `say` bodies, with their corr
        self.audio: dict[int, bytearray] = {}   # stream -> PCM received
        self.audio_end: dict[int, dict] = {}    # stream -> audio.end body
        self.notified: list[dict] = []
        self.confirm_requests: list[dict] = []
        self.executed: list[dict] = []          # invocations actually run
        self._approved: dict[str, list] = {}    # confirmation id -> the steps approved
        self._done: dict[str, tuple[float, dict]] = {}   # invocation id -> (ts, reply body)
        self._turns: dict[str, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None

    # ── identity ─────────────────────────────────────────────────────────────
    @property
    def public_key_b64(self) -> str:
        return base64.b64encode(protocol.public_key_der(self.key.public_key())).decode()

    def key_pem(self) -> bytes:
        return self.key.private_bytes(serialization.Encoding.PEM,
                                      serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption())

    # ── wire ─────────────────────────────────────────────────────────────────
    async def _send(self, type_: str, body: dict | None = None, *, corr: str | None = None) -> str:
        msg = protocol.envelope(type_, body, corr=corr)
        await self.ws.send(protocol.encode(msg))
        return msg["id"]

    async def _recv(self) -> dict:
        return protocol.decode(await asyncio.wait_for(self.ws.recv(), 10))

    async def pair(self, code: str, platform: str = "test") -> dict:
        """Spend a pairing code. Returns the core's reply body (or error body)."""
        import websockets

        async with websockets.connect(self.url, max_size=protocol.MAX_FRAME) as ws:
            self.ws = ws
            await self._send("pair", {"pairing_code": code, "device_name": self.name,
                                      "platform": platform, "public_key": self.public_key_b64})
            reply = await self._recv()
        self.ws = None
        if reply["type"] == "paired":
            self.device_id = reply["body"]["device_id"]
        return {"type": reply["type"], **reply["body"]}

    async def connect(self) -> dict:
        """hello → challenge → auth → welcome → manifest. Returns the welcome
        body, or the error body if the core refused."""
        import websockets

        self.ws = await websockets.connect(self.url, max_size=protocol.MAX_FRAME)
        await self._send("hello", {"device_id": self.device_id, "app_version": "fake-1",
                                   "protocols": [protocol.VERSION], "platform": "test"})
        challenge = await self._recv()
        if challenge["type"] != "challenge":
            await self.ws.close()
            return {"type": challenge["type"], **challenge["body"]}
        nonce = bytes.fromhex(challenge["body"]["nonce"])
        await self._send("auth", {"signature": protocol.sign(
            self.key, protocol.auth_payload(nonce, self.device_id))}, corr=challenge["id"])
        welcome = await self._recv()
        if welcome["type"] != "welcome":
            await self.ws.close()
            return {"type": welcome["type"], **welcome["body"]}
        self.session_id = welcome["body"]["session_id"]
        await self._send("manifest", {"capabilities": self.capabilities})
        self._reader = asyncio.create_task(self._read_loop())
        return {"type": "welcome", **welcome["body"]}

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
        if self.ws is not None:
            await self.ws.close()

    # ── user actions ─────────────────────────────────────────────────────────
    async def say(self, text: str, timeout: float = 30.0, *, voice: dict | None = None,
                  on_sent: Callable[[str], Any] | None = None) -> list[str]:
        """Type (or, with `voice`, speak) something to NORA; return what she
        said back during that turn."""
        body: dict[str, Any] = {"text": text}
        if voice is not None:
            body["voice"] = voice
        msg_id = await self._send("utterance", body)
        if on_sent is not None:
            on_sent(msg_id)
        fut = asyncio.get_running_loop().create_future()
        self._turns[msg_id] = fut
        start = len(self.said)
        try:
            await asyncio.wait_for(fut, timeout)
        finally:
            self._turns.pop(msg_id, None)
        return self.said[start:]

    async def barge_in(self, utterance_id: str) -> None:
        await self._send("voice.barge_in", {}, corr=utterance_id)

    async def set_kill(self, active: bool) -> None:
        self.killed = active
        await self._send("kill", {"active": active})

    # ── inbound ──────────────────────────────────────────────────────────────
    async def _read_loop(self) -> None:
        try:
            async for frame in self.ws:
                if isinstance(frame, bytes):
                    from nora.hub import voice
                    stream, pcm = voice.parse_frame(frame)
                    self.audio.setdefault(stream, bytearray()).extend(pcm)
                    continue
                msg = protocol.decode(frame)
                kind, body = msg["type"], msg["body"]
                if kind == "say":
                    self.said.append(body.get("text", ""))
                    self.says.append({**body, "corr": msg.get("corr")})
                elif kind == "audio.end":
                    self.audio_end[body.get("stream", 0)] = body
                elif kind == "turn.done":
                    fut = self._turns.get(msg.get("corr") or "")
                    if fut is not None and not fut.done():
                        fut.set_result(body)
                elif kind == "notify":
                    self.notified.append(body)
                elif kind == "confirm_request":
                    await self._on_confirm(msg)
                elif kind == "invoke":
                    await self._send("result", self._on_invoke(msg), corr=msg["id"])
                elif kind == "ping":
                    await self._send("pong", corr=msg["id"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not type(e).__name__.startswith("ConnectionClosed"):
                logger.exception("fake device read loop: %s", e)

    async def _on_confirm(self, msg: dict) -> None:
        body = msg["body"]
        self.confirm_requests.append(body)
        # A real device renders the prompt from the steps, not from `rendered`.
        approved = bool(self.approve(body)) and not self.killed
        if approved:
            self._approved[body.get("invocation_id", "")] = body.get("steps") or []
        await self._send("confirm_response", {"invocation_id": body.get("invocation_id"),
                                              "approved": approved, "method": "auto"},
                         corr=msg["id"])

    def _on_invoke(self, msg: dict) -> dict:
        inv_id, body = msg["id"], msg["body"]
        now = time.time()
        self._done = {k: v for k, v in self._done.items() if now - v[0] < _REPLAY_WINDOW_SEC}
        if inv_id in self._done:
            return self._done[inv_id][1]

        def err(code: str, message: str) -> dict:
            return {"success": False, "device": self.device_id, "action": body.get("capability"),
                    "error": {"code": code, "message": message, "retryable": False}}

        cap = body.get("capability")
        if self.killed:
            reply = err(protocol.POLICY_BLOCKED, "Remote control is off on this device")
        elif cap not in self.tiers:
            reply = err(protocol.CAPABILITY_UNAVAILABLE, f"{cap} not on this device")
        elif now * 1000 - msg["ts"] > body.get("deadline_ms", 0):
            reply = err(protocol.EXPIRED, "invocation arrived past its deadline")
        elif cap in self.live_only and body.get("origin") != "live_user":
            reply = err(protocol.POLICY_BLOCKED, f"{cap} needs a live user")
        elif self.tiers[cap] >= protocol.TIER_CONFIRM and not self._was_approved(body):
            # The device's own tier table, enforced whatever the core claims.
            reply = err(protocol.POLICY_BLOCKED, f"{cap} was not confirmed on this device")
        else:
            reply = self._execute(cap, body.get("params") or {})
            self.executed.append({"id": inv_id, **body})
        self._done[inv_id] = (now, reply)
        return reply

    def _was_approved(self, body: dict) -> bool:
        """Only if this device approved *this* step — capability and params —
        under the confirmation id the invocation carries."""
        steps = self._approved.get((body.get("confirmation") or {}).get("id") or "")
        step = {"action": body.get("capability"), "params": body.get("params") or {}}
        return steps is not None and step in steps

    def _execute(self, cap: str, params: dict[str, Any]) -> dict:
        if cap in self.replies:
            return {"device": self.device_id, "action": cap, "duration_ms": 1,
                    **self.replies[cap](params)}
        if cap == "test.echo":
            result = {"message": f"echo: {params.get('text', '')}"}
        elif cap == "test.secret":
            result = {"message": "The secret word is marmalade."}
        else:
            result = {"message": "done"}
        return {"success": True, "device": self.device_id, "action": cap,
                "result": result, "duration_ms": 1}


# ── CLI ──────────────────────────────────────────────────────────────────────

_STATE = Path.home() / ".config" / "nora-fake-device" / "identity.json"


def _load_identity() -> tuple[ec.EllipticCurvePrivateKey, str | None]:
    data = json.loads(_STATE.read_text())
    key = serialization.load_pem_private_key(data["key"].encode(), password=None)
    return key, data.get("device_id")


def _save_identity(dev: FakeDevice) -> None:
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    _STATE.write_text(json.dumps({"key": dev.key_pem().decode(), "device_id": dev.device_id}))
    _STATE.chmod(0o600)


def main() -> None:
    ap = argparse.ArgumentParser(description="Protocol v1 fake device")
    ap.add_argument("--url", default="ws://127.0.0.1:8770" + protocol.PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pair"); p.add_argument("code")
    s = sub.add_parser("say"); s.add_argument("text")
    args = ap.parse_args()

    async def run() -> None:
        if args.cmd == "pair":
            dev = FakeDevice(args.url)
            print(await dev.pair(args.code))
            if dev.device_id:
                _save_identity(dev)
            return
        key, device_id = _load_identity()
        dev = FakeDevice(args.url, key=key, device_id=device_id)
        welcome = await dev.connect()
        if welcome["type"] != "welcome":
            print(welcome)
            return
        for line in await dev.say(args.text):
            print("NORA:", line)
        await dev.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()

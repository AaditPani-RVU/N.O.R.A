"""The Device Hub — where phones and laptops connect, authenticate, and act.

One WebSocket endpoint, `/v1/device`, on its own thread and event loop, bound
to 127.0.0.1. Other devices reach it through `tailscale serve`, which adds a
real `*.ts.net` TLS certificate and keeps it off every LAN the laptop joins;
the hub authenticates every device itself regardless (plan §7.1–7.2).

A connection is one of two things:

  pair   → the device spends a pairing code and registers its public key,
           then disconnects. It is *pending* until approved on the core.
  hello  → challenge → auth (signature) → welcome → manifest, and then a live
           session: the device's capabilities become commands, jobs it asked
           for are delivered to it, and it can send typed turns (`utterance`).

The hub talks to the rest of NORA through three narrow seams and nothing else:
`command_engine.register_device_capability` (what the LLM can call),
`delivery.register` (where unprompted answers go), and `pipeline.handle_turn`
with a device `Channel` (typed requests and their confirmations).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from nora import channel as _channel, command_engine, delivery, store
from nora.config import get_config
from nora.hub import protocol, registry
from nora.hub import voice as _voice
from nora.schemas import StepResult

logger = logging.getLogger("nora.hub")

HANDSHAKE_TIMEOUT = 10.0
PING_INTERVAL = 25.0
_TIER_RISK = {0: "low", 1: "low", 2: "medium", 3: "high"}


def _cfg() -> dict:
    return get_config().get("hub", {}) or {}


def _speech_hints() -> list[str]:
    """Names for the phone's recogniser to listen for (Sharp D): the artists
    the user plays most, which it otherwise hears as "deaf tools"."""
    try:
        from nora import music_names
        return music_names.biasing()
    except Exception as exc:                     # never worth a refused session
        logger.debug("no speech hints: %s", exc)
        return []


@dataclass
class Session:
    device: registry.Device
    ws: Any
    loop: asyncio.AbstractEventLoop
    session_id: str = field(default_factory=protocol.new_id)
    capabilities: dict[str, dict] = field(default_factory=dict)
    killed: bool = False
    _pending: dict[str, asyncio.Future] = field(default_factory=dict)
    _outbox: asyncio.Queue = field(default_factory=asyncio.Queue)
    _seq: int = 0
    _turns: set = field(default_factory=set)
    frustration: Any = None
    _stream: int = 0
    # utterance id -> the VoiceOut speaking its answer (voice turns only)
    voices: dict[str, Any] = field(default_factory=dict)

    @property
    def device_id(self) -> str:
        return self.device.id

    # ── outbound ─────────────────────────────────────────────────────────────
    def send_nowait(self, type_: str, body: dict | None = None, *,
                    corr: str | None = None, id_: str | None = None) -> str:
        """Queue a frame from any thread. Frames leave in the order queued."""
        msg = protocol.envelope(type_, body, corr=corr, id_=id_)
        self.loop.call_soon_threadsafe(self._outbox.put_nowait, msg)
        return msg["id"]

    def send_bytes_nowait(self, frame: bytes) -> None:
        """Queue a binary frame (voice audio) behind whatever is already queued."""
        self.loop.call_soon_threadsafe(self._outbox.put_nowait, frame)

    def next_stream(self) -> int:
        self._stream += 1
        return self._stream

    async def writer(self) -> None:
        # The only place frames are written, so `seq` is assigned here: one
        # task, strictly increasing, whichever thread queued the frame.
        while True:
            msg = await self._outbox.get()
            if isinstance(msg, bytes):
                await self.ws.send(msg)
                continue
            self._seq += 1
            msg["seq"] = self._seq
            await self.ws.send(protocol.encode(msg))

    async def request(self, type_: str, body: dict, timeout: float) -> dict:
        """Send a frame and wait for the device's frame answering it (`corr`)."""
        msg_id = protocol.new_id()
        fut = self.loop.create_future()
        self._pending[msg_id] = fut
        try:
            self.send_nowait(type_, body, id_=msg_id)
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(msg_id, None)

    def resolve(self, msg: dict) -> None:
        fut = self._pending.get(msg.get("corr") or "")
        if fut is not None and not fut.done():
            fut.set_result(msg["body"])

    def fail_pending(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("device disconnected"))


class Hub:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self.port: int | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self, host: str = "127.0.0.1", port: int = 8770) -> int:
        """Start serving on a background thread. Returns the bound port (0 → any)."""
        if self._thread is not None:
            return self.port or port

        def _run() -> None:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._serve(host, port))
            self.loop.run_forever()

        self._thread = threading.Thread(target=_run, daemon=True, name="nora-hub")
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("hub did not start")
        return self.port

    async def _serve(self, host: str, port: int) -> None:
        import websockets

        self._server = await websockets.serve(
            self._handle, host, port, max_size=protocol.MAX_FRAME,
            ping_interval=PING_INTERVAL, ping_timeout=PING_INTERVAL,
            process_request=self._check_path)
        self.port = self._server.sockets[0].getsockname()[1]
        self._ready.set()
        logger.info("Device hub listening on %s:%d%s", host, self.port, protocol.PATH)

    def stop(self) -> None:
        if self.loop is None:
            return

        async def _close() -> None:
            for s in list(self._sessions.values()):
                await s.ws.close(1001, "core shutting down")
            self._server.close()
            await self._server.wait_closed()

        try:
            asyncio.run_coroutine_threadsafe(_close(), self.loop).result(5)
        except Exception as e:
            logger.debug("hub close: %s", e)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)
        self._thread = None
        self.loop = None
        self._ready.clear()

    @staticmethod
    def _check_path(connection, request):
        if request.path != protocol.PATH:
            return connection.respond(404, "Not found\n")
        return None

    def sessions(self) -> list[str]:
        with self._lock:
            return sorted(self._sessions)

    # ── connection ───────────────────────────────────────────────────────────
    async def _handle(self, ws) -> None:
        try:
            first = protocol.decode(await asyncio.wait_for(ws.recv(), HANDSHAKE_TIMEOUT))
            if first["type"] == "pair":
                await self._pair(ws, first)
            elif first["type"] == "hello":
                session = await self._handshake(ws, first)
                if session is not None:
                    await self._run_session(session)
            else:
                await self._refuse(ws, first, "expected pair or hello")
        except (asyncio.TimeoutError, protocol.ProtocolError) as e:
            logger.info("Hub connection dropped during setup: %s", e)
            await ws.close(1008, "protocol error")
        except Exception as e:
            if type(e).__name__.startswith("ConnectionClosed"):
                return
            logger.exception("Hub connection failed: %s", e)

    async def _refuse(self, ws, msg: dict, reason: str) -> None:
        await ws.send(protocol.encode(protocol.envelope(
            "error", {"code": "AUTH_FAILED", "message": reason}, corr=msg.get("id"))))
        await ws.close(1008, reason[:100])

    async def _pair(self, ws, msg: dict) -> None:
        body = msg["body"]
        try:
            device = registry.redeem(
                str(body.get("pairing_code", "")),
                name=str(body.get("device_name", "")),
                platform=str(body.get("platform", "")),
                public_key_b64=str(body.get("public_key", "")))
        except protocol.ProtocolError as e:
            await self._refuse(ws, msg, str(e))
            return
        if device is None:
            await self._refuse(ws, msg, "pairing code invalid or expired")
            return
        logger.info("Paired new device %s (%s) — pending approval", device.id, device.name)
        await ws.send(protocol.encode(protocol.envelope(
            "paired", {"device_id": device.id, "status": device.status}, corr=msg["id"])))
        await ws.close(1000, "paired")

    async def _handshake(self, ws, hello: dict) -> Session | None:
        device_id = str(hello["body"].get("device_id", ""))
        if protocol.VERSION not in (hello["body"].get("protocols") or []):
            await self._refuse(ws, hello, "no common protocol version")
            return None
        device = registry.get(device_id)
        # Same answer for unknown, pending and revoked: the socket is not the
        # place to tell a stranger which device ids exist.
        if device is None or not device.usable:
            await self._refuse(ws, hello, "device not authorised")
            return None

        nonce = os.urandom(32)
        challenge = protocol.envelope("challenge", {"nonce": nonce.hex()}, corr=hello["id"])
        await ws.send(protocol.encode(challenge))
        auth = protocol.decode(await asyncio.wait_for(ws.recv(), HANDSHAKE_TIMEOUT))
        signature = auth["body"].get("signature", "")
        if (auth["type"] != "auth" or not isinstance(signature, str)
                or not protocol.verify(device.public_key, signature,
                                       protocol.auth_payload(nonce, device.id))):
            await self._refuse(ws, auth, "signature did not verify")
            return None

        session = Session(device=device, ws=ws, loop=asyncio.get_running_loop())
        await ws.send(protocol.encode(protocol.envelope("welcome", {
            "session_id": session.session_id, "protocol": protocol.VERSION,
            "core_version": "nora-phase2", "resume_from_seq": 0,
            "speech_hints": _speech_hints(),
        }, corr=auth["id"])))

        manifest = protocol.decode(await asyncio.wait_for(ws.recv(), HANDSHAKE_TIMEOUT))
        if manifest["type"] != "manifest":
            await self._refuse(ws, manifest, "expected manifest")
            return None
        self._apply_manifest(session, manifest["body"].get("capabilities") or [])
        return session

    def _apply_manifest(self, session: Session, entries: list) -> None:
        caps: dict[str, dict] = {}
        for raw in entries[:100]:
            try:
                entry = protocol.check_manifest_entry(raw)
            except protocol.ProtocolError as e:
                logger.warning("%s: capability rejected: %s", session.device_id, e)
                continue
            if entry["tier"] >= protocol.TIER_FORBIDDEN or not entry["available"]:
                continue
            caps[entry["name"]] = entry
        session.capabilities = caps

    def _register(self, session: Session) -> None:
        for name, entry in session.capabilities.items():
            tier = self._effective_tier(name, entry["tier"], _channel.ORIGIN_LIVE_USER)
            desc = entry["description"]
            if tier >= protocol.TIER_CONFIRM:
                desc = f"{desc} (asks on the device first)".strip()
            command_engine.register_device_capability(
                name, self._proxy(session.device_id, name), device=session.device_id,
                sig=protocol.signature_hint(entry), description=desc,
                risk=_TIER_RISK.get(tier, "high"), tier=tier)
        from nora import tool_retrieval
        tool_retrieval.warm_soon()

    def _sink(self, session: Session):
        def send(text: str, kind: str) -> bool:
            if session.killed:
                return False
            title = "Reminder" if kind == "reminder" else "NORA"
            session.send_nowait("notify", {"title": title, "body": text,
                                           "priority": "default", "kind": kind})
            return True
        return send

    async def _run_session(self, session: Session) -> None:
        with self._lock:
            old = self._sessions.get(session.device_id)
            self._sessions[session.device_id] = session
        if old is not None:
            # A reconnect while the old socket lingers: the newest wins.
            await old.ws.close(1000, "replaced by a newer connection")
        self._register(session)
        sink = self._sink(session)
        delivery.register(session.device_id, sink)
        registry.touch(session.device_id)
        logger.info("Device %s (%s) connected with %d capabilities",
                    session.device_id, session.device.name, len(session.capabilities))

        from nora import jobs
        await asyncio.get_running_loop().run_in_executor(None, jobs.flush, session.device_id)
        # Load Kokoro now (~1.5 s once), not on this device's first spoken turn.
        asyncio.get_running_loop().run_in_executor(None, _voice.core_voice_available)

        writer = asyncio.create_task(session.writer())
        watchdog = asyncio.create_task(self._watch_revocation(session))
        try:
            async for frame in session.ws:
                try:
                    msg = protocol.decode(frame)
                except protocol.ProtocolError as e:
                    logger.warning("%s sent a bad frame: %s", session.device_id, e)
                    await session.ws.close(1008, "protocol error")
                    break
                self._dispatch(session, msg)
        finally:
            writer.cancel()
            watchdog.cancel()
            session.fail_pending()
            for out in list(session.voices.values()):
                out.cancel()
            with self._lock:
                current = self._sessions.get(session.device_id) is session
                if current:
                    self._sessions.pop(session.device_id, None)
            if current:
                command_engine.unregister_device(session.device_id)
            delivery.unregister(session.device_id, sink)
            registry.touch(session.device_id)
            logger.info("Device %s disconnected", session.device_id)

    async def _watch_revocation(self, session: Session) -> None:
        """Revocation from the CLI lands in the store; drop the socket when it does."""
        while True:
            await asyncio.sleep(2.0)
            device = registry.get(session.device_id)
            if device is None or not device.usable:
                logger.info("Device %s revoked — disconnecting", session.device_id)
                await session.ws.close(1008, "revoked")
                return

    def _dispatch(self, session: Session, msg: dict) -> None:
        kind = msg["type"]
        if kind not in protocol.DEVICE_TYPES:
            logger.info("%s sent unknown type %s", session.device_id, kind)
            return
        if kind in ("result", "confirm_response", "pong"):
            session.resolve(msg)
        elif kind == "ping":
            session.send_nowait("pong", corr=msg["id"])
        elif kind == "kill":
            session.killed = bool(msg["body"].get("active", True))
            logger.warning("Device %s kill switch %s", session.device_id,
                           "ON" if session.killed else "off")
            if session.killed:
                command_engine.unregister_device(session.device_id)
            else:
                self._register(session)
        elif kind == "event":
            # Events feed the trigger engine in Phase 8; for now, ack and log.
            session.send_nowait("ack", {"seq": msg.get("seq", 0)}, corr=msg["id"])
            name = msg["body"].get("name")
            if name == "voice.turn" and isinstance(msg["body"].get("data"), dict):
                _voice.record_turn(session.device_id, msg["body"]["data"])
            else:
                logger.info("%s event %s", session.device_id, name)
        elif kind == "voice.barge_in":
            out = session.voices.get(msg.get("corr") or "")
            if out is not None:
                out.cancel()
                logger.info("%s barged in; stopped speaking", session.device_id)
        elif kind == "utterance":
            text = str(msg["body"].get("text", ""))[:2000]
            voice = msg["body"].get("voice")
            core_tts = isinstance(voice, dict) and voice.get("tts") == "core"
            test = msg["body"].get("test") is True          # nora.dev: run, don't remember
            task = asyncio.create_task(self._turn(session, text, msg["id"], core_tts=core_tts,
                                                  test=test))
            session._turns.add(task)
            task.add_done_callback(session._turns.discard)
        elif kind == "manifest":
            command_engine.unregister_device(session.device_id)
            self._apply_manifest(session, msg["body"].get("capabilities") or [])
            if not session.killed:
                self._register(session)

    # ── turns from the device ────────────────────────────────────────────────
    async def _turn(self, session: Session, text: str, corr: str, *, core_tts: bool = False,
                    test: bool = False) -> None:
        from nora import dialogue, pipeline, wiring
        from nora.frustration import FrustrationTracker

        out = None
        if core_tts:
            ready = await asyncio.get_running_loop().run_in_executor(None, _voice.core_voice_available)
            if ready:
                out = _voice.VoiceOut(session)
                session.voices[corr] = out

        def speak(line: str, *_a, mood: str | None = None, **_kw) -> None:
            if line:
                body = {"text": line, "mood": mood or "", "turn_id": ch.turn_id}
                audio = out.line(line, mood) if out is not None else None
                if audio is not None:
                    body["audio"] = audio
                session.send_nowait("say", body, corr=corr)
                # The laptop's speaker keeps what NORA said in the transcript;
                # a device's lines must too. Without them the chat model saw
                # only the user's side ("what time is it" x4, then "that's
                # crazy") and answered every remark with the time.
                dialogue.record_nora(line, kind=mood or "info")

        async def confirm(req: _channel.ConfirmRequest) -> bool:
            return await self._ask(session, req.turn_id, req.rendered,
                                   [{"action": s.action, "params": s.parameters}
                                    for s in req.steps], tier=protocol.TIER_CONFIRM,
                                   expires_in=req.expires_in)

        ch = _channel.Channel(device_id=session.device_id, kind="device", speak=speak,
                              confirm=confirm, session_id=session.session_id, test=test)
        if session.frustration is None:
            session.frustration = FrustrationTracker()
        deps = wiring.build(listener=None, frustration=session.frustration, speak=speak)
        delivery.note_active(session.device_id)
        try:
            outcome = await pipeline.handle_turn(text, deps, channel=ch)
            kind = outcome.kind
        except Exception as e:
            logger.exception("Device turn failed: %s", e)
            speak("Something went wrong on my side.")
            kind = "error"
        if out is not None:
            # Synthesis usually runs past the turn; barge-in must still reach it.
            out.close(on_done=lambda: session.voices.pop(corr, None))
        session.send_nowait("turn.done", {"turn_id": ch.turn_id, "outcome": kind}, corr=corr)

    async def _ask(self, session: Session, ref: str, rendered: str, steps: list,
                   *, tier: int, expires_in: float = 60.0) -> bool:
        """Ask the device's user. False on decline, expiry or disconnect."""
        try:
            body = await session.request("confirm_request", {
                "invocation_id": ref, "rendered": rendered, "steps": steps,
                "tier": tier, "expires_in": expires_in,
            }, timeout=expires_in)
        except (asyncio.TimeoutError, ConnectionError):
            return False
        return body.get("approved") is True

    # ── invoking capabilities ────────────────────────────────────────────────
    def _effective_tier(self, name: str, declared: int, origin: str) -> int:
        """max(core floor, device tier), plus one for anything not asked live."""
        floor = int((_cfg().get("capability_tiers") or {}).get(name, 0))
        tier = max(floor, declared)
        if origin != _channel.ORIGIN_LIVE_USER:
            tier += 1
        return tier

    @staticmethod
    def _untrusted(name: str, entry: dict) -> bool:
        """The device's word, or the core's list: either can mark a
        capability's output untrusted, neither can clear the other's mark."""
        listed = _cfg().get("untrusted_capabilities") or []
        return bool(entry.get("untrusted_output")) or name in listed

    def _proxy(self, device_id: str, capability: str):
        hub = self

        async def invoke_capability(**params) -> StepResult:
            # Read the channel here, in the turn's context: `call` runs the
            # invocation on the hub's loop, which has a context of its own.
            return await hub.call(device_id, capability, params, channel=_channel.current())

        invoke_capability.__name__ = capability.replace(".", "_")
        return invoke_capability

    async def call(self, device_id: str, capability: str, params: dict, *,
                   channel: _channel.Channel | None = None) -> StepResult:
        """`invoke` from any thread or loop. Sessions and their futures belong
        to the hub's loop, so the work always runs there."""
        fut = asyncio.run_coroutine_threadsafe(
            self.invoke(device_id, capability, params, channel=channel), self.loop)
        return await asyncio.wrap_future(fut)

    async def invoke(self, device_id: str, capability: str, params: dict, *,
                     channel: _channel.Channel | None = None) -> StepResult:
        """Run one capability on one device. Always returns a StepResult.
        Must run on the hub's loop; use `call` from anywhere else.

        `channel` is the turn that asked; none means a job or schedule, which
        is `origin: job` and so one tier stricter (plan §7.4).
        """
        origin = channel.origin if channel else _channel.ORIGIN_JOB
        turn_id = channel.turn_id if channel else ""

        def fail(code: str, message: str) -> StepResult:
            # A refusal is policy, not the capability misbehaving: withheld,
            # so the trust ledger does not count it against the tool. The
            # phone refusing to act from the background is the OS's policy,
            # and the device has already said what it did instead.
            refused = code in (protocol.POLICY_BLOCKED, protocol.USER_DECLINED,
                               protocol.BACKGROUND_RESTRICTED)
            return StepResult(action=capability, success=False, message=message,
                              error_code=code, withheld=refused)

        with self._lock:
            session = self._sessions.get(device_id)
        if session is None:
            return fail(protocol.DEVICE_OFFLINE, "That device isn't connected right now.")
        if session.killed:
            return fail(protocol.POLICY_BLOCKED, "Remote control is switched off on that device.")
        device = registry.get(device_id)
        if device is None or not device.usable:
            return fail(protocol.POLICY_BLOCKED, "That device is no longer authorised.")
        entry = session.capabilities.get(capability)
        if entry is None:
            return fail(protocol.CAPABILITY_UNAVAILABLE, f"{capability} isn't available.")
        if entry["requires_live_user"] and origin != _channel.ORIGIN_LIVE_USER:
            return fail(protocol.POLICY_BLOCKED,
                        f"{capability} only runs when you ask for it directly.")
        params = protocol.fit_params(entry["params_schema"], params)
        problem = protocol.validate_params(entry["params_schema"], params)
        if problem:
            return fail(protocol.INVALID_PARAMS, f"Bad parameters for {capability}: {problem}")

        tier = self._effective_tier(capability, entry["tier"], origin)
        if tier >= protocol.TIER_FORBIDDEN:
            return fail(protocol.POLICY_BLOCKED, f"{capability} can't run from {origin}.")

        inv_id = protocol.new_id()
        deadline_ms = int((_cfg().get("invoke_deadlines_ms") or {}).get(
            capability, _cfg().get("invoke_deadline_ms", 8000)))
        self._record(inv_id, device_id, capability, params, origin, turn_id, tier)

        confirmation = None
        if (tier >= protocol.TIER_CONFIRM and channel is not None
                and channel.confirmed_by == device_id):
            # This device's user already approved this turn's steps, this one
            # included; asking again on the same screen is noise. The device
            # still checks the step against what it approved under that id.
            confirmation = {"id": channel.turn_id, "approved_at": int(time.time() * 1000)}
            self._confirmed(inv_id, device_id)
        elif tier >= protocol.TIER_CONFIRM:
            rendered = f"{capability} {json.dumps(params, ensure_ascii=False)}"
            approved = await self._ask(session, inv_id, rendered,
                                       [{"action": capability, "params": params}], tier=tier)
            if not approved:
                self._finish(inv_id, "error", None, protocol.USER_DECLINED)
                return fail(protocol.USER_DECLINED, "Not confirmed on the device, so I left it.")
            confirmation = {"id": inv_id, "approved_at": int(time.time() * 1000)}
            self._confirmed(inv_id, device_id)
            if channel is not None and not channel.confirmed_by:
                channel.confirmed_by = device_id

        body = {"capability": capability, "version": entry["version"], "params": params,
                "deadline_ms": deadline_ms, "origin": origin, "turn_id": turn_id,
                "confirmation": confirmation, "tier": tier}
        try:
            reply = await self._send_invoke(session, inv_id, body, deadline_ms / 1000 + 2)
        except asyncio.TimeoutError:
            self._finish(inv_id, "error", None, protocol.TIMEOUT)
            return fail(protocol.TIMEOUT, f"{capability} timed out on the device.")
        except ConnectionError:
            self._finish(inv_id, "error", None, protocol.DEVICE_OFFLINE)
            return fail(protocol.DEVICE_OFFLINE, "The device disconnected mid-way.")

        if reply.get("success") is True:
            result = reply.get("result") or {}
            untrusted = self._untrusted(capability, entry)
            # Third-party text (notifications) is answered from, not kept:
            # the invocation row records that it ran and how much came back.
            self._finish(inv_id, "ok", _redacted(result) if untrusted else result, None)
            message = result.get("message") if isinstance(result, dict) else None
            limit = 4000 if untrusted else 500
            return StepResult(action=capability, success=True, untrusted=untrusted,
                              message=str(message or json.dumps(result, ensure_ascii=False))[:limit],
                              data=result if isinstance(result, dict) else {})
        err = reply.get("error") or {}
        code = str(err.get("code") or protocol.EXECUTION_FAILED)
        self._finish(inv_id, "error", None, code)
        return fail(code, str(err.get("message") or f"{capability} failed on the device.")[:300])

    async def _send_invoke(self, session: Session, inv_id: str, body: dict,
                           timeout: float) -> dict:
        """An invoke's frame id *is* the invocation id, so a replay after
        reconnect carries the same id and the device can deduplicate it."""
        fut = session.loop.create_future()
        session._pending[inv_id] = fut
        try:
            session.send_nowait("invoke", body, id_=inv_id)
            return await asyncio.wait_for(fut, timeout)
        finally:
            session._pending.pop(inv_id, None)

    # ── invocation log ───────────────────────────────────────────────────────
    @staticmethod
    def _record(inv_id, device_id, capability, params, origin, turn_id, tier) -> None:
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO invocations (id, device_id, capability, params, origin,"
                " turn_id, tier, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (inv_id, device_id, capability, json.dumps(params), origin, turn_id,
                 tier, "sent", time.time()))

    @staticmethod
    def _confirmed(inv_id: str, device_id: str) -> None:
        with store.transaction() as conn:
            conn.execute("UPDATE invocations SET confirmed_by = ? WHERE id = ?",
                         (device_id, inv_id))

    @staticmethod
    def _finish(inv_id: str, status: str, result, error_code: str | None) -> None:
        with store.transaction() as conn:
            conn.execute(
                "UPDATE invocations SET status = ?, result = ?, error_code = ?,"
                " finished_at = ? WHERE id = ?",
                (status, json.dumps(result) if result is not None else None,
                 error_code, time.time(), inv_id))


def _redacted(result) -> dict:
    count = None
    if isinstance(result, dict):
        items = result.get("items")
        count = len(items) if isinstance(items, list) else result.get("count")
    return {"redacted": True, "count": count}


_hub: Hub | None = None


def get() -> Hub | None:
    return _hub


async def call_capability(name: str, params: dict | None = None) -> StepResult:
    """Run `name` on whichever connected device offers it, on behalf of the
    turn in progress — for core commands that need the phone for one part of
    a job (where it is, before routing there). Same policy path as the LLM
    calling it directly: tiers, origin, validation, the invocation log.
    """
    meta = command_engine.get_action_meta(name)
    if _hub is None or meta is None or not meta.device:
        return StepResult(action=name, success=False, error_code=protocol.DEVICE_OFFLINE,
                          message="Your phone isn't connected right now.")
    return await _hub.call(meta.device, name, params or {}, channel=_channel.current())


def start() -> int | None:
    """Start the hub if `hub.enabled`. Returns the port, or None when off."""
    global _hub
    cfg = _cfg()
    if not cfg.get("enabled", False):
        return None
    host = str(cfg.get("host", "127.0.0.1"))
    if host not in ("127.0.0.1", "::1", "localhost"):
        # Plan §7.2: the hub is published through `tailscale serve`, never by
        # binding a routable address. Refuse rather than quietly listen on a LAN.
        logger.error("hub.host must be a loopback address (got %s); hub not started", host)
        return None
    _hub = Hub()
    return _hub.start(host, int(cfg.get("port", 8770)))


def stop() -> None:
    global _hub
    if _hub is not None:
        _hub.stop()
        _hub = None

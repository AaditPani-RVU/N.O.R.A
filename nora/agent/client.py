"""The agent's connection: the reference protocol client with real capabilities.

`FakeDevice` is the protocol v1 client the tests drive the hub with; it
already pairs, signs the challenge, enforces its own tier table, honours
deadlines and replays, and answers confirmations. The agent is that client
with the laptop's capabilities behind it, parameters checked against each
capability's schema on this side too (plan §7.6), and a connection that
comes back on its own with backoff and jitter (plan §5).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from pathlib import Path
from typing import Any, Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from nora.agent import capabilities as caps
from nora.hub import protocol
from nora.hub.fake_device import FakeDevice

logger = logging.getLogger("nora.agent")

VERSION = "agent-1"
CONFIG_DIR = Path.home() / ".config" / "nora-agent"


def default_shared() -> Path:
    return Path.home() / "NORA Shared"


class Agent(FakeDevice):
    def __init__(self, url: str, *, name: str = "travel laptop", key=None, device_id: str | None = None,
                 shared_dir: Path | None = None,
                 approve: Callable[[dict], Any] = lambda _req: False,
                 notify: Callable[[str, str], None] | None = None) -> None:
        self.shared_dir = shared_dir or default_shared()
        self._caps = {c.name: c for c in caps.build(self.shared_dir)}
        super().__init__(url, name=name, key=key, device_id=device_id, approve=approve,
                         capabilities=[c.entry for c in self._caps.values()])
        self.platform = "laptop"
        self.app_version = VERSION
        self._notify = notify or _desktop_notify

    # ── what the core sends ─────────────────────────────────────────────────
    def on_notify(self, body: dict) -> None:
        try:
            self._notify(str(body.get("title") or "NORA"), str(body.get("body") or ""))
        except Exception as e:
            logger.info("notification not shown: %s", e)

    def _execute(self, cap: str, params: dict[str, Any]) -> dict:
        c = self._caps[cap]
        base = {"device": self.device_id, "action": cap}
        schema = protocol.check_manifest_entry(c.entry)["params_schema"]
        problem = protocol.validate_params(schema, params)
        started = time.monotonic()
        if problem:
            return {**base, "success": False,
                    "error": {"code": protocol.INVALID_PARAMS, "message": problem, "retryable": False}}
        try:
            result = c.run(params)
        except caps.Failed as e:
            return {**base, "success": False,
                    "error": {"code": e.code, "message": e.message, "retryable": e.retryable}}
        except Exception as e:
            logger.exception("%s failed", cap)
            return {**base, "success": False,
                    "error": {"code": protocol.EXECUTION_FAILED, "message": f"{cap} failed: {e}"[:300],
                              "retryable": False}}
        logger.info("ran %s", cap)
        return {**base, "success": True, "result": result,
                "duration_ms": int((time.monotonic() - started) * 1000)}

    # ── staying connected ───────────────────────────────────────────────────
    async def run_forever(self, *, on_state: Callable[[str], None] = lambda _s: None,
                          stop: asyncio.Event | None = None) -> None:
        """Connect, and reconnect whenever the link drops: 1 s doubling to
        60 s, full jitter. A refusal (revoked, not approved) is said once per
        change and retried slowly, so approving on the core is enough."""
        stop = stop or asyncio.Event()
        delay = 1.0
        last = ""
        while not stop.is_set():
            try:
                welcome = await self.connect()
            except Exception as e:   # noqa: BLE001 - any failure to reach the core
                state = f"can't reach the core ({type(e).__name__})"
                await self._quiet_close()
            else:
                if welcome.get("type") == "welcome":
                    on_state("connected")
                    delay = 1.0
                    last = "connected"
                    stopping = asyncio.ensure_future(stop.wait())
                    await asyncio.wait([self._reader, stopping], return_when=asyncio.FIRST_COMPLETED)
                    stopping.cancel()
                    await self._quiet_close()
                    if stop.is_set():
                        return
                    state = "connection dropped"
                else:
                    state = f"refused: {welcome.get('message') or welcome.get('code') or welcome.get('type')}"
                    delay = max(delay, 30.0)
            if state != last:
                on_state(state)
                last = state
            wait = random.uniform(0, delay)
            delay = min(60.0, delay * 2)
            try:
                await asyncio.wait_for(stop.wait(), wait)
            except asyncio.TimeoutError:
                pass

    async def _quiet_close(self) -> None:
        try:
            await self.close()
        except Exception:
            pass
        self.ws = None

    @property
    def connected(self) -> bool:
        return self.ws is not None and self._reader is not None and not self._reader.done()


def _desktop_notify(title: str, body: str) -> None:
    caps.notify(title, body)


# ── identity on disk ─────────────────────────────────────────────────────────

def identity_path() -> Path:
    return CONFIG_DIR / "identity.json"


def save_identity(agent: Agent) -> None:
    """The private key never leaves this file; it's readable by the user only."""
    p = identity_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch(mode=0o600, exist_ok=True)
    p.chmod(0o600)
    p.write_text(json.dumps({"url": agent.url, "device_id": agent.device_id, "name": agent.name,
                             "shared_dir": str(agent.shared_dir), "key": agent.key_pem().decode()}))


def load_identity() -> dict:
    p = identity_path()
    if not p.exists():
        raise SystemExit("Not paired yet: python -m nora.agent pair <core url> <code>")
    data = json.loads(p.read_text())
    data["key"] = serialization.load_pem_private_key(data["key"].encode(), password=None)
    if not isinstance(data["key"], ec.EllipticCurvePrivateKey):
        raise SystemExit("The saved key isn't a P-256 key; pair again.")
    return data


def from_identity(**kw) -> Agent:
    data = load_identity()
    return Agent(kw.pop("url", None) or data["url"], name=data.get("name", "travel laptop"),
                 key=data["key"], device_id=data["device_id"],
                 shared_dir=Path(data.get("shared_dir") or default_shared()), **kw)

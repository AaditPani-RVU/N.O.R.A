"""The real Device Hub on a throwaway store, driven over stdin — for the
Android app's JVM tests (`HubIntegrationTest`).

The Kotlin client is tested against the same Python code the phone will talk
to in production, not a reimplementation of it. Nothing here touches NORA's
live state: the store and the audit log go to a temp directory.

Protocol: one JSON object per line on stdout. The first line is
`{"url": ..., "code": ...}`. Then each command line on stdin gets one reply:

    code                                  → {"code": "ABCD-EFGH"}
    approve <device_id>                   → {"ok": true}
    revoke <device_id>                    → {"ok": true}
    sessions                              → {"sessions": [...]}
    caps                                  → {"caps": {name: device_id}}
    invoke <device_id> <cap> <json> [origin]
                                          → {"success", "message", "error_code"}
    invocations                           → {"rows": [...]}
    deliver <device_id> <kind> <text>     → {"ok": true}   (a job or reminder)
    turns                                 → {"turns": [...]} (typed turns received)
    quit

Typed turns (`utterance`) don't reach a model here: `_fake_turn` echoes them,
and "confirm …" asks the device first. A voice turn asking for the core's
voice gets fake PCM: one sample per character of each line.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

_tmp = Path(tempfile.mkdtemp(prefix="nora-harness-"))
os.environ["NORA_STORE_PATH"] = str(_tmp / "nora_core.db")

from nora import audit_log, channel, command_engine, delivery, store  # noqa: E402
from nora.hub import registry  # noqa: E402
from nora.hub.server import Hub  # noqa: E402

audit_log._LOG_PATH = _tmp / "audit.jsonl"


def out(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


# Typed turns from the device run this instead of the real pipeline: the
# test is about the chat round trip, not the model. "confirm …" asks the
# device's user first; anything else is echoed.
_turns: list[str] = []


async def _fake_turn(text, deps, rms=0.0, channel=None):
    from nora.pipeline import TurnOutcome
    from nora.schemas import ActionStep

    _turns.append(text)
    if text.startswith("confirm "):
        approved = await channel.confirm(_channel_mod.ConfirmRequest(
            turn_id=channel.turn_id, rendered="Ping the phone?", expires_in=10,
            steps=[ActionStep(action="test.ping", parameters={"text": text[8:]})]))
        channel.speak("Done." if approved else "Left it.")
        return TurnOutcome(kind="executed" if approved else "cancelled", text=text)
    channel.speak(f"You said: {text}")
    channel.speak("That's all.")
    return TurnOutcome(kind="chat", text=text)


def _stub_turns() -> None:
    import contextlib
    # stdout is the reply channel; pygame greets whoever imports it there.
    with contextlib.redirect_stdout(sys.stderr):
        from nora import pipeline, wiring
    pipeline.handle_turn = _fake_turn
    wiring.build = lambda **_kw: None
    # Voice turns asking for the core's voice get fake PCM instead of Kokoro:
    # one 16-bit sample per character, value = the character, so the app's
    # test can check what it played.
    from nora.hub import voice
    voice.LATENCY_PATH = _tmp / "voice_latency.jsonl"
    voice.core_voice_available = lambda: True
    voice._kokoro = lambda text, rate: (b"".join(ord(c).to_bytes(2, "little") for c in text),
                                        voice.KOKORO_RATE)


_channel_mod = channel


def main() -> None:
    _stub_turns()
    hub = Hub()
    port = hub.start("127.0.0.1", 0)
    code, _ = registry.create_code()
    out({"url": f"ws://127.0.0.1:{port}/v1/device", "code": code})

    for line in sys.stdin:
        parts = line.strip().split(" ", 3)
        cmd = parts[0] if parts else ""
        try:
            if cmd == "quit":
                break
            elif cmd == "code":
                out({"code": registry.create_code()[0]})
            elif cmd == "approve":
                out({"ok": registry.approve(parts[1])})
            elif cmd == "revoke":
                out({"ok": registry.revoke(parts[1])})
            elif cmd == "sessions":
                out({"sessions": hub.sessions()})
            elif cmd == "caps":
                names = [n for n in command_engine.get_available_actions()
                         if command_engine.get_action_meta(n).device]
                out({"caps": {n: command_engine.get_action_meta(n).device for n in names}})
            elif cmd == "invoke":
                device_id, cap = parts[1], parts[2]
                rest = parts[3] if len(parts) > 3 else "{}"
                params_json, _, origin = rest.partition(" ")
                ch = None
                if (origin or "live_user") == "live_user":
                    ch = channel.Channel(device_id="local", kind="voice",
                                         speak=lambda *_a, **_k: None)
                result = asyncio.run(hub.call(device_id, cap, json.loads(params_json),
                                              channel=ch))
                out({"success": result.success, "message": result.message,
                     "error_code": getattr(result, "error_code", None)})
            elif cmd == "deliver":
                # deliver <device_id> <kind> <text>: as a finished job or reminder would.
                out({"ok": delivery.deliver(parts[3], device=parts[1], kind=parts[2])})
            elif cmd == "turns":
                out({"turns": _turns})
            elif cmd == "invocations":
                with store.transaction() as conn:
                    rows = conn.execute(
                        "SELECT capability, origin, tier, status, error_code"
                        " FROM invocations ORDER BY created_at").fetchall()
                out({"rows": [list(r) for r in rows]})
            else:
                out({"error": f"unknown command {cmd!r}"})
        except Exception as e:  # the test reads the error, the harness keeps going
            out({"error": f"{type(e).__name__}: {e}"})
    hub.stop()


if __name__ == "__main__":
    main()

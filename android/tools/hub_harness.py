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
    quit
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

from nora import audit_log, channel, command_engine, store  # noqa: E402
from nora.hub import registry  # noqa: E402
from nora.hub.server import Hub  # noqa: E402

audit_log._LOG_PATH = _tmp / "audit.jsonl"


def out(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


def main() -> None:
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

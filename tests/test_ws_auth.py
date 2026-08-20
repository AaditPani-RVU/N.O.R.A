"""WebSocket auth + audio relay against a real socket (nora/ui_server.py).

Stdlib unittest only — run with:  python -m unittest tests.test_ws_auth -v

Regression cover for the query-string auth break: websockets >= 14 removed
`connection.path` in favour of `connection.request.path`. Reading only the old
attribute made every token in a URL resolve to "", so remote clients silently
fell through to the first-message wait and were rejected. Mocks would not have
caught it — this stands up an actual server.
"""
from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path

from nora import audio_relay, ui_server

try:
    import websockets
    _HAS_WS = True
except ImportError:
    _HAS_WS = False

TOKEN = "regression-token"
PORT = 8794


@unittest.skipUnless(_HAS_WS, "websockets not installed")
class TestWebSocketAuth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ui_server.start_ws(port=PORT, token=TOKEN)
        time.sleep(1.0)   # daemon thread needs its loop up before we connect

    def _run(self, coro):
        return asyncio.run(asyncio.wait_for(coro, timeout=10))

    def test_query_string_token_is_accepted(self):
        async def go():
            async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws?token={TOKEN}") as ws:
                return json.loads(await ws.recv())
        self.assertEqual(self._run(go())["type"], "auth_ok")

    def test_wrong_token_is_rejected(self):
        async def go():
            async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws?token=nope") as ws:
                return json.loads(await ws.recv())
        msg = self._run(go())
        self.assertEqual(msg["type"], "error")
        self.assertEqual(msg["message"], "Unauthorized")

    def test_first_message_auth_still_works(self):
        """Fallback path, for proxies that drop the query string."""
        async def go():
            async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws") as ws:
                await ws.send(json.dumps({"type": "auth", "token": TOKEN}))
                return json.loads(await ws.recv())
        self.assertEqual(self._run(go())["type"], "auth_ok")

    def test_audio_chunk_reaches_a_connected_client_intact(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.write(b"\xff\xfb" + b"PAYLOAD" * 8)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))

        async def go():
            async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws?token={TOKEN}") as ws:
                await ws.recv()   # auth_ok
                await ws.recv()   # state snapshot
                audio_relay._enabled = True
                audio_relay.push_chunk(tmp.name, "hello from nora")
                return json.loads(await ws.recv())

        msg = self._run(go())
        self.assertEqual(msg["type"], "audio")
        self.assertEqual(msg["text"], "hello from nora")
        self.assertEqual(base64.b64decode(msg["data"]), Path(tmp.name).read_bytes())


if __name__ == "__main__":
    unittest.main()

"""Phase 0 remote-surface hardening (NORA_DISTRIBUTED_PLAN.md §1, S1–S6).

Stdlib unittest only — run with:  python -m unittest tests.test_remote_security -v

Each class pins one hole that was open before:
  S1  remote_mic accepted audio (i.e. commands) from anyone, unauthenticated
  S2  send_whatsapp / send_email / delete_calendar_event ran without confirmation
  S3  an unset NORA_API_TOKEN meant "open to the network", not "localhost only"
  S4  tokens were compared with == instead of in constant time
  S5  servers on 0.0.0.0 answered any LAN peer
  S6  dashboard POSTs (/type_command, /proc_kill, …) and data GETs had no auth,
      and `Access-Control-Allow-Origin: *` let any web page call them
Real sockets throughout, as in test_ws_auth / test_ui_routes.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import os
import threading
import time
import unittest
from http.server import HTTPServer
from unittest import mock

import numpy as np

from nora import remote_mic, security, text_input, ui_server

try:
    import websockets
    _HAS_WS = True
except ImportError:
    _HAS_WS = False

TOKEN = "s3cret-phase0"
UI_PORT = 8811
MIC_PORT = 8812
WS_OPEN_PORT = 8813
WS_TOKEN_PORT = 8814


class _TokenEnv:
    """Set or clear NORA_API_TOKEN for one test, restoring it afterwards."""

    def _set_token(self, value: str | None) -> None:
        prev = os.environ.get("NORA_API_TOKEN")
        self.addCleanup(self._restore, prev)
        if value is None:
            os.environ.pop("NORA_API_TOKEN", None)
        else:
            os.environ["NORA_API_TOKEN"] = value

    @staticmethod
    def _restore(prev: str | None) -> None:
        if prev is None:
            os.environ.pop("NORA_API_TOKEN", None)
        else:
            os.environ["NORA_API_TOKEN"] = prev


def _serve(handler, port: int) -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    return srv


def _request(port, method, path, body=b"", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def _drain_text_queue() -> list[str]:
    items = []
    while not text_input._queue.empty():
        items.append(text_input._queue.get_nowait())
    return items


# ── S3 / S4 / S5: the checks themselves ──────────────────────────────────────

class TestTokenCheck(_TokenEnv, unittest.TestCase):
    def test_matching_token_passes(self):
        self._set_token(TOKEN)
        self.assertTrue(security.check_api_token(TOKEN, peer="100.101.1.2"))

    def test_wrong_or_missing_token_fails(self):
        self._set_token(TOKEN)
        self.assertFalse(security.check_api_token("nope", peer="127.0.0.1"))
        self.assertFalse(security.check_api_token("", peer="127.0.0.1"))

    def test_comparison_is_constant_time(self):
        self._set_token(TOKEN)
        with mock.patch("nora.security.hmac.compare_digest", return_value=True) as cd:
            self.assertTrue(security.check_api_token("anything", peer="127.0.0.1"))
        cd.assert_called_once()

    def test_unset_token_allows_direct_loopback_only(self):
        self._set_token(None)
        self.assertTrue(security.check_api_token("", peer="127.0.0.1"))
        self.assertTrue(security.check_api_token("", peer="::ffff:127.0.0.1"))

    def test_unset_token_refuses_everyone_else(self):
        self._set_token(None)
        self.assertFalse(security.check_api_token("", peer="100.101.1.2"))
        self.assertFalse(security.check_api_token("", peer="192.168.1.20"))
        self.assertFalse(security.check_api_token("", peer=None))

    def test_unset_token_refuses_proxied_loopback(self):
        """Behind `tailscale serve` every request arrives from 127.0.0.1."""
        self._set_token(None)
        self.assertFalse(security.check_api_token("", peer="127.0.0.1", proxied=True))

    def test_proxy_headers_are_detected(self):
        self.assertTrue(security.is_proxied({"X-Forwarded-For": "100.1.2.3"}))
        self.assertTrue(security.is_proxied({"Tailscale-User-Login": "me@example.com"}))
        self.assertFalse(security.is_proxied({"Host": "localhost"}))


class TestPeerFilter(unittest.TestCase):
    def test_loopback_and_tailnet_are_allowed(self):
        for peer in ("127.0.0.1", "::1", "100.64.0.1", "100.127.255.254", "fd7a:115c:a1e0::5"):
            self.assertTrue(security.peer_allowed(peer), peer)

    def test_lan_internet_and_garbage_are_refused(self):
        for peer in ("192.168.1.20", "10.0.0.5", "172.16.3.4", "8.8.8.8", "100.128.0.1",
                     "not-an-ip", "", None):
            self.assertFalse(security.peer_allowed(peer), peer)

    def test_networks_come_from_config(self):
        cfg = {"security": {"remote_networks": ["192.168.1.0/24", "bogus"]}}
        with mock.patch("nora.security.get_config", return_value=cfg):
            self.assertTrue(security.peer_allowed("192.168.1.20"))
            self.assertFalse(security.peer_allowed("127.0.0.1"))


# ── S6: dashboard HTTP routes ────────────────────────────────────────────────

class TestDashboardRoutes(_TokenEnv, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = _serve(ui_server._Handler, UI_PORT)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self._set_token(TOKEN)
        _drain_text_queue()

    def _post(self, path, body, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return _request(UI_PORT, "POST", path, json.dumps(body).encode(), headers)

    def test_type_command_needs_the_token(self):
        status, _, _ = self._post("/type_command", {"text": "shutdown"})
        self.assertEqual(status, 401)
        self.assertEqual(_drain_text_queue(), [])

    def test_type_command_with_the_token_is_queued(self):
        status, _, _ = self._post("/type_command", {"text": "open spotify"}, token=TOKEN)
        self.assertEqual(status, 200)
        # Queued with where it was typed, so the transcript can say so (Sharp F).
        self.assertEqual(_drain_text_queue(), [("open spotify", "dashboard")])

    def test_proc_kill_needs_the_token(self):
        with mock.patch("nora.visuals.kill_process") as kill:
            status, _, _ = self._post("/proc_kill", {"pid": 1})
        self.assertEqual(status, 401)
        kill.assert_not_called()

    def test_other_posts_need_the_token(self):
        for path in ("/ptt", "/music_ctl", "/interrupt"):
            self.assertEqual(self._post(path, {})[0], 401, path)

    def test_data_routes_need_the_token(self):
        for path in ("/state", "/history", "/processes", "/memory_graph", "/analytics"):
            self.assertEqual(_request(UI_PORT, "GET", path)[0], 401, path)
        self.assertEqual(_request(UI_PORT, "GET", f"/state?token={TOKEN}")[0], 200)
        self.assertEqual(
            _request(UI_PORT, "GET", "/state", headers={"Authorization": f"Bearer {TOKEN}"})[0], 200)

    def test_page_and_scripts_load_without_the_token(self):
        for path in ("/", "/index.html", "/remote.js", "/takeovers.js", "/geo.js"):
            self.assertEqual(_request(UI_PORT, "GET", path)[0], 200, path)

    def test_no_cross_origin_access_is_granted(self):
        _, headers, _ = _request(UI_PORT, "GET", f"/state?token={TOKEN}",
                                 headers={"Origin": "https://evil.example"})
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        _, headers, _ = _request(UI_PORT, "OPTIONS", "/type_command",
                                 headers={"Origin": "https://evil.example",
                                          "Access-Control-Request-Method": "POST"})
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_peers_outside_remote_networks_get_403(self):
        with mock.patch("nora.security.peer_allowed", return_value=False):
            self.assertEqual(_request(UI_PORT, "GET", "/")[0], 403)
            status, _, _ = self._post("/type_command", {"text": "x"}, token=TOKEN)
        self.assertEqual(status, 403)
        self.assertEqual(_drain_text_queue(), [])

    def test_dashboard_sends_the_token_on_api_calls(self):
        page = _request(UI_PORT, "GET", "/")[2].decode("utf-8")
        self.assertIn("headers.set('Authorization', 'Bearer ' + token)", page)


# ── S1: remote mic ───────────────────────────────────────────────────────────

class TestRemoteMic(_TokenEnv, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = _serve(remote_mic._Handler, MIC_PORT)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self._set_token(TOKEN)
        self.silence = np.zeros(1600, dtype=np.float32).tobytes()

    def test_audio_without_token_is_refused_before_transcription(self):
        with mock.patch("nora.transcriber.transcribe") as tr:
            status, _, _ = _request(MIC_PORT, "POST", "/audio", self.silence)
        self.assertEqual(status, 401)
        tr.assert_not_called()

    def test_audio_with_token_is_accepted(self):
        status, _, body = _request(MIC_PORT, "POST", "/audio", self.silence,
                                   {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_ping_needs_the_token(self):
        self.assertEqual(_request(MIC_PORT, "GET", "/ping")[0], 401)
        self.assertEqual(
            _request(MIC_PORT, "GET", "/ping", headers={"Authorization": f"Bearer {TOKEN}"})[0], 200)

    def test_oversized_body_is_refused_unread(self):
        conn = http.client.HTTPConnection("127.0.0.1", MIC_PORT, timeout=5)
        try:
            conn.putrequest("POST", "/audio")
            conn.putheader("Authorization", f"Bearer {TOKEN}")
            conn.putheader("Content-Length", str(remote_mic._MAX_BODY_BYTES + 1))
            conn.endheaders()
            self.assertEqual(conn.getresponse().status, 413)
        finally:
            conn.close()

    def test_peers_outside_remote_networks_get_403(self):
        with mock.patch("nora.security.peer_allowed", return_value=False):
            status, _, _ = _request(MIC_PORT, "POST", "/audio", self.silence,
                                    {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 403)


# ── S3 / S5: WebSocket ───────────────────────────────────────────────────────

@unittest.skipUnless(_HAS_WS, "websockets not installed")
class TestWebSocketPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ui_server.start_ws(port=WS_OPEN_PORT, token="")
        ui_server.start_ws(port=WS_TOKEN_PORT, token=TOKEN)
        time.sleep(1.0)

    def _first_message(self, url):
        async def go():
            async with websockets.connect(url) as ws:
                return json.loads(await ws.recv())
        return asyncio.run(asyncio.wait_for(go(), timeout=10))

    def test_unset_token_still_serves_this_machine(self):
        self.assertEqual(self._first_message(f"ws://127.0.0.1:{WS_OPEN_PORT}/ws")["type"], "auth_ok")

    def test_unset_token_refuses_proxied_connections(self):
        async def go():
            async with websockets.connect(
                    f"ws://127.0.0.1:{WS_OPEN_PORT}/ws",
                    additional_headers={"X-Forwarded-For": "100.90.1.2"}) as ws:
                return json.loads(await ws.recv())
        msg = asyncio.run(asyncio.wait_for(go(), timeout=10))
        self.assertEqual(msg["type"], "error")

    def test_non_string_token_is_rejected_not_crashing(self):
        async def go():
            async with websockets.connect(f"ws://127.0.0.1:{WS_TOKEN_PORT}/ws") as ws:
                await ws.send(json.dumps({"type": "auth", "token": 12345}))
                return json.loads(await ws.recv())
        self.assertEqual(asyncio.run(asyncio.wait_for(go(), timeout=10))["type"], "error")

    def test_peers_outside_remote_networks_are_dropped(self):
        async def go():
            async with websockets.connect(f"ws://127.0.0.1:{WS_TOKEN_PORT}/ws?token={TOKEN}") as ws:
                return await ws.recv()
        with mock.patch("nora.security.peer_allowed", return_value=False):
            with self.assertRaises(websockets.exceptions.ConnectionClosed):
                asyncio.run(asyncio.wait_for(go(), timeout=10))


# ── S2: sending and deleting on the user's behalf ────────────────────────────

class TestOutboundActionsConfirm(unittest.TestCase):
    ACTIONS = ("send_whatsapp", "send_email", "delete_calendar_event")

    def test_security_policy_requires_confirmation(self):
        for action in self.ACTIONS:
            self.assertTrue(security.needs_confirmation(action), action)

    def test_command_metadata_requires_confirmation(self):
        import nora.commands.google_services  # noqa: F401  (registers the actions)
        import nora.commands.notifications  # noqa: F401
        from nora import command_engine
        for action in self.ACTIONS:
            meta = command_engine.get_action_meta(action)
            self.assertIsNotNone(meta, action)
            self.assertTrue(meta.requires_confirmation, action)
            self.assertEqual(meta.risk, "high", action)

    def test_learned_consent_can_never_skip_them(self):
        from nora import autonomy
        for action in self.ACTIONS:
            self.assertIn(action, autonomy._HARD_CONFIRM, action)


if __name__ == "__main__":
    unittest.main()

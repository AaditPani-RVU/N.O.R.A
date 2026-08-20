"""HTTP routing for the dashboard (nora/ui_server.py).

Stdlib unittest only — run with:  python -m unittest tests.test_ui_routes -v

Regression cover: `self.path` is the raw request target and includes the query
string, so routing on it directly made `/?token=…` match no branch and 404. That
is precisely the URL a remote client needs in order to pass NORA_API_TOKEN, so
the dashboard was unreachable from any device that had to authenticate — while
the bare `/` kept working, which is what made it easy to miss.
"""
from __future__ import annotations

import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer

from nora import ui_server

PORT = 8797


class TestQueryStringRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", PORT), ui_server._Handler)
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def _status(self, path: str) -> int:
        try:
            return urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code

    def test_dashboard_with_token_query(self):
        self.assertEqual(self._status("/?token=sample123"), 200)

    def test_bare_dashboard(self):
        self.assertEqual(self._status("/"), 200)

    def test_index_html_with_query(self):
        self.assertEqual(self._status("/index.html?token=sample123"), 200)

    def test_json_endpoints_tolerate_a_query_string(self):
        for path in ("/state", "/metrics", "/music"):
            self.assertEqual(self._status(f"{path}?cachebust=1"), 200, path)

    def test_unknown_route_still_404s(self):
        self.assertEqual(self._status("/definitely-not-a-route"), 404)

    def test_served_page_carries_the_audio_client(self):
        body = urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/?token=sample123", timeout=5).read()
        # If the phone gets a page, it must be a page that can actually play audio.
        self.assertIn(b"audio-unlock", body)
        self.assertIn(b"audio_stop", body)

    def test_interrupt_route_exists(self):
        # The dashboard STOP button and the Esc key both POST here; a 404 would
        # leave the UI unable to cut NORA off mid-sentence.
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/interrupt", data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST")
        self.assertEqual(urllib.request.urlopen(req, timeout=5).status, 200)


class TestTranscriptEcho(unittest.TestCase):
    """The dashboard transcript can only show the user's own side of the
    conversation if the server echoes heard utterances back to it."""

    def test_notify_user_bumps_the_sequence(self):
        before = ui_server._state.get("user_seq", 0)
        ui_server.notify_user("what is on my screen")
        self.assertEqual(ui_server._state["user_text"], "what is on my screen")
        self.assertEqual(ui_server._state["user_seq"], before + 1)

    def test_blank_utterances_are_ignored(self):
        ui_server.notify_user("something real")
        seq = ui_server._state["user_seq"]
        ui_server.notify_user("   ")
        ui_server.notify_user("")
        self.assertEqual(ui_server._state["user_seq"], seq)

    def test_repeated_reply_still_counts_as_a_new_turn(self):
        # Text alone cannot distinguish "NORA said the same thing again" from
        # "no new frame arrived", which is why the counter exists.
        ui_server.notify(speaking=True, text="I could not find that file.")
        first = ui_server._state["reply_seq"]
        ui_server.notify(speaking=True, text="I could not find that file.")
        self.assertEqual(ui_server._state["reply_seq"], first + 1)


if __name__ == "__main__":
    unittest.main()

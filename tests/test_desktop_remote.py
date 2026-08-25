"""The desktop remote — window map, snapshot identity, and route auth.

Stdlib unittest only — run with:
    python -m unittest tests.test_desktop_remote -v

The remote hands the dashboard a map of the focused window and takes back a
tap, so the things worth pinning are the ones that decide *what gets pressed*:
that an element id only means anything within the snapshot it came from, that
a snapshot goes stale rather than silently addressing a different widget, and
that the routes refuse to drive the desktop for an unauthenticated caller.

AT-SPI itself is faked. A real accessibility tree needs a live desktop session
with a11y enabled, which no test runner has; what these cover is the layer
NORA actually owns.
"""
from __future__ import annotations

import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer

from nora import desktop, ui_server

PORT = 8798


class FakeAccessible:
    """Just enough of an Atspi.Accessible for the re-check before acting."""

    def __init__(self, name, role):
        self._name, self._role = name, role

    def get_name(self):
        return self._name

    def get_role_name(self):
        return self._role


class FakeWidget:
    def __init__(self, name, role="button", kind="button", rect=(0, 0, 40, 20)):
        self.name = name
        self.role = role
        self.kind = kind
        self.description = ""
        self.rect = rect
        self.enabled = True
        self.focused = False
        self.checked = False
        self.value = ""
        self.accessible = FakeAccessible(name, role)


class FakeTree:
    """Enough of atspi_tree for desktop.py to run against."""

    def __init__(self, widgets, bounds=(0, 0, 800, 600)):
        self.widgets = widgets
        self.bounds = bounds
        self.showing = True
        self.actions = 1
        self.done = []

    def collect_interactive(self, limit=220, win=None, app_name=""):
        return {"ok": True, "app": app_name or "TestApp", "window": object(),
                "title": "Test Window", "bounds": self.bounds,
                "widgets": self.widgets[:limit],
                "truncated": len(self.widgets) > limit}

    def is_showing(self, _acc):
        return self.showing

    def get_action_count(self, _w):
        return self.actions

    def do_action(self, widget, _idx=0):
        self.done.append(widget.name)
        return True

    def set_text(self, widget, text):
        widget.value = text
        return True


def _install(tree):
    desktop._tree = lambda: tree
    return tree


class TestWindowMap(unittest.TestCase):
    def setUp(self):
        self._real = desktop._tree
        _install(FakeTree([
            FakeWidget("Save", rect=(10, 20, 60, 24)),
            FakeWidget("Cancel", rect=(80, 20, 60, 24)),
            FakeWidget("", role="text", kind="input", rect=(10, 60, 200, 24)),
        ]))

    def tearDown(self):
        desktop._tree = self._real

    def test_rects_are_window_relative(self):
        """Screen coords minus the window origin — the dashboard lays out in
        the window's own space and knows nothing about monitor offsets."""
        _install(FakeTree([FakeWidget("Save", rect=(910, 320, 60, 24))],
                          bounds=(900, 300, 400, 300)))
        m = desktop.window_map()
        self.assertTrue(m["ok"])
        el = m["elements"][0]
        self.assertEqual((el["x"], el["y"]), (10, 20))

    def test_elements_carry_kind_and_state(self):
        m = desktop.window_map()
        kinds = [e["kind"] for e in m["elements"]]
        self.assertEqual(kinds, ["button", "button", "input"])
        self.assertTrue(all(e["enabled"] for e in m["elements"]))

    def test_positional_flag_detects_missing_geometry(self):
        """A toolkit that reports no extents stacks everything at the origin;
        the dashboard needs to know to stop drawing a map."""
        _install(FakeTree([FakeWidget(f"b{i}", rect=(0, 0, 40, 20))
                           for i in range(8)]))
        self.assertFalse(desktop.window_map()["positional"])

    def test_real_layout_is_positional(self):
        self.assertTrue(desktop.window_map()["positional"])

    def test_unavailable_tree_reports_rather_than_raising(self):
        desktop._tree = lambda: None
        m = desktop.window_map()
        self.assertFalse(m["ok"])
        self.assertIn("AT-SPI", m["error"])


class TestActing(unittest.TestCase):
    def setUp(self):
        self._real = desktop._tree
        self.tree = _install(FakeTree([
            FakeWidget("Save"),
            FakeWidget("", role="text", kind="input"),
        ]))
        self.map = desktop.window_map()

    def tearDown(self):
        desktop._tree = self._real

    def test_click_uses_the_atspi_action(self):
        r = desktop.act(self.map["snap"], 0, "click")
        self.assertTrue(r["ok"])
        self.assertEqual(r["via"], "atspi")
        self.assertEqual(self.tree.done, ["Save"])

    def test_type_sets_field_text(self):
        r = desktop.act(self.map["snap"], 1, "type", "report.txt")
        self.assertTrue(r["ok"])
        self.assertEqual(self.tree.widgets[1].value, "report.txt")

    def test_type_refuses_a_button(self):
        r = desktop.act(self.map["snap"], 0, "type", "nope")
        self.assertFalse(r["ok"])
        self.assertIn("not a text field", r["error"])

    def test_stale_snapshot_is_refused(self):
        """An id is only meaningful inside the snapshot that produced it —
        honouring it against a newer tree presses something nobody saw."""
        r = desktop.act("not-the-current-snap", 0, "click")
        self.assertFalse(r["ok"])
        self.assertTrue(r["stale"])
        self.assertEqual(self.tree.done, [])

    def test_expired_snapshot_is_refused(self):
        desktop._snap_at = time.time() - (desktop._SNAP_TTL + 5)
        r = desktop.act(self.map["snap"], 0, "click")
        self.assertFalse(r["ok"])
        self.assertIn("expired", r["error"])

    def test_vanished_element_is_refused(self):
        self.tree.showing = False
        r = desktop.act(self.map["snap"], 0, "click")
        self.assertFalse(r["ok"])
        self.assertEqual(self.tree.done, [])

    def test_out_of_range_id(self):
        self.assertFalse(desktop.act(self.map["snap"], 99, "click")["ok"])

    def test_unknown_action(self):
        self.assertFalse(desktop.act(self.map["snap"], 0, "wiggle")["ok"])

    def test_a_new_map_invalidates_the_old_snapshot(self):
        old = self.map["snap"]
        desktop.window_map()
        self.assertTrue(desktop.act(old, 0, "click")["stale"])


class TestRouteAuth(unittest.TestCase):
    """The remote drives the user's actual desktop, so a configured token is
    required — unlike the read-only routes, which stay open."""

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

    def setUp(self):
        self._real = desktop._tree
        _install(FakeTree([FakeWidget("Save")]))
        self._prev = os.environ.get("NORA_API_TOKEN")
        os.environ["NORA_API_TOKEN"] = "s3cret"

    def tearDown(self):
        desktop._tree = self._real
        if self._prev is None:
            os.environ.pop("NORA_API_TOKEN", None)
        else:
            os.environ["NORA_API_TOKEN"] = self._prev

    def _get(self, path):
        try:
            r = urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5)
            return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, None

    def _post(self, path, body, token=""):
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        try:
            r = urllib.request.urlopen(req, timeout=5)
            return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_map_requires_the_token(self):
        self.assertEqual(self._get("/desktop")[0], 401)

    def test_map_with_token(self):
        status, body = self._get("/desktop?token=s3cret")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["elements"][0]["name"], "Save")

    def test_act_requires_the_token(self):
        self.assertEqual(self._post("/desktop_act", {"id": 0})[0], 401)

    def test_act_accepts_a_bearer_header(self):
        snap = self._get("/desktop?token=s3cret")[1]["snap"]
        status, body = self._post(
            "/desktop_act", {"snap": snap, "id": 0, "action": "click"}, token="s3cret")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_open_when_no_token_is_configured(self):
        os.environ.pop("NORA_API_TOKEN", None)
        self.assertEqual(self._get("/desktop")[0], 200)


class TestLaunchURL(unittest.TestCase):
    """The tab NORA opens on the desktop has to be able to reach the routes
    it just gated. It is a different origin from the LAN address a phone uses,
    so it inherits nothing from that tab having authenticated."""

    def setUp(self):
        self._prev = os.environ.get("NORA_API_TOKEN")
        self._started = []

    def tearDown(self):
        for srv in self._started:
            srv.shutdown()
            srv.server_close()
        if self._prev is None:
            os.environ.pop("NORA_API_TOKEN", None)
        else:
            os.environ["NORA_API_TOKEN"] = self._prev

    def _start(self, port):
        """Run start() for real, capturing the server so the port is released.

        start() returns only the URL, so the socket is caught on the way out
        by standing in for the constructor it uses.
        """
        real = ui_server.HTTPServer

        def capture(*a, **kw):
            srv = real(*a, **kw)
            self._started.append(srv)
            return srv

        ui_server.HTTPServer = capture
        try:
            return ui_server.start(port=port)
        finally:
            ui_server.HTTPServer = real

    def test_url_carries_the_token_when_one_is_set(self):
        os.environ["NORA_API_TOKEN"] = "s3cret"
        self.assertEqual(self._start(8799), "http://localhost:8799/?token=s3cret")

    def test_token_is_url_encoded(self):
        os.environ["NORA_API_TOKEN"] = "a b/c&d"
        self.assertEqual(
            self._start(8800), "http://localhost:8800/?token=a%20b%2Fc%26d")

    def test_bare_url_when_no_token_is_configured(self):
        os.environ.pop("NORA_API_TOKEN", None)
        self.assertEqual(self._start(8801), "http://localhost:8801")


if __name__ == "__main__":
    unittest.main()

"""Data producers behind the orb takeovers (nora/visuals.py) and their routes.

Stdlib unittest only — run with:  python -m unittest tests.test_visuals -v

The memory-graph tests skip when the Chroma store is empty or absent, so this
file stays runnable on a machine that has never run NORA. The process tests
always run: every machine has a process table.

Regression cover worth naming:
  * cpu_percent() is a delta since the previous read on the *same* Process
    object, so process_snapshot has to hold those objects between polls. A
    fresh object every call reports 0.0 forever and the constellation shows a
    completely idle machine.
  * The PCA projection is whitened. Without it the first component dominates,
    every point normalises toward one axis, and the constellation collapses
    into a single hemisphere instead of using the sphere it is drawn on.
"""
from __future__ import annotations

import threading
import time
import unittest
import urllib.request
from http.server import HTTPServer

from nora import ui_server, visuals

PORT = 8798


def _graph_or_skip():
    graph = visuals.memory_graph()
    if graph.get("error") or not graph.get("nodes"):
        raise unittest.SkipTest("no populated Chroma store on this machine")
    return graph


class TestProcessSnapshot(unittest.TestCase):
    def test_shape_and_ordering(self):
        snap = visuals.process_snapshot(limit=10)
        self.assertLessEqual(len(snap["procs"]), 10)
        self.assertGreater(snap["total"], 0)
        self.assertGreaterEqual(snap["cpu_count"], 1)
        for row in snap["procs"]:
            for key in ("pid", "name", "cpu", "rss", "mem_pct", "user", "self"):
                self.assertIn(key, row)
            self.assertGreaterEqual(row["cpu"], 0)
            self.assertGreaterEqual(row["rss"], 0)
        keys = [(r["cpu"], r["rss"]) for r in snap["procs"]]
        self.assertEqual(keys, sorted(keys, reverse=True),
                         "rows must be CPU-first, memory as the tiebreak")

    def test_cpu_is_a_delta_not_always_zero(self):
        # The first read primes; a later one has something to diff against.
        # If the Process objects were not cached between calls every row
        # would read 0.0 forever, which is the bug this guards.
        visuals.process_snapshot()
        time.sleep(0.7)
        snap = visuals.process_snapshot()
        self.assertTrue(any(r["cpu"] > 0 for r in snap["procs"]),
                        "no process reported any CPU across two polls")

    def test_limit_is_respected_but_total_is_honest(self):
        small = visuals.process_snapshot(limit=3)
        self.assertLessEqual(len(small["procs"]), 3)
        self.assertGreater(small["total"], len(small["procs"]))
        self.assertEqual(small["shown"], len(small["procs"]))

    def test_kill_missing_pid_reports_instead_of_raising(self):
        # Reached from a tap on a canvas: the caller wants something to
        # render, not an exception to swallow.
        result = visuals.kill_process(2 ** 31 - 1)
        self.assertFalse(result["ok"])
        self.assertIn("error", result)


class TestProjection(unittest.TestCase):
    def test_pca3_is_whitened(self):
        import numpy as np
        rng = np.random.default_rng(7)
        # One axis deliberately 50x the others: unwhitened, it swamps the rest.
        mat = rng.normal(size=(300, 24))
        mat[:, 0] *= 50.0
        out = visuals._pca3(mat)
        self.assertEqual(out.shape, (300, 3))
        sd = out.std(axis=0)
        self.assertLess(sd.max() / sd.min(), 1.30,
                        f"components not equalised: {sd}")

    def test_nodes_land_on_the_unit_sphere(self):
        import math
        graph = _graph_or_skip()
        for node in graph["nodes"][:60]:
            self.assertGreaterEqual(node["lat"], -90.0)
            self.assertLessEqual(node["lat"], 90.0)
            self.assertGreaterEqual(node["lon"], -180.0)
            self.assertLessEqual(node["lon"], 180.0)
            la, lo = math.radians(node["lat"]), math.radians(node["lon"])
            cl = math.cos(la)
            norm = math.hypot(cl * math.sin(lo), math.sin(la), cl * math.cos(lo))
            self.assertAlmostEqual(norm, 1.0, places=6)

    def test_graph_uses_more_than_one_hemisphere(self):
        # The whole reason for whitening. A collapsed projection puts nearly
        # everything on one side and wastes the sphere.
        graph = _graph_or_skip()
        lons = [n["lon"] for n in graph["nodes"]]
        east = sum(1 for v in lons if v >= 0)
        share = east / len(lons)
        self.assertGreater(share, 0.15)
        self.assertLess(share, 0.85)

    def test_edges_are_valid_and_thresholded(self):
        graph = _graph_or_skip()
        n = len(graph["nodes"])
        for a, b, score in graph["edges"]:
            self.assertTrue(0 <= a < n and 0 <= b < n)
            self.assertNotEqual(a, b)
            self.assertGreaterEqual(score, 0.45)
            self.assertLessEqual(score, 1.0)

    def test_nodes_carry_a_kind(self):
        graph = _graph_or_skip()
        kinds = {n["kind"] for n in graph["nodes"]}
        self.assertTrue(kinds <= {"knowledge", "episode"})

    def test_async_never_blocks(self):
        # The cold build is seconds of SVD and ui_server is single-threaded,
        # so a miss must return immediately rather than compute inline.
        started = time.time()
        got = visuals.memory_graph_async()
        self.assertLess(time.time() - started, 1.0)
        self.assertTrue(got.get("building") or got.get("nodes") is not None)


class TestRoutes(unittest.TestCase):
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

    def _get(self, path: str):
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=25) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()

    def test_processes_route(self):
        import json
        status, ctype, body = self._get("/processes")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        self.assertIn("procs", json.loads(body))

    def test_memory_graph_route_answers_immediately(self):
        import json
        started = time.time()
        status, _ctype, body = self._get("/memory_graph")
        self.assertEqual(status, 200)
        self.assertLess(time.time() - started, 2.0)
        payload = json.loads(body)
        self.assertTrue("nodes" in payload or payload.get("building"))

    def test_takeovers_js_is_served_same_origin(self):
        status, ctype, body = self._get("/takeovers.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", ctype)
        self.assertIn(b"NORA_TAKEOVERS", body)

    def test_dashboard_still_loads_the_takeover_bundle(self):
        _status, _ctype, body = self._get("/")
        for needle in (b"/takeovers.js", b'id="takec"', b'id="tk-card"'):
            self.assertIn(needle, body)


class TestNotifyTakeover(unittest.TestCase):
    def test_seq_advances_so_the_page_fires_once_per_request(self):
        ui_server.notify_takeover("processes")
        first = ui_server._state["takeover_seq"]
        ui_server.notify_takeover("memory", focus=["knowledge:x"])
        second = ui_server._state["takeover_seq"]
        self.assertEqual(second, first + 1)
        self.assertEqual(ui_server._state["takeover"]["kind"], "memory")
        self.assertEqual(ui_server._state["takeover"]["focus"], ["knowledge:x"])

    def test_off_is_a_real_kind(self):
        ui_server.notify_takeover("off")
        self.assertEqual(ui_server._state["takeover"]["kind"], "off")


if __name__ == "__main__":
    unittest.main()

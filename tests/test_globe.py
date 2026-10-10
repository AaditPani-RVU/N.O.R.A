"""Sharp Phase H: the globe, God's Eye View behind five flat commands.

Against a fake GEV that answers MCP the way the real one does (shapes taken
from a08a53c), so the suite never needs the service running. Exit criteria
covered here: the three questions route right and are answered, the
dashboard gets the view, the prompt grows by at most 300 tokens, and with
the globe stopped NORA says so instead of hanging.
"""
from __future__ import annotations

import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from nora import command_engine, doctor, fast_path, gev, intent_parser
from nora.commands import globe


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _view(lat, lon, alt, layers, follow=None):
    return {"camera": {"lat": lat, "lon": lon, "altitude_m": alt, "heading_deg": 0, "pitch_deg": -90},
            "layers": layers, "style": None, "map": None, "follow": follow, "annotations": [],
            "url": "http://127.0.0.1:5173/#v=2"}


class FakeGev:
    """Answers tools/call like GEV does: text summary, structuredContent."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.rise = datetime.now(timezone.utc).astimezone().replace(hour=5, minute=5, second=0) + timedelta(days=1)

    def answer(self, name: str, args: dict) -> dict:
        self.calls.append((name, args))
        area = args.get("area") or args.get("location") or {}
        place = area.get("place", "")
        if place == "Atlantis":
            return {"isError": True, "content": [{"type": "text", "text": 'No place matched "Atlantis"; try a more specific name or a bbox'}]}
        if name == "aircraft_in_area":
            if place == "Bengaluru":            # a city boundary that misses the airport
                return self._ok("0 aircraft in Bengaluru, Bangalore North, Karnataka, India.",
                                {"total": 0, "rows": [], "view": _view(12.97, 77.59, 50828, ["flights"])})
            rows = [{"callsign": "AIC101 ", "altitude_m": 9304}, {"callsign": "IGO22", "altitude_m": 3000}]
            label = "Greater London, England, United Kingdom" if place else f"{area.get('radius_km')} km around"
            return self._ok(f"38 aircraft in {label}.", {"total": 38, "rows": rows,
                                                         "view": _view(51.5, -0.12, 73373, ["flights"])})
        if name == "next_satellite_pass":
            return self._ok("ISS (ZARYA) next rises over Bengaluru at ...", {
                "satellite": "ISS (ZARYA)", "location": {"label": "Bengaluru, Karnataka, India"},
                "pass": {"rise": _iso(self.rise), "max_elevation_deg": 38.5, "rise_direction": "SSW", "visible": True},
                "view": _view(12.97, 77.59, 3000000, ["satellites"], {"kind": "satellite", "id": "25544"})})
        if name == "get_earthquakes":
            return self._ok("4 earthquakes of M2.5+ in the last 24 hours in Indonesia; strongest M5.1 133 km WNW of Ternate, Indonesia.",
                            {"total": 4, "rows": [{"magnitude": 5.1, "place": "133 km WNW of Ternate"}],
                             "view": _view(-2, 118, 4410632, ["earthquakes"])})
        if name == "situation_brief":
            return self._ok("Situation in Taiwan: Weather at Taiwan: partly cloudy. 0 earthquakes of M2.5+ in the last 24 hours in Taiwan. "
                            "16 aircraft in Taiwan (regional feed: 250nm regional fallback).",
                            {"view": _view(23.7, 121, 400000, ["flights", "earthquakes"])})
        if name == "show_in_gods_eye_view":
            return self._ok("Open Paris in God's Eye View: http://...",
                            {"view": _view(48.86, 2.32, 21763, args.get("layers") or [])})
        return {"isError": True, "content": [{"type": "text", "text": "unknown tool"}]}

    @staticmethod
    def _ok(summary: str, data: dict) -> dict:
        return {"content": [{"type": "text", "text": summary + "\n\n" + json.dumps(data)}],
                "structuredContent": data}

    def serve(self) -> HTTPServer:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if req["method"] == "ping":
                    result = {}
                else:
                    result = fake.answer(req["params"]["name"], req["params"]["arguments"])
                body = json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server


class _WithGev(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeGev()
        self.server = self.fake.serve()
        self.addCleanup(self.server.shutdown)
        cfg = {"globe": {"enabled": True, "url": f"http://127.0.0.1:{self.server.server_port}", "timeout_sec": 5}}
        self.shown: list[dict] = []
        for p in (mock.patch.object(gev, "get_config", return_value=cfg),
                  mock.patch("nora.ui_server.notify_takeover",
                             side_effect=lambda kind, **kw: self.shown.append({"kind": kind, **kw})),
                  mock.patch.object(globe, "_here", return_value={"name": "Bengaluru", "lat": 12.97, "lon": 77.59})):
            p.start()
            self.addCleanup(p.stop)


class GlobeCommandsTest(_WithGev):
    def test_flights_over_a_place(self) -> None:
        said = globe.flights_near("London")
        self.assertEqual(said, "38 aircraft in Greater London. The nearest is AIC101 at 9,300 metres.")
        self.assertEqual(self.shown[-1]["kind"], "globe")
        self.assertEqual(self.shown[-1]["view"]["layers"], ["flights"])
        self.assertNotIn("url", self.shown[-1]["view"])

    def test_a_city_boundary_that_misses_the_flights_widens_to_a_radius(self) -> None:
        said = globe.flights_near("Bengaluru")
        self.assertTrue(said.startswith("38 aircraft within 60 km of Bengaluru."), said)
        self.assertEqual(self.fake.calls[-1][1]["area"], {"lat": 12.97, "lon": 77.59, "radius_km": 60})

    def test_flights_over_me_use_where_the_user_is(self) -> None:
        said = globe.flights_near("me")
        self.assertTrue(said.startswith("38 aircraft around you right now."), said)
        self.assertEqual(self.fake.calls[-1][1]["area"]["lat"], 12.97)

    def test_the_iss_pass_is_said_in_local_time_and_words(self) -> None:
        said = globe.satellite_pass("", "the space station")
        self.assertEqual(said, "The ISS next rises over you at 5:05 AM tomorrow, in the south-southwest, "
                               "peaking at 39 degrees. You'll be able to see it.")
        self.assertEqual(self.fake.calls[-1][1], {"location": {"lat": 12.97, "lon": 77.59}, "satellite": "ISS"})

    def test_earthquakes_are_said_the_way_a_person_would(self) -> None:
        said = globe.quakes_near("Indonesia")
        self.assertEqual(said, "4 earthquakes of magnitude 2.5 or more in the last day in Indonesia; the strongest "
                               "was magnitude 5.1, 133 km west-northwest of Ternate, Indonesia.")

    def test_a_situation_brief_drops_the_screen_furniture(self) -> None:
        said = globe.situation_brief("Taiwan")
        self.assertEqual(said, "Weather: partly cloudy. No earthquakes of magnitude 2.5 or more in the last day "
                               "in Taiwan. 16 aircraft in Taiwan.")

    def test_globe_show_turns_friendly_layer_names_into_gevs(self) -> None:
        said = globe.globe_show("Paris", "planes and quakes")
        self.assertEqual(self.fake.calls[-1][1]["layers"], ["flights", "earthquakes"])
        self.assertEqual(said, "Showing Paris on the globe with flights, earthquakes on.")
        # A city close-up gets photoreal 3D; a world view keyless imagery.
        self.assertEqual(self.shown[-1]["view"]["map"], "photoreal")
        globe.quakes_near("Indonesia")
        self.assertEqual(self.shown[-1]["view"]["map"], "esri-imagery")

    def test_an_unknown_place_is_said_plainly(self) -> None:
        self.assertEqual(globe.flights_near("Atlantis"), "I couldn't find a place called Atlantis.")

    def test_a_slow_upstream_is_not_called_a_stopped_globe(self) -> None:
        with mock.patch.object(gev, "call", side_effect=gev.Unavailable("timed out")):
            self.assertIn("live data didn't come back", globe.flights_near("London"))

    def test_the_doctor_sees_the_globe(self) -> None:
        self.assertTrue(doctor.check_globe()[0].ok)


class GlobeStoppedTest(unittest.TestCase):
    def test_with_the_globe_stopped_nora_says_so_without_hanging(self) -> None:
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                                   # nothing listens there now
        cfg = {"globe": {"enabled": True, "url": f"http://127.0.0.1:{port}"}}
        with mock.patch.object(gev, "get_config", return_value=cfg), \
                mock.patch.object(globe, "_here", return_value={"lat": 1, "lon": 2}):
            for fn in (globe.flights_near, globe.satellite_pass, globe.quakes_near, globe.situation_brief,
                       globe.globe_show):
                t0 = time.monotonic()
                self.assertIn("globe isn't running", fn("Tokyo"))
                self.assertLess(time.monotonic() - t0, 1.0)
            finding = doctor.check_globe()[0]
        self.assertFalse(finding.ok)
        self.assertIn("nora-globe", finding.fix)


class GlobeRoutingTest(unittest.TestCase):
    CASES = [
        ("when does the ISS pass over me", "satellite_pass", {"place": ""}),
        ("When will the space station pass over Tokyo?", "satellite_pass", {"place": "Tokyo"}),
        ("what's flying over Delhi", "flights_near", {"place": "Delhi"}),
        ("how many planes are over me right now", "flights_near", {"place": ""}),
        ("earthquakes near Kathmandu", "quakes_near", {"place": "Kathmandu"}),
        ("show flights over Tokyo on the globe", "globe_show", {"place": "Tokyo", "layers": "flights"}),
    ]
    NOT_GLOBE = ["book me flights to London", "tell me about planes", "show me Paris", "flights to london"]

    def test_the_three_questions_route_without_a_model(self) -> None:
        for text, action, params in self.CASES:
            with self.subTest(text=text):
                r = fast_path.resolve(text)
                self.assertIsNotNone(r)
                self.assertEqual(r.steps[0].action, action)
                for k, v in params.items():
                    self.assertEqual(r.steps[0].parameters.get(k), v)

    def test_near_misses_stay_off_the_globe(self) -> None:
        for text in self.NOT_GLOBE:
            with self.subTest(text=text):
                r = fast_path.resolve(text)
                acts = [s.action for s in r.steps] if r and r.steps else []
                self.assertFalse(set(acts) & {"flights_near", "satellite_pass", "quakes_near", "globe_show"})


class PromptBudgetTest(unittest.TestCase):
    GLOBE = {"flights_near", "satellite_pass", "quakes_near", "situation_brief", "globe_show"}
    TEXTS = ["what's the weather in London", "tell me about Tokyo", "earthquakes near Kathmandu",
             "open firefox and play some music", "remind me to call mum at six", "what's on my calendar tomorrow",
             "show flights and earthquakes over Tokyo on the globe", "plan a trip to Ladakh"]

    def test_the_adapter_grows_the_intent_prompt_by_at_most_300_tokens(self) -> None:
        command_engine.discover_commands()
        full = {t: intent_parser._build_system_prompt({}, None, t) for t in self.TEXTS}
        meta = {k: v for k, v in command_engine._meta.items() if k not in self.GLOBE}
        reg = {k: v for k, v in command_engine._registry.items() if k not in self.GLOBE}
        with mock.patch.dict(command_engine._meta, meta, clear=True), \
                mock.patch.dict(command_engine._registry, reg, clear=True):
            without = {t: intent_parser._build_system_prompt({}, None, t) for t in self.TEXTS}
        for t in self.TEXTS:
            with self.subTest(text=t):
                self.assertLessEqual((len(full[t]) - len(without[t])) / 4, 300)


if __name__ == "__main__":
    unittest.main()

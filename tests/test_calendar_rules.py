"""Calendar rules (Phase 8c): leave-by alerts and the night-before alarm."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from unittest import mock

import pytest

from nora import calendar_rules as cr
from nora import places, reversible, store, task_ledger, triggers

CFG = {**cr._DEFAULTS}
HOME = (12.9716, 77.5946)
COLLEGE = (12.9236, 77.4987)


@pytest.fixture(autouse=True)
def fresh():
    store.reset_for_tests()
    triggers.reset_for_tests()
    cr._routes.clear()
    with mock.patch.object(cr, "_cfg", return_value=CFG), \
            mock.patch.object(triggers, "sync_all"), \
            mock.patch.object(cr, "sync_all") as sync:
        places.save("home", lat=HOME[0], lon=HOME[1])
        places.save("college", lat=COLLEGE[0], lon=COLLEGE[1])
        yield sync
    store.reset_for_tests()


def route(seconds: float, mode: str = "driving"):
    return mock.patch.object(places, "route", return_value=places.Route(seconds, 12000, mode))


def ev(summary: str, start: datetime, location: str = "", id_: str = "e1") -> cr.CalEvent:
    return cr.CalEvent(f"{id_}@{start.isoformat()}", summary, start, location)


def google(summary: str, start: datetime, location: str = "") -> dict:
    return {"id": summary, "summary": summary, "location": location,
            "start": {"dateTime": start.astimezone().isoformat()}}


# ── reading the calendar ─────────────────────────────────────────────────────

def test_all_day_and_cancelled_events_are_left_out_and_the_rest_sorted():
    nine, eight = datetime(2026, 11, 24, 9), datetime(2026, 11, 24, 8)
    raw = [google("Lecture", nine), {"summary": "Holiday", "start": {"date": "2026-11-24"}},
           {**google("Gone", eight), "status": "cancelled"}, google("Gym", eight)]
    got = cr.parse_events(raw)
    assert [e.summary for e in got] == ["Gym", "Lecture"]
    assert got[1].start == nine


@pytest.mark.parametrize("loc", ["", "https://meet.google.com/abc", "Zoom", "Microsoft Teams Meeting",
                                 "Online"])
def test_online_or_placeless_events_have_nowhere_to_go(loc):
    assert ev("x", datetime.now(), loc).online


def test_a_real_address_is_somewhere():
    assert not ev("x", datetime.now(), "RV University, Mysore Road").online


# ── where ────────────────────────────────────────────────────────────────────

def test_origin_is_home_until_the_phone_has_crossed_anything():
    assert cr.origin_now().name == "home"


def test_origin_is_the_place_the_phone_is_inside():
    triggers.record_crossing(triggers.Event("d", "geofence.transition",
                                            {"place": "college", "transition": "enter"}, time.time()))
    assert cr.origin_now().name == "college"


def test_no_origin_once_the_phone_has_left_everywhere_known():
    triggers.record_crossing(triggers.Event("d", "geofence.transition",
                                            {"place": "home", "transition": "exit"}, time.time()))
    assert cr.origin_now() is None


def test_a_location_naming_a_saved_place_needs_no_geocoding():
    with mock.patch.object(places, "geocode") as geo:
        assert cr.destination("College - Room 204").name == "college"
    geo.assert_not_called()


def test_geocoded_locations_are_cached_hits_and_misses():
    with mock.patch.object(places, "geocode", return_value=(12.9, 77.6, "Phoenix")) as geo:
        assert cr.destination("Phoenix Marketcity").lat == 12.9
        assert cr.destination("phoenix  marketcity").lat == 12.9
    assert geo.call_count == 1
    with mock.patch.object(places, "geocode", return_value=None) as geo:
        assert cr.destination("Nowhere Lane") is None
        assert cr.destination("Nowhere Lane") is None
    assert geo.call_count == 2          # near home, then anywhere; then cached


def test_travel_pads_a_drive_for_traffic():
    with route(20 * 60):
        trip = cr.travel(cr.home(), cr.Spot("college", *COLLEGE))
    assert trip.seconds == pytest.approx(20 * 60 * 1.3)
    assert trip.spoken == "about 26 minutes by car with traffic"


def test_travel_to_where_you_are_is_nothing():
    with mock.patch.object(places, "route") as r:
        assert cr.travel(cr.home(), cr.Spot("x", HOME[0] + 0.0005, HOME[1])).seconds == 0
    r.assert_not_called()


# ── leave by ─────────────────────────────────────────────────────────────────

def test_leave_by_alert_comes_heads_up_minutes_before_the_time_to_leave():
    now = datetime(2026, 11, 24, 8, 0)
    lecture = ev("Lecture", datetime(2026, 11, 24, 9, 0), "college")
    # 20 min drive × 1.3 = 26 min, + 10 min buffer → leave at 8:24; heads-up at 8:19.
    with route(20 * 60), mock.patch.object(cr, "_tell") as tell:
        assert cr.check_leave_by(now, [lecture]) == 0
        assert cr.check_leave_by(now.replace(minute=19), [lecture]) == 1
        assert cr.check_leave_by(now.replace(minute=20), [lecture]) == 0      # once only
    text = tell.call_args.args[0]
    assert text == ("Leave by 8:24 AM for Lecture (9 AM at college): "
                    "about 26 minutes by car with traffic.")
    assert tell.call_args.kwargs == {"kind": "leave", "say_at_home": True}


def test_an_alert_seen_late_says_leave_now():
    lecture = ev("Lecture", datetime(2026, 11, 24, 9, 0), "college")
    with route(20 * 60), mock.patch.object(cr, "_tell") as tell:
        cr.check_leave_by(datetime(2026, 11, 24, 8, 40), [lecture])
    assert tell.call_args.args[0].startswith("Time to leave for Lecture")


def test_no_alert_when_you_are_somewhere_unknown():
    triggers.record_crossing(triggers.Event("d", "geofence.transition",
                                            {"place": "home", "transition": "exit"}, time.time()))
    lecture = ev("Lecture", datetime(2026, 11, 24, 9, 0), "college")
    with route(20 * 60), mock.patch.object(cr, "_tell") as tell:
        assert cr.check_leave_by(datetime(2026, 11, 24, 8, 30), [lecture]) == 0
    tell.assert_not_called()


def test_no_alert_for_an_event_where_you_already_are():
    triggers.record_crossing(triggers.Event("d", "geofence.transition",
                                            {"place": "college", "transition": "enter"}, time.time()))
    lecture = ev("Lecture", datetime(2026, 11, 24, 9, 0), "college")
    with mock.patch.object(cr, "_tell") as tell:
        assert cr.check_leave_by(datetime(2026, 11, 24, 8, 50), [lecture]) == 0
    tell.assert_not_called()


def test_events_past_or_far_off_or_online_are_not_looked_up():
    now = datetime(2026, 11, 24, 8, 0)
    evs = [ev("Past", now - timedelta(minutes=5), "college", "a"),
           ev("Tonight", now + timedelta(hours=10), "college", "b"),
           ev("Standup", now + timedelta(minutes=30), "https://zoom.us/j/1", "c")]
    with mock.patch.object(cr, "destination") as dest:
        assert cr.check_leave_by(now, evs) == 0
    dest.assert_not_called()


def test_a_place_that_cant_be_found_is_tried_once():
    lecture = ev("Party", datetime(2026, 11, 24, 9, 0), "Somewhere odd")
    with mock.patch.object(cr, "destination", return_value=None) as dest:
        cr.check_leave_by(datetime(2026, 11, 24, 8, 0), [lecture])
        cr.check_leave_by(datetime(2026, 11, 24, 8, 1), [lecture])
    assert dest.call_count == 1


# ── the night before ─────────────────────────────────────────────────────────

def test_morning_alarm_allows_for_travel_buffer_and_getting_ready():
    with route(20 * 60):
        m = cr.plan_morning([ev("Lecture", datetime(2026, 11, 24, 9, 0), "college")])
    # 9:00 − 26 min − 10 min − 60 min = 7:24 → down to 7:20.
    assert m.alarm == datetime(2026, 11, 24, 7, 20)
    assert cr.morning_say(m) == ("Good morning. Lecture is at 9 AM at college. "
                                 "Leave by 8:24 AM; it's about 26 minutes by car with traffic.")


def test_a_late_start_leaves_the_alarm_alone():
    assert cr.plan_morning([ev("Brunch", datetime(2026, 11, 24, 11, 30), "college")]) is None
    assert cr.plan_morning([]) is None


def test_an_online_first_event_only_needs_getting_ready():
    m = cr.plan_morning([ev("Standup", datetime(2026, 11, 24, 9, 0), "https://meet.google.com/x")])
    assert m.alarm == datetime(2026, 11, 24, 8, 0) and m.trip is None


def test_night_before_sets_the_alarm_once_and_undo_cancels_it(fresh):
    task_ledger.create_task("Submit the assignment", due_on=date(2026, 11, 24))
    lecture = ev("Lecture", datetime(2026, 11, 24, 9, 0), "college")
    with route(20 * 60), mock.patch.object(cr, "_wake_phone", return_value="d_phone"), \
            mock.patch.object(cr, "_tell") as tell, \
            mock.patch("time.time", return_value=datetime(2026, 11, 23, 21, 40).timestamp()):
        assert not cr.check_night_before(datetime(2026, 11, 23, 21, 0), [lecture])   # too early
        assert cr.check_night_before(datetime(2026, 11, 23, 21, 40), [lecture])
        assert not cr.check_night_before(datetime(2026, 11, 23, 21, 41), [lecture])
        assert cr.alarms() == [{"id": "2026-11-24",
                                "at_ms": int(datetime(2026, 11, 24, 7, 20).timestamp() * 1000),
                                "title": "Lecture at 9 AM",
                                "say": cr.morning_say(cr.plan_morning([lecture]))}]
        text = tell.call_args.args[0]
        assert "alarm on your phone for 7:20 AM" in text and "Submit the assignment" in text
        assert fresh.called
        entry = reversible.get_recent_reversible(5)[-1]
        assert entry["inverse_action"] == "cancel_wake_alarm"
        from nora.commands import wake_alarms
        assert wake_alarms.cancel_wake_alarm(**entry["inverse_params"]) == \
            "Cancelled the 7:20 AM alarm on your phone."
        assert cr.alarms() == []


def test_night_before_waits_for_the_phone():
    lecture = ev("Lecture", datetime(2026, 11, 24, 9, 0), "college")
    with route(20 * 60), mock.patch.object(cr, "_tell"):
        with mock.patch.object(cr, "_wake_phone", return_value=""):
            assert not cr.check_night_before(datetime(2026, 11, 23, 21, 40), [lecture])
        with mock.patch.object(cr, "_wake_phone", return_value="d_phone"):
            assert cr.check_night_before(datetime(2026, 11, 23, 22, 10), [lecture])


def test_a_free_morning_says_so_without_an_alarm():
    with mock.patch.object(cr, "_wake_phone", return_value="d_phone"), \
            mock.patch.object(cr, "_tell") as tell:
        assert not cr.check_night_before(datetime(2026, 11, 23, 21, 40),
                                         [ev("Lunch", datetime(2026, 11, 24, 13, 0), "college")])
    assert "left your alarm alone" in tell.call_args.args[0]
    assert cr.alarms() == []


# ── telling ──────────────────────────────────────────────────────────────────

def test_tell_speaks_at_home_and_sends_to_phones():
    spoken = []
    with mock.patch.object(cr, "_speak", spoken.append), \
            mock.patch("nora.delivery.connected", return_value=["d_phone"]), \
            mock.patch("nora.delivery.deliver", return_value=True) as deliver, \
            mock.patch("nora.calls.quiet_now", return_value=False), \
            mock.patch("nora.calls.notify_laptop") as laptop:
        cr._tell("Leave by 8:24", kind="leave", say_at_home=True)
    assert spoken == ["Leave by 8:24"]
    deliver.assert_called_once_with("Leave by 8:24", device="d_phone", kind="leave")
    laptop.assert_not_called()


def test_tell_falls_back_to_the_laptop_notification_when_nobody_hears_it():
    triggers.record_crossing(triggers.Event("d", "geofence.transition",
                                            {"place": "home", "transition": "exit"}, time.time()))
    with mock.patch.object(cr, "_speak", mock.Mock()) as speak, \
            mock.patch("nora.delivery.connected", return_value=[]), \
            mock.patch("nora.calls.notify_laptop") as laptop:
        cr._tell("Leave by 8:24", kind="leave", say_at_home=True)
    speak.assert_not_called()
    laptop.assert_called_once()


def test_a_route_is_asked_for_once_not_every_minute():
    lecture = ev("Lecture", datetime(2026, 11, 24, 9, 0), "college")
    with route(20 * 60) as r, mock.patch.object(cr, "_tell"):
        for minute in range(0, 19):
            cr.check_leave_by(datetime(2026, 11, 24, 8, minute), [lecture])
    assert r.call_count == 1

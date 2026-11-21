"""Device events and the rules they set off (Phase 8): where the user is, and
what leaving and arriving home do."""
from __future__ import annotations

import time
from datetime import date, timedelta
from unittest import mock

import pytest

from nora import places, store, task_ledger, triggers

CFG = {"enabled": True, "stale_after_sec": 600, "flap_sec": 180, "geofence_radius_m": 150,
       "home": {"place": "home", "leave_lock_laptop": True, "leave_pause_media": True,
                "leave_checklist": True, "low_battery_pct": 30, "arrive_brief": True,
                "arrive_resume_music": True}}


@pytest.fixture(autouse=True)
def fresh():
    store.reset_for_tests()
    triggers.reset_for_tests()
    with mock.patch.object(triggers, "_cfg", return_value=CFG):
        yield
    store.reset_for_tests()


def crossing(transition: str, ago: float = 0, place: str = "home", device: str = "d_phone"):
    return triggers.Event(device, "geofence.transition", {"place": place, "transition": transition},
                          time.time() - ago)


# ── where the user is ────────────────────────────────────────────────────────

def test_the_first_crossing_counts_and_a_repeat_does_not():
    first = triggers.record_crossing(crossing("exit"))
    assert (first.place, first.state, first.act, first.since) == ("home", "outside", True, None)
    assert triggers.record_crossing(crossing("exit")) is None
    assert triggers.where("Home") == "outside"


def test_a_late_crossing_updates_the_state_but_is_not_acted_on():
    c = triggers.record_crossing(crossing("exit", ago=3600))
    assert c.state == "outside" and not c.act


def test_an_older_crossing_than_the_one_known_is_ignored():
    triggers.record_crossing(crossing("exit", ago=60))
    assert triggers.record_crossing(crossing("enter", ago=120)) is None
    assert triggers.where("home") == "outside"


def test_coming_straight_back_is_drift_not_a_trip():
    triggers.record_crossing(crossing("exit", ago=90))
    back = triggers.record_crossing(crossing("enter", ago=30))
    assert back.state == "inside" and not back.act


def test_arriving_knows_how_long_you_were_out():
    triggers.record_crossing(crossing("exit", ago=2 * 3600))
    back = triggers.record_crossing(crossing("enter"))
    assert back.act and back.since == pytest.approx(time.time() - 2 * 3600, abs=5)


def test_nonsense_events_change_nothing():
    assert triggers.record_crossing(crossing("dwell")) is None
    assert triggers.record_crossing(crossing("exit", place="")) is None


# ── fences ───────────────────────────────────────────────────────────────────

def test_fences_are_the_saved_places_with_coordinates():
    with mock.patch.object(triggers, "sync_all"):
        places.save("home", lat=12.97, lon=77.59)
        places.save("college", address="RV University")       # no coordinates yet
    assert triggers.fences() == [{"id": "home", "lat": 12.97, "lon": 77.59, "radius_m": 150.0}]


def test_saving_or_forgetting_a_place_refences_the_phone():
    with mock.patch.object(triggers, "sync_all") as sync:
        places.save("home", lat=12.97, lon=77.59)
        places.forget("home")
        places.forget("nowhere")
    assert sync.call_count == 2


# ── leaving home ─────────────────────────────────────────────────────────────

@pytest.fixture
def laptop():
    """The laptop's side effects, recorded instead of done."""
    with mock.patch("nora.commands.system_control.lock_screen", return_value="Screen locked.") as lock, \
         mock.patch("nora.commands.spotify.pause_music", return_value="Paused.") as pause, \
         mock.patch("nora.commands.spotify.resume_music", return_value="Resuming Everlong.") as resume, \
         mock.patch("nora.reversible.record_action") as undo, \
         mock.patch("nora.delivery.deliver") as deliver, \
         mock.patch("nora.missed_calls.briefing_line", return_value="Mama called at 6 PM.") as missed, \
         mock.patch("nora.calls.quiet_now", return_value=False) as quiet:
        yield mock.Mock(lock=lock, pause=pause, resume=resume, undo=undo, deliver=deliver,
                        missed=missed, quiet=quiet)


def sent(laptop) -> str:
    laptop.deliver.assert_called_once()
    return laptop.deliver.call_args.args[0]


def test_leaving_home_locks_the_laptop_pauses_music_and_checks_the_phone(laptop):
    task_ledger.create_task("Submit the assignment", due_on=date.today())
    with mock.patch.object(triggers, "_spotify_playing", return_value=True), \
         mock.patch.object(triggers, "_battery", return_value={"battery_percent": 22, "charging": False}):
        triggers.handle(crossing("exit"))
    laptop.lock.assert_called_once()
    laptop.pause.assert_called_once()
    assert laptop.undo.call_args.args[:3] == ("pause_music", {}, "resume_music")
    text = sent(laptop)
    assert "Locked the laptop and paused Spotify." in text
    assert "on 22%" in text and "Due today: Submit the assignment." in text


def test_a_charging_or_full_phone_and_silent_music_leave_less_to_say(laptop):
    with mock.patch.object(triggers, "_spotify_playing", return_value=False), \
         mock.patch.object(triggers, "_battery", return_value={"battery_percent": 22, "charging": True}):
        triggers.handle(crossing("exit"))
    laptop.pause.assert_not_called()
    assert sent(laptop) == "You've left home. Locked the laptop."


def test_switched_off_parts_are_left_alone(laptop):
    cfg = {**CFG, "home": {**CFG["home"], "leave_lock_laptop": False, "leave_checklist": False}}
    with mock.patch.object(triggers, "_cfg", return_value=cfg), \
         mock.patch.object(triggers, "_spotify_playing", return_value=False):
        triggers.handle(crossing("exit"))
    laptop.lock.assert_not_called()
    assert sent(laptop) == "You've left home."


def test_a_late_exit_does_nothing(laptop):
    triggers.handle(crossing("exit", ago=3600))
    laptop.lock.assert_not_called()
    laptop.deliver.assert_not_called()


def test_other_places_set_off_no_home_rules(laptop):
    triggers.handle(crossing("exit", place="college"))
    laptop.lock.assert_not_called()
    assert triggers.where("college") == "outside"


# ── arriving home ────────────────────────────────────────────────────────────

def test_arriving_home_says_welcome_with_what_was_missed_and_resumes_music(laptop):
    task_ledger.create_task("Lab record", due_on=date.today() + timedelta(days=1))
    triggers.record_crossing(crossing("exit", ago=2 * 3600 + 600))
    speak = mock.Mock()
    triggers.start(speak=speak)
    triggers.stop()
    with mock.patch.object(triggers, "_spotify_playing", return_value=False):
        triggers.handle(crossing("enter"))
    said = speak.call_args.args[0]
    assert said.startswith("Welcome home. You were out 2 hours 10 minutes.")
    assert "Mama called at 6 PM." in said and "Due tomorrow: Lab record." in said
    assert "music" not in said                        # spoken before the music starts
    laptop.resume.assert_called_once()
    assert laptop.undo.call_args.args[:3] == ("resume_music", {}, "pause_music")
    assert sent(laptop).endswith("Say undo to stop it.")


def test_quiet_hours_keep_the_laptop_silent_and_the_music_off(laptop):
    laptop.quiet.return_value = True
    speak = mock.Mock()
    triggers.start(speak=speak)
    triggers.stop()
    triggers.handle(crossing("enter"))
    speak.assert_not_called()
    laptop.resume.assert_not_called()
    assert sent(laptop).startswith("Welcome home.")


def test_music_already_playing_is_left_alone(laptop):
    with mock.patch.object(triggers, "_spotify_playing", return_value=True):
        triggers.handle(crossing("enter"))
    laptop.resume.assert_not_called()


# ── the queue ────────────────────────────────────────────────────────────────

def test_emit_queues_with_the_devices_time_and_never_a_future_one():
    triggers.emit("d_phone", "geofence.transition", {"place": "home", "transition": "exit"}, time.time() - 30)
    triggers.emit("d_phone", "device.status", {}, time.time() + 3600)
    a, b = triggers._queue.get_nowait(), triggers._queue.get_nowait()
    assert a.at == pytest.approx(time.time() - 30, abs=2)
    assert b.at == pytest.approx(time.time(), abs=2)


# ── locking the laptop ───────────────────────────────────────────────────────

def test_lock_screen_asks_clavis_first_then_logind():
    from nora.commands import system_control
    ran = []

    def run(cmd, **_):
        ran.append(cmd[0].rsplit("/", 1)[-1])
        return mock.Mock(returncode=1 if cmd[0].endswith("qs") else 0)
    with mock.patch("sys.platform", "linux"), mock.patch("subprocess.run", side_effect=run):
        assert system_control.lock_screen() == "Screen locked."
    assert ran == ["qs", "loginctl"]

"""Routines (NORA_ASSIST_PLAN C)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from nora import assist, calls, fast_path, routines, store


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    with store.transaction() as conn:
        for t in ("routines", "routine_log", "nudges"):
            conn.execute(f"DELETE FROM {t}")
    told: list[str] = []
    monkeypatch.setattr(assist, "tell", lambda text, **kw: told.append(text) or True)
    monkeypatch.setattr(calls, "quiet_now", lambda now=None: False)
    return told


NOW = datetime(2026, 11, 24, 9, 0)


@pytest.mark.parametrize("a,b", [("fed Milo", "has Milo been fed"), ("fed Milo", "feed Milo"),
                                 ("walked the dog", "walk dog"), ("took my meds", "take meds")])
def test_log_and_question_meet(a, b):
    assert routines.key(a) == routines.key(b)


def test_answer_today_and_not_today():
    assert "don't have it logged" in routines.answer("feed Milo", NOW)
    routines.log("fed Milo", at=(NOW - timedelta(hours=1)).timestamp())
    assert routines.answer("did I feed Milo", NOW).startswith("Yes, you fed Milo at 8 am")
    assert routines.answer("feed Milo", NOW + timedelta(days=1)).startswith("Not today")


def test_parse_times():
    assert routines.parse_times("9 am and 9 pm") == [(9, 0), (21, 0)]
    assert routines.parse_times("8:30am, 7pm") == [(8, 30), (19, 0)]
    assert routines.parse_times("morning and evening") == [(8, 0), (19, 0)]


def test_missed_slot_nudged_once(_clean):
    told = _clean
    routines.add("feed Milo", "8 am and 8 pm", grace_min=45)
    routines.check(NOW.replace(hour=8, minute=30))           # inside grace
    assert told == []
    routines.check(NOW)                                       # 9:00, past grace
    routines.check(NOW + timedelta(minutes=10))
    assert len(told) == 1 and "feed milo" in told[0]


def test_logged_slot_not_nudged(_clean):
    told = _clean
    routines.add("feed Milo", "8 am")
    routines.log("I fed Milo", at=NOW.replace(hour=7).timestamp())   # early still counts
    routines.check(NOW)
    assert told == []


def test_vocabulary_has_the_pet():
    routines.add("feed Milo", "8 am")
    assert "Milo" in routines.vocabulary()


@pytest.mark.parametrize("said,action", [
    ("I fed Milo", "log_routine"), ("Milo has been fed", "log_routine"),
    ("did I feed Milo today", "last_routine"), ("has Milo been fed", "last_routine"),
    ("remind me if I haven't fed Milo by 9 am and 9 pm", "add_routine"),
])
def test_fast_path(said, action):
    assert fast_path.resolve(said).steps[0].action == action


def test_took_the_bus_is_not_a_routine():
    r = fast_path.resolve("I took the bus")
    assert r is None or r.steps[0].action != "log_routine"

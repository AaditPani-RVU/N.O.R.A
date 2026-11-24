"""Follow-through ladder and nightly check-in (NORA_ASSIST_PLAN B)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from nora import assist, calls, fast_path, follow_through, store, task_ledger


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    with store.transaction() as conn:
        conn.execute("DELETE FROM tasks")
        conn.execute("DELETE FROM nudges")
    told: list[tuple[str, str]] = []
    monkeypatch.setattr(assist, "tell", lambda text, kind="reminder", say_at_home=False:
                        told.append((kind, text)) or True)
    monkeypatch.setattr(calls, "quiet_now", lambda now=None: False)
    rang: list[str] = []
    monkeypatch.setattr(calls, "ring", lambda reason, **kw: rang.append(reason) or True)
    return told, rang


NOW = datetime(2026, 11, 24, 11, 0)


def _task(title: str, days_late: int) -> str:
    return task_ledger.create_task(title, due_on=NOW.date() - timedelta(days=days_late))


def test_one_nudge_a_day_naming_every_late_task(_clean):
    told, _ = _clean
    _task("Submit the assignment", 3)
    _task("Pay the rent", 1)
    text = follow_through.nudge(NOW)
    assert "Submit the assignment (3 days late)" in text and "Pay the rent (1 day late)" in text
    assert follow_through.nudge(NOW + timedelta(hours=2)) is None        # same day
    assert follow_through.nudge(NOW + timedelta(days=1)) is not None     # next day
    assert len(told) == 2


def test_not_before_nudge_from(_clean):
    _task("Submit the assignment", 3)
    assert follow_through.nudge(NOW.replace(hour=8)) is None


def test_due_today_is_not_late(_clean):
    task_ledger.create_task("Study", due_on=NOW.date())
    assert follow_through.nudge(NOW) is None


def test_rings_only_after_enough_days_and_nudges(_clean):
    _, rang = _clean
    _task("Submit the assignment", 9)
    assert follow_through.ring(NOW) is None                 # never nudged yet
    for d in range(3):
        follow_through.nudge(NOW + timedelta(days=d))
    assert follow_through.ring(NOW + timedelta(days=2)) == "Submit the assignment is 11 days late"
    assert follow_through.ring(NOW + timedelta(days=2, hours=1)) is None   # every 3 days at most
    assert len(rang) == 1


def test_checkin_once_a_night_with_tomorrow_and_late(_clean):
    told, _ = _clean
    task_ledger.create_task("CV project review", due_on=NOW.date() + timedelta(days=1))
    _task("Submit the assignment", 4)
    night = NOW.replace(hour=21, minute=5)
    text = follow_through.checkin(night)
    assert "Due tomorrow: CV project review" in text
    assert "Submit the assignment (4 days late)" in text
    assert follow_through.checkin(night + timedelta(minutes=30)) is None
    assert told[-1][0] == "checkin"


def test_quiet_night_says_nothing(_clean):
    told, _ = _clean
    assert follow_through.checkin(NOW.replace(hour=21, minute=5)) is None
    assert told == []


def test_push_and_drop_commands():
    from nora.commands import task_commands
    t = task_ledger.create_task("Submit the assignment", due_on=date.today() - timedelta(days=2))
    assert "Moved Submit the assignment" in task_commands.push_task("the assignment", "friday")
    assert task_ledger.get_task(t)["due_on"] > date.today().isoformat()
    assert task_commands.drop_task("it") == "Dropped Submit the assignment."
    assert task_ledger.get_task(t)["status"] == task_ledger.STATUS_ABANDONED


@pytest.mark.parametrize("said,action,params", [
    ("push the assignment to friday", "push_task", {"query": "the assignment", "due": "friday"}),
    ("push it", "push_task", {"query": "it", "due": "tomorrow"}),
    ("drop it", "drop_task", {"query": "it"}),
])
def test_fast_path(said, action, params):
    r = fast_path.resolve(said)
    assert r.steps[0].action == action and r.steps[0].parameters == params

"""Missed calls, in context (NORA_CALLS_PLAN §3.2): what the phone reports,
how it's said beside the calendar, the line after a meeting, and "call back"."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from unittest import mock

import pytest

from nora import command_engine, fast_path, missed_calls, screened_calls, store
from nora.commands import screened_calls as cmds
from nora.schemas import StepResult


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    store.reset_for_tests()
    missed_calls.reset_for_tests()
    monkeypatch.setattr(missed_calls, "_STATE_PATH", tmp_path / "missed.json")
    monkeypatch.setattr(missed_calls, "_events_around", lambda since, until: [])
    yield
    store.reset_for_tests()
    missed_calls.reset_for_tests()


@pytest.fixture
def phone():
    for name in ("phone.missed_calls", "phone.call_contact", "phone.block_number"):
        command_engine.register_device_capability(
            name, lambda **_: None, device="d_phone", sig=f"{name}()", description=name, risk="low")
    yield
    command_engine.unregister_device("d_phone")


def _phone_says(*items: dict):
    """Patch the hub: phone.missed_calls answers `items`; other calls are recorded."""
    placed = []

    async def call_capability(name, params=None):
        if name == "phone.missed_calls":
            return StepResult(action=name, success=True, untrusted=True, data={"items": list(items)})
        placed.append((name, params))
        return StepResult(action=name, success=True, message="Calling.")
    return mock.patch("nora.hub.server.call_capability", call_capability), placed


def _item(caller, minutes_ago, count=1, first=None, on_screen=True):
    return {"caller": caller, "count": count, "minutes_ago": minutes_ago,
            "first_minutes_ago": first if first is not None else minutes_ago, "on_screen": on_screen}


def _event(summary, start: datetime, end: datetime, id_="e1"):
    return {"id": id_, "summary": summary, "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()}}


def test_parse_keeps_order_and_bounds_names():
    now = time.time()
    calls = missed_calls._parse({"items": [_item("Dad", 90), _item("Mom", 10, count=2, first=30),
                                           "junk", {"caller": "x", "minutes_ago": "soon"}]}, now)
    assert [c.caller for c in calls] == ["Mom", "Dad"]
    assert calls[0].count == 2 and calls[0].first_at == pytest.approx(now - 1800)


def test_numbers_are_told_apart_from_names():
    assert missed_calls.Missed("+91 98450 12345", 1, 0, 0).number == "+91 98450 12345"
    assert missed_calls.Missed("Mom", 1, 0, 0).number == ""
    assert missed_calls.Missed("Room 101", 1, 0, 0).number == ""
    assert missed_calls.callback_params(missed_calls.Missed("Mom", 1, 0, 0)) == {"name": "Mom"}


def test_describe_puts_calls_in_their_meeting():
    now = datetime(2026, 10, 23, 16, 0)
    ev = _event("Design review", datetime(2026, 10, 23, 15, 0), datetime(2026, 10, 23, 15, 45))
    mom = missed_calls.Missed("Mom", 2, datetime(2026, 10, 23, 15, 10).timestamp(),
                              datetime(2026, 10, 23, 15, 25).timestamp())
    dad = missed_calls.Missed("Dad", 1, datetime(2026, 10, 22, 21, 5).timestamp(),
                              datetime(2026, 10, 22, 21, 5).timestamp())
    assert missed_calls.describe([mom, dad], [ev], now) == (
        "Mom called twice while you were in Design review; Dad called, yesterday at 9:05 PM.")
    three = missed_calls.Missed("Priya", 3, mom.first_at, datetime(2026, 10, 23, 15, 50).timestamp())
    assert missed_calls.describe([three], [ev], now) == "Priya called 3 times, last at 3:50 PM."


def test_who_called_lists_the_phone_and_offers_to_call_back(phone):
    patch, _ = _phone_says(_item("Mom", 12, count=2, first=20))
    with patch:
        said, untrusted = asyncio.run(missed_calls.who_called(24))
    assert said.startswith("Mom called twice, last at") and "Say 'call back'" in said
    assert untrusted
    assert missed_calls.mentioned().caller == "Mom"


def test_who_called_adds_blocked_numbers_once(phone):
    with mock.patch("time.time", return_value=time.time() - 60):
        screened_calls.record("d_phone", {"number": "+91 98450 12345", "action": "rang"})
        screened_calls.record("d_phone", {"number": "+91 99999 00000", "action": "rejected"})
    patch, _ = _phone_says(_item("098450 12345", 1))
    with patch:
        said, _ = asyncio.run(missed_calls.who_called(24))
    assert "098450 12345 called" in said and said.count("98450") == 1
    assert said.endswith("One blocked number called too.")


def test_who_called_with_nothing_missed(phone):
    patch, _ = _phone_says()
    with patch:
        assert asyncio.run(missed_calls.who_called(24)) == ("No missed calls in the last 24 hours.", True)


def test_without_the_phone_it_falls_back_to_screened_calls():
    said, untrusted = asyncio.run(missed_calls.who_called(24))
    assert "phone isn't connected" in said and not untrusted


def test_phrasings_reach_who_called_and_call_back(phone):
    for text in ("did anyone call", "Did anybody call me while I was out?", "any missed calls",
                 "do I have any missed calls?", "did I miss any calls", "missed calls"):
        r = fast_path.resolve(text)
        assert r is not None and r.steps[0].action == "who_called", text
    for text in ("call her back", "call them back", "okay, call back", "return the call", "ring him back"):
        r = fast_path.resolve(text)
        assert r is not None and r.steps[0].action == "call_back", text
    for text in ("call me back in 10 minutes", "call that number back"):
        r = fast_path.resolve(text)
        assert r is None or not r.steps or r.steps[0].action != "call_back", text


def test_call_back_calls_who_was_mentioned(phone):
    missed_calls.remember(missed_calls.Missed("Mom", 2, 0, 0))
    patch, placed = _phone_says(_item("Dad", 1))
    with patch:
        asyncio.run(cmds.call_back())
    assert placed == [("phone.call_contact", {"name": "Mom"})]


def test_call_back_without_a_mention_takes_the_newest_caller(phone):
    patch, placed = _phone_says(_item("+91 98450 12345", 3), _item("Dad", 30))
    with patch:
        asyncio.run(cmds.call_back())
    assert placed == [("phone.call_contact", {"number": "+91 98450 12345"})]


def test_call_back_with_no_one_says_so(phone):
    patch, placed = _phone_says()
    with patch:
        r = asyncio.run(cmds.call_back())
    assert placed == [] and "don't know" in r.message


def test_after_a_meeting_the_phone_hears_who_called(phone, monkeypatch):
    now = datetime(2026, 10, 23, 15, 50).timestamp()
    ev = _event("Design review", datetime(2026, 10, 23, 15, 0), datetime(2026, 10, 23, 15, 45))
    monkeypatch.setattr(missed_calls, "_events_around", lambda since, until: [ev])
    monkeypatch.setattr("nora.calls.quiet_now", lambda: False)
    calls = [missed_calls.Missed("Mom", 2, now - 35 * 60, now - 25 * 60),
             missed_calls.Missed("Dad", 1, now - 2 * 60, now - 2 * 60)]       # after it ended
    monkeypatch.setattr(missed_calls, "fetch_sync", lambda hours, include_seen: calls)
    sent = []
    monkeypatch.setattr("nora.delivery.broadcast", lambda text, kind: sent.append(text) or ["d_phone"])
    assert missed_calls.check_meetings(now) == 1
    assert sent == ["Mom called twice while you were in Design review. Say 'call back' to return it."]
    assert missed_calls.mentioned().caller == "Mom"
    assert missed_calls.check_meetings(now + 60) == 0          # once per event


def test_after_a_meeting_nothing_is_said_without_calls_or_in_quiet_hours(phone, monkeypatch):
    now = datetime(2026, 10, 23, 15, 50).timestamp()
    ev = _event("Standup", datetime(2026, 10, 23, 15, 0), datetime(2026, 10, 23, 15, 45))
    monkeypatch.setattr(missed_calls, "_events_around", lambda since, until: [ev])
    monkeypatch.setattr("nora.calls.quiet_now", lambda: True)
    assert missed_calls.check_meetings(now) == 0
    monkeypatch.setattr("nora.calls.quiet_now", lambda: False)
    monkeypatch.setattr(missed_calls, "fetch_sync", lambda hours, include_seen: [])
    assert missed_calls.check_meetings(now) == 0


def test_after_meeting_line_names_up_to_three():
    span = (1000.0, 5000.0)
    calls = [missed_calls.Missed(n, 1, 2000, 2000) for n in ("A", "B", "C", "D")]
    line = missed_calls.after_meeting_line(calls, {"summary": "Sync"}, span)
    assert line == "A called, B called, C called and 1 more while you were in Sync."
    assert missed_calls.after_meeting_line([missed_calls.Missed("A", 1, 2000, 2000, on_screen=False)],
                                           {"summary": "Sync"}, span) == ""


def test_briefing_uses_the_phone_then_falls_back_to_screening(phone, monkeypatch):
    monkeypatch.setattr(missed_calls, "fetch_sync", lambda hours, include_seen: [
        missed_calls.Missed("Mom", 1, time.time() - 3600, time.time() - 3600)])
    assert missed_calls.briefing_line().startswith("Mom called, ")
    monkeypatch.setattr(missed_calls, "fetch_sync", lambda hours, include_seen: [])
    with mock.patch("time.time", return_value=time.time() - 60):
        screened_calls.record("d_phone", {"number": "+91 98450 12345", "action": "silenced"})
    assert missed_calls.briefing_line().startswith("One unknown number called")

"""Unknown callers the phone screened (NORA_CALLS_PLAN §3.1): the record on
the core, how it's said, and the requests that act on "that number"."""
from __future__ import annotations

import time
from datetime import datetime
from unittest import mock

import pytest

from nora import command_engine, fast_path, screened_calls, store


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset_for_tests()
    yield
    store.reset_for_tests()


@pytest.fixture
def phone():
    """A connected phone offering what the rules reach for."""
    for name in ("phone.block_number", "phone.call_contact"):
        command_engine.register_device_capability(
            name, lambda **_: None, device="d_phone", sig=f"{name}(number: str)", description=name, risk="low")
    yield
    command_engine.unregister_device("d_phone")


def _screened(number: str, action: str, ago_min: float = 0) -> None:
    with mock.patch("time.time", return_value=time.time() - ago_min * 60):
        screened_calls.record("d_phone", {"number": number, "action": action})


def test_records_newest_first_and_ignores_unknown_actions():
    _screened("+91 98450 12345", "rang", ago_min=30)
    _screened("+91 80 2222 3333", "silenced", ago_min=5)
    screened_calls.record("d_phone", {"number": "+1 555", "action": "answered"})
    assert [(c.number, c.action) for c in screened_calls.recent()] == [
        ("+91 80 2222 3333", "silenced"), ("+91 98450 12345", "rang")]


def test_last_number_skips_withheld_callers():
    _screened("+91 98450 12345", "rang", ago_min=10)
    _screened("", "silenced", ago_min=1)
    assert screened_calls.last_number() == "+91 98450 12345"


def test_old_calls_age_out_of_the_window():
    _screened("+91 98450 12345", "rang", ago_min=25 * 60)
    assert screened_calls.recent(24) == []
    assert len(screened_calls.recent(48)) == 1


def test_describe_says_who_when_and_what_happened():
    now = datetime(2026, 10, 18, 9, 0)
    calls = [screened_calls.Screened("+91 80 2222 3333", "silenced", datetime(2026, 10, 18, 8, 12).timestamp()),
             screened_calls.Screened("", "rang", datetime(2026, 10, 17, 22, 5).timestamp())]
    said = screened_calls.describe(calls, 24, now)
    assert said == ("Two unknown numbers called in the last 24 hours: +91 80 2222 3333 at 8:12 AM, "
                    "silenced; a withheld number yesterday at 10:05 PM.")


def test_describe_with_nothing_says_contacts_arent_tracked():
    assert "contacts aren't tracked" in screened_calls.describe([], 24)


def test_briefing_line_counts_what_got_through_or_was_silenced():
    assert screened_calls.briefing_line() == ""
    _screened("+91 98450 12345", "silenced", ago_min=60)
    _screened("+91 11 4444 5555", "rang", ago_min=120)
    _screened("+91 99999 00000", "rejected", ago_min=30)     # blocked: not news
    assert screened_calls.briefing_line().startswith("Two unknown numbers called in the last 12 hours.")


def test_who_called_is_answered_without_a_model():
    for text in ("who called", "Who called me while I was out?", "who rang today", "who called last night"):
        r = fast_path.resolve(text)
        assert r is not None and r.steps[0].action == "who_called", text


def test_block_and_call_back_reach_the_last_number(phone):
    _screened("+91 98450 12345", "rang")
    r = fast_path.resolve("block the last number that called")
    assert (r.steps[0].action, r.steps[0].parameters) == ("phone.block_number", {"number": "+91 98450 12345"})
    r = fast_path.resolve("call that number back")
    assert (r.steps[0].action, r.steps[0].parameters) == ("phone.call_contact", {"number": "+91 98450 12345"})


def test_that_number_with_no_caller_says_so(phone):
    r = fast_path.resolve("block that number")
    assert not r.steps and "No unknown number" in r.response


def test_without_the_phone_it_says_the_phone_isnt_there():
    _screened("+91 98450 12345", "rang")
    r = fast_path.resolve("call the last caller back")
    assert not r.steps and "phone isn't connected" in r.response


def test_ordinary_calls_and_blocks_are_left_alone():
    for text in ("call mom", "call me back in 10 minutes", "block distracting websites"):
        r = fast_path.resolve(text)
        assert r is None or r.steps[0].action not in ("phone.block_number", "who_called"), text


def test_the_hub_records_a_screened_call_event():
    from nora.hub.server import Hub
    hub, session = Hub(), mock.Mock(device_id="d_phone")
    hub._dispatch(session, {"type": "event", "id": "e1", "body": {"name": "call.screened",
                                                      "data": {"number": "+91 98450 12345", "action": "silenced"}}})
    assert [(c.number, c.action) for c in screened_calls.recent()] == [("+91 98450 12345", "silenced")]

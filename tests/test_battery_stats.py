"""Phase 10: idle battery drain from the phone's samples."""
from __future__ import annotations

import time

import pytest


from nora import battery_stats as bs
from nora import store, triggers

H = 3600.0
T0 = time.time() - 4 * 86400   # inside the 30 days kept


@pytest.fixture(autouse=True)
def fresh():
    store.reset_for_tests()
    yield
    store.reset_for_tests()


def sample(at_h: float, level: int, charging=False, screen_on=False, device="d_phone"):
    bs.record(device, {"level": level, "charging": charging, "screen_on": screen_on}, T0 + at_h * H)


def test_idle_drain_counts_only_screen_off_unplugged_time():
    sample(0, 90)                       # screen off overnight: 8 h, 2 %
    sample(8, 88, screen_on=True)       # woke up, used it for an hour: 10 %
    sample(9, 78)
    sample(17, 76)                      # off again: 8 h, 2 %
    d = bs.drain(now=T0 + 18 * H)
    assert d.idle_hours == 16 and d.idle_pct_per_day == 6.0
    assert d.unplugged_hours == 17 and d.unplugged_pct_per_day == pytest.approx(19.8, abs=0.1)
    assert "6% a day while idle" in bs.spoken(d) and "over the 3% target" in bs.spoken(d)


def test_charging_and_gaps_are_left_out():
    sample(0, 50)
    sample(1, 60, charging=True)        # level went up: not drain
    sample(2, 100, charging=True)
    sample(3, 100)
    sample(60, 90)                      # 57 h gap: the phone was off
    d = bs.drain(now=T0 + 61 * H)
    assert d.idle_hours == 0 and d.idle_pct_per_day is None


def test_too_little_idle_time_says_so():
    sample(0, 90)
    sample(2, 89)
    assert "not enough to say" in bs.spoken(bs.drain(now=T0 + 3 * H))


def test_samples_arrive_as_events_through_the_trigger_rules():
    triggers.handle(triggers.Event("d_phone", "battery.sample",
                                   {"level": 77, "charging": False, "screen_on": False}, T0))
    triggers.handle(triggers.Event("d_phone", "battery.sample", {"level": "lots"}, T0 + 1))
    rows = store.query("SELECT level FROM battery_samples")
    assert [r["level"] for r in rows] == [77]

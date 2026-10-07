"""Sharp B: tomorrow's forecast, and the caches the prefetcher keeps warm."""
from __future__ import annotations

from datetime import date, timedelta
from unittest import mock

import pytest

from nora.commands import google_services as g
from nora.commands import weather as w

HERE = {"name": "here", "country": "", "lat": 12.97, "lon": 77.59}


def _payload(days: int = 16) -> dict:
    return {
        "current": {"temperature_2m": 22.4, "apparent_temperature": 22.0, "weather_code": 3,
                    "wind_speed_10m": 2.0},
        "daily": {"temperature_2m_max": [28 + i for i in range(days)],
                  "temperature_2m_min": [20 + i for i in range(days)],
                  "precipitation_probability_max": [10, 60] + [0] * (days - 2),
                  "weather_code": [3, 61] + [0] * (days - 2)},
    }


@pytest.fixture
def forecast(monkeypatch):
    w._FORECAST.clear()
    monkeypatch.setattr(w, "_resolve", lambda location: HERE)
    resp = mock.Mock(**{"json.return_value": _payload(), "raise_for_status.return_value": None})
    with mock.patch.object(w.requests, "get", return_value=resp) as get:
        yield get
    w._FORECAST.clear()


def test_tomorrow_is_tomorrows_forecast_not_todays(forecast):
    out = w.get_weather("", "tomorrow")
    assert out.startswith("Tomorrow") and "high of 29" in out and "rain 60 percent" in out
    assert "22 degrees" not in out                       # no "right now" for a later day


def test_today_still_reads_the_current_weather(forecast):
    out = w.get_weather()
    assert "22 degrees" in out and "High of 28" in out


def test_one_fetch_serves_today_tomorrow_and_a_weekday_for_a_while(forecast):
    w.get_weather()
    w.get_weather("", "tomorrow")
    w.get_weather("", (date.today() + timedelta(days=3)).strftime("%A").lower())
    assert forecast.call_count == 1
    assert forecast.call_args.kwargs["params"]["forecast_days"] == 16


def test_days_it_cannot_answer(forecast):
    assert "two weeks" in w.get_weather("", (date.today() + timedelta(days=30)).isoformat())
    assert "which day" in w.get_weather("", "the day the music died")


def test_prefetch_refreshes_the_local_forecast(forecast):
    w.get_weather()
    w.prefetch()
    assert forecast.call_count == 2                      # a refresh, not a cache hit


def test_calendar_reads_are_cached_and_dropped_when_nora_changes_it(monkeypatch):
    g._calendar_changed()
    svc = mock.MagicMock()
    svc.events.return_value.list.return_value.execute.return_value = {
        "items": [{"summary": "Standup", "start": {"dateTime": "2026-10-08T09:30:00+05:30"}}]}
    monkeypatch.setattr(g, "_calendar_service", lambda: svc)

    assert "Standup" in g.check_calendar("today")
    assert "Standup" in g.check_calendar("today")
    assert svc.events.return_value.list.call_count == 1

    g.add_calendar_event("Dentist", "today", "5pm")
    g.check_calendar("today")
    assert svc.events.return_value.list.call_count == 2
    g._calendar_changed()

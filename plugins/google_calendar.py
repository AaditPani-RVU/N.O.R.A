"""NORA plugin: extended Google Calendar voice commands.

Uses the same direct Google API auth as google_services.py — no CLI dependency.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from nora.command_engine import register

logger = logging.getLogger("nora.plugins.google_calendar")


def _svc():
    from nora.commands.google_services import _calendar_service
    return _calendar_service()


def _fmt(ev: dict) -> str:
    from nora.commands.google_services import _fmt_event_time
    return f"{ev.get('summary', 'Untitled')} at {_fmt_event_time(ev)}"


@register("calendar_week", sig="calendar_week()",
          description="List calendar events for this week", category="notification")
def calendar_week() -> str:
    try:
        local_tz = datetime.now().astimezone().tzinfo
        now = datetime.now(tz=local_tz)
        week_start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        week_end = week_start + timedelta(days=7)

        result = _svc().events().list(
            calendarId="primary",
            timeMin=week_start.isoformat(),
            timeMax=week_end.isoformat(),
            maxResults=30,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = result.get("items", [])

        if not events:
            return "Nothing on your calendar this week."

        by_day: dict[str, list[str]] = {}
        for ev in events:
            start = ev["start"].get("dateTime", ev["start"].get("date", ""))
            try:
                day = datetime.fromisoformat(start).strftime("%A")
            except Exception:
                day = "Unknown"
            by_day.setdefault(day, []).append(ev.get("summary", "Untitled"))

        parts = [f"{day}: {', '.join(items)}" for day, items in by_day.items()]
        return "This week — " + ". ".join(parts) + "."
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        logger.error("calendar_week failed: %s", e)
        return "I couldn't fetch this week's calendar."


@register("calendar_next_event", sig="calendar_next_event()",
          description="When is my next calendar event", category="notification")
def calendar_next_event() -> str:
    try:
        local_tz = datetime.now().astimezone().tzinfo
        now = datetime.now(tz=local_tz)

        result = _svc().events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            maxResults=1,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = result.get("items", [])

        if not events:
            return "No upcoming events found."

        ev = events[0]
        start_str = ev["start"].get("dateTime", ev["start"].get("date", ""))
        dt = datetime.fromisoformat(start_str)
        delta = dt - now
        minutes = int(delta.total_seconds() / 60)
        if minutes < 60:
            when = f"in {minutes} minute{'s' if minutes != 1 else ''}"
        else:
            hours = minutes // 60
            when = f"in {hours} hour{'s' if hours != 1 else ''}"

        return f"Your next event is {ev.get('summary', 'Untitled')} at {dt.strftime('%-I:%M %p')}, {when}."
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        logger.error("calendar_next_event failed: %s", e)
        return "I couldn't find your next event."


@register("calendar_free_time", sig='calendar_free_time(day: str = "today")',
          description="Find free time slots on a given day", category="notification")
def calendar_free_time(day: str = "today") -> str:
    try:
        from nora.commands.google_services import _parse_date, _day_window
        dt = _parse_date(day)
        time_min, time_max = _day_window(dt)

        result = _svc().events().list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            maxResults=20,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = result.get("items", [])

        work_start = dt.replace(hour=8, minute=0)
        work_end = dt.replace(hour=20, minute=0)

        busy: list[tuple[datetime, datetime]] = []
        for ev in events:
            s = ev["start"].get("dateTime")
            e = ev["end"].get("dateTime")
            if s and e:
                busy.append((datetime.fromisoformat(s), datetime.fromisoformat(e)))

        free: list[str] = []
        cursor = work_start
        for start, end in sorted(busy):
            if cursor < start:
                free.append(
                    f"{cursor.strftime('%-I:%M %p')} to {start.strftime('%-I:%M %p')}"
                )
            cursor = max(cursor, end)
        if cursor < work_end:
            free.append(f"{cursor.strftime('%-I:%M %p')} to {work_end.strftime('%-I:%M %p')}")

        if not free:
            return f"You're fully booked {day} between 8 AM and 8 PM."
        return f"Free slots {day}: {', '.join(free)}."
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        logger.error("calendar_free_time failed: %s", e)
        return "I couldn't calculate your free time."


@register("calendar_tomorrow", sig="calendar_tomorrow()",
          description="What's on my calendar tomorrow", category="notification")
def calendar_tomorrow() -> str:
    try:
        from nora.commands.google_services import _parse_date, _day_window
        dt = _parse_date("tomorrow")
        time_min, time_max = _day_window(dt)

        result = _svc().events().list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            maxResults=15,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = result.get("items", [])

        if not events:
            return "Nothing on your calendar tomorrow."

        parts = [_fmt(ev) for ev in events]
        label = "event" if len(events) == 1 else "events"
        return f"Tomorrow you have {len(events)} {label}: {', '.join(parts)}."
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        logger.error("calendar_tomorrow failed: %s", e)
        return "I couldn't fetch tomorrow's calendar."

"""Calendar rules (distributed plan Phase 8c): leave-by alerts and the night-before alarm.

Two rules read the Google calendar on their own, on a one-minute tick:

**Leave by.** A timed event with a place in its `location` gets a travel
estimate from where the user is now (the saved place the phone's geofences
last put them inside) to where the event is (a saved place by name, or the
address geocoded near home). `heads_up_min` before the time to leave, the
phone gets "Leave by 8:25 for Lecture (9:00 at college): about 25 minutes by
car", and the laptop says it too when the user is at home. OSRM has no
traffic, so a drive is padded by `traffic_factor`, and `buffer_min` is left
to spare. Online meetings (a link, Zoom, Meet, Teams) have nowhere to go.
When the user is somewhere NORA doesn't know, there is no origin and no
alert: a guess from home would be wrong exactly when it matters.

**The night before.** At `night_before_at`, tomorrow's first timed event
decides the alarm: start, minus travel from home, minus `buffer_min`, minus
`ready_min` to get up and out. Only when that event starts before
`alarm_if_before`, so a free morning keeps the user's own alarm. The alarm
lives on the phone (`phone.wake_alarm`, Android's AlarmManager, rung like a
call from NORA on the alarm stream), so it goes off with the link down. The
phone also gets a note with tomorrow's plan and what's due; "undo" cancels
the alarm. Like the geofences, the core keeps the list and hands the whole
of it over on every connect, so a phone reboot heals on the next connect.

Each alert fires once per event (`calendar_alerts`, store migration 7),
including across restarts.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

from nora import store
from nora.config import get_config

logger = logging.getLogger("nora.calendar_rules")

WAKE_CAPABILITY = "phone.wake_alarm"

_DEFAULTS = {
    "enabled": True,
    "leave_by": True,
    "heads_up_min": 5,
    "buffer_min": 10,
    "traffic_factor": 1.3,
    "mode": "driving",
    "night_before": True,
    "night_before_at": "21:30",
    "night_before_until": "23:30",
    "ready_min": 60,
    "alarm_if_before": "11:00",
}

_ONLINE = re.compile(r"https?://|\bzoom\b|meet\.google|\bgoogle meet\b|\bteams\b|\bwebex\b|"
                     r"\bskype\b|\bdiscord\b|\bonline\b|\bvirtual\b", re.I)


def _cfg() -> dict:
    return {**_DEFAULTS, **(get_config().get("calendar_rules", {}) or {})}


def _hhmm(text: str, default: str) -> tuple[int, int]:
    try:
        h, m = (int(x) for x in str(text or default).strip().split(":"))
        return h, m
    except ValueError:
        h, m = (int(x) for x in default.split(":"))
        return h, m


def _clock(dt: datetime) -> str:
    return dt.strftime("%-I:%M %p").replace(":00 ", " ") if dt.minute == 0 else dt.strftime("%-I:%M %p")


# ── events ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CalEvent:
    key: str
    summary: str
    start: datetime          # local, naive
    location: str

    @property
    def online(self) -> bool:
        return not self.location.strip() or bool(_ONLINE.search(self.location))


def parse_events(raw: list[dict]) -> list[CalEvent]:
    """Google's event dicts → timed events, earliest first. All-day ones are dropped."""
    out = []
    for ev in raw:
        start = (ev.get("start") or {}).get("dateTime")
        if not start:
            continue
        try:
            at = datetime.fromisoformat(start).astimezone().replace(tzinfo=None)
        except ValueError:
            continue
        if (ev.get("status") or "") == "cancelled":
            continue
        out.append(CalEvent(f"{ev.get('id', '')}@{start}", str(ev.get("summary") or "Your event"),
                            at, str(ev.get("location") or "")))
    return sorted(out, key=lambda e: e.start)


def _fetch_day(day: datetime) -> list[CalEvent]:
    from nora.commands import google_services as gs
    if not gs._TOKEN_FILE.exists():
        return []
    return parse_events(gs._events_between(*gs._day_window(day)))


# ── where from, where to ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class Spot:
    name: str           # a saved place's name, or the address as written
    lat: float
    lon: float


def origin_now() -> Spot | None:
    """The saved place the user is in, by the phone's geofences. When no
    crossing has ever been seen, home (the core and the user's default);
    when the phone last left everywhere NORA knows, None."""
    from nora import places
    rows = store.query("SELECT place FROM place_presence WHERE state = 'inside' "
                       "ORDER BY changed_at DESC")
    for r in rows:
        p = places.get(r["place"])
        if p is not None and p.located:
            return Spot(p.name, p.lat, p.lon)
    if rows or store.query("SELECT 1 FROM place_presence LIMIT 1"):
        return None
    return home()


def home() -> Spot | None:
    from nora import places
    name = str(((get_config().get("triggers", {}) or {}).get("home", {}) or {}).get("place", "home"))
    p = places.get(name)
    return Spot(p.name, p.lat, p.lon) if p is not None and p.located else None


_GEO_TTL = 14 * 86400


def destination(location: str) -> Spot | None:
    """Where an event is: a saved place it names, else the address geocoded
    near home. Lookups (and misses) are kept for two weeks."""
    from nora import places
    loc = location.strip()
    if not loc:
        return None
    p = places.get(loc)
    if p is None:
        low = loc.lower()
        for cand in places.listing():
            if cand.located and re.search(rf"\b{re.escape(cand.name)}\b", low):
                p = cand
                break
    if p is not None and p.located:
        return Spot(p.name, p.lat, p.lon)
    key = re.sub(r"\s+", " ", loc.lower())[:300]
    rows = store.query("SELECT lat, lon, at FROM geocode_cache WHERE query = ?", (key,))
    if rows and time.time() - rows[0]["at"] < _GEO_TTL:
        r = rows[0]
        return Spot(loc, r["lat"], r["lon"]) if r["lat"] is not None else None
    h = home()
    hit = places.geocode(loc, near=(h.lat, h.lon) if h else None)
    if hit is None and h is not None:
        hit = places.geocode(loc)        # somewhere further than the city
    with store.transaction() as conn:
        conn.execute("INSERT INTO geocode_cache (query, lat, lon, at) VALUES (?,?,?,?) "
                     "ON CONFLICT(query) DO UPDATE SET lat=excluded.lat, lon=excluded.lon, at=excluded.at",
                     (key, hit[0] if hit else None, hit[1] if hit else None, time.time()))
    return Spot(loc, hit[0], hit[1]) if hit else None


@dataclass(frozen=True)
class Trip:
    seconds: float         # padded for traffic
    spoken: str            # "about 25 minutes by car"


def travel(origin: Spot, dest: Spot) -> Trip | None:
    """Door to door, or None when there is no route. Already there → zero."""
    from nora import places
    if places.straight_line_km((origin.lat, origin.lon), (dest.lat, dest.lon)) < 0.3:
        return Trip(0.0, "you're already there")
    cfg = _cfg()
    r = places.route((origin.lat, origin.lon), (dest.lat, dest.lon), str(cfg["mode"]))
    if r is None:
        return None
    factor = float(cfg["traffic_factor"]) if r.mode == "driving" else 1.0
    padded = r.seconds * max(1.0, factor)
    minutes = max(1, round(padded / 60))
    how = {"driving": "by car", "walking": "on foot", "bicycling": "by bike"}.get(r.mode, "")
    took = (f"{minutes} minutes" if minutes < 90 else
            f"{minutes // 60} hour{'s' if minutes >= 120 else ''}"
            + (f" {minutes % 60} minutes" if minutes % 60 else ""))
    tail = " with traffic" if r.mode == "driving" and factor > 1 else ""
    return Trip(padded, f"about {took} {how}{tail}".replace("  ", " "))


# ── once per event ───────────────────────────────────────────────────────────

def _done(key: str, kind: str) -> bool:
    return bool(store.query("SELECT 1 FROM calendar_alerts WHERE key = ? AND kind = ?", (key, kind)))


def _mark(key: str, kind: str) -> None:
    with store.transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO calendar_alerts (key, kind, at) VALUES (?,?,?)",
                     (key, kind, time.time()))
        conn.execute("DELETE FROM calendar_alerts WHERE at < ?", (time.time() - 14 * 86400,))


# ── leave by ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LeavePlan:
    event: CalEvent
    leave_at: datetime
    trip: Trip
    dest: Spot

    def text(self, now: datetime) -> str:
        where = self.dest.name
        when = f"{self.event.summary} ({_clock(self.event.start)} at {where})"
        if self.leave_at <= now:
            return f"Time to leave for {when}: {self.trip.spoken}."
        return f"Leave by {_clock(self.leave_at)} for {when}: {self.trip.spoken}."


def plan_leave(ev: CalEvent, origin: Spot, dest: Spot) -> LeavePlan | None:
    trip = travel(origin, dest)
    if trip is None or trip.seconds <= 0:
        return None
    buffer = timedelta(minutes=float(_cfg()["buffer_min"]))
    leave_at = ev.start - timedelta(seconds=trip.seconds) - buffer
    # To the minute below: "leave by 8:25", never 8:25:40.
    return LeavePlan(ev, leave_at.replace(second=0, microsecond=0), trip, dest)


def check_leave_by(now: datetime | None = None, events: list[CalEvent] | None = None) -> int:
    """Alert for every event whose time to leave is close. Returns how many."""
    cfg = _cfg()
    if not cfg["leave_by"]:
        return 0
    now = now or datetime.now()
    if events is None:
        events = _fetch_day(now)
        if (now + timedelta(hours=3)).date() != now.date():
            events += _fetch_day(now + timedelta(hours=3))
    # Nothing is more than a few hours' travel; skip the geocoding for the rest.
    soon = [e for e in events if now < e.start <= now + timedelta(hours=4)
            and not e.online and not _done(e.key, "leave")]
    if not soon:
        return 0
    origin = origin_now()
    if origin is None:
        logger.info("leave-by: not at a known place, so no travel time for %d event(s)", len(soon))
        return 0
    lead = timedelta(minutes=float(cfg["heads_up_min"]))
    count = 0
    for ev in soon:
        dest = destination(ev.location)
        if dest is None:
            logger.info("leave-by: can't place %r", ev.location[:60])
            _mark(ev.key, "leave")           # don't geocode it again every minute
            continue
        plan = plan_leave(ev, origin, dest)
        if plan is None:
            _mark(ev.key, "leave")           # there already, or no route
            continue
        if now < plan.leave_at - lead:
            continue
        _mark(ev.key, "leave")
        _tell(plan.text(now), kind="leave", say_at_home=True)
        count += 1
    return count


# ── the night before ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Morning:
    event: CalEvent
    alarm: datetime
    trip: Trip | None
    dest: Spot | None


def plan_morning(events: list[CalEvent]) -> Morning | None:
    """Tomorrow's alarm from its first timed event, or None for a free morning."""
    cfg = _cfg()
    if not events:
        return None
    first = events[0]
    ah, am = _hhmm(cfg["alarm_if_before"], "11:00")
    if (first.start.hour, first.start.minute) >= (ah, am):
        return None
    trip = dest = None
    h = home()
    if not first.online and h is not None:
        dest = destination(first.location)
        if dest is not None:
            trip = travel(h, dest)
    away = timedelta(seconds=trip.seconds if trip else 0)
    if trip and trip.seconds > 0:
        away += timedelta(minutes=float(cfg["buffer_min"]))
    alarm = first.start - away - timedelta(minutes=float(cfg["ready_min"]))
    # Five-minute steps read like an alarm someone set: 7:20, not 7:23.
    alarm = alarm.replace(second=0, microsecond=0)
    alarm -= timedelta(minutes=alarm.minute % 5)
    return Morning(first, alarm, trip, dest)


def morning_say(m: Morning) -> str:
    """What she says on answering the alarm."""
    parts = [f"Good morning. {m.event.summary} is at {_clock(m.event.start)}"]
    if m.dest is not None:
        parts[0] += f" at {m.dest.name}"
    parts[0] += "."
    if m.trip is not None and m.trip.seconds > 0:
        leave = m.event.start - timedelta(seconds=m.trip.seconds) \
            - timedelta(minutes=float(_cfg()["buffer_min"]))
        parts.append(f"Leave by {_clock(leave.replace(second=0, microsecond=0))}; it's {m.trip.spoken}.")
    return " ".join(parts)


def _due_tomorrow(day: date) -> str:
    from nora import task_ledger
    titles = [t["title"] for t in task_ledger.due_that_day(day)]
    if not titles:
        return ""
    from nora.triggers import _join
    return f"Due tomorrow: {_join(titles[:3])}."


def check_night_before(now: datetime | None = None, events: list[CalEvent] | None = None) -> bool:
    """Once a night, in the window: set tomorrow's alarm and send the plan."""
    cfg = _cfg()
    if not cfg["night_before"]:
        return False
    now = now or datetime.now()
    sh, sm = _hhmm(cfg["night_before_at"], "21:30")
    eh, em = _hhmm(cfg["night_before_until"], "23:30")
    t = now.hour * 60 + now.minute
    if not sh * 60 + sm <= t < eh * 60 + em:
        return False
    tomorrow = now.date() + timedelta(days=1)
    key = tomorrow.isoformat()
    if _done(key, "night"):
        return False
    phone = _wake_phone()
    if not phone:
        # Kept for the next tick in the window: the phone may connect by then.
        return False
    if events is None:
        events = _fetch_day(datetime.combine(tomorrow, datetime.min.time()).replace(hour=12))
    m = plan_morning(events)
    _mark(key, "night")
    lines = []
    if m is None:
        if events:
            lines.append(f"Tomorrow starts with {events[0].summary} at {_clock(events[0].start)}, "
                         f"so I've left your alarm alone.")
    else:
        set_alarm(key, m.alarm, f"{m.event.summary} at {_clock(m.event.start)}", morning_say(m))
        line = f"Tomorrow: {m.event.summary} at {_clock(m.event.start)}"
        if m.dest is not None:
            line += f" at {m.dest.name}"
        if m.trip is not None and m.trip.seconds > 0:
            line += f", {m.trip.spoken}"
        lines.append(line + ".")
        lines.append(f"I've set an alarm on your phone for {_clock(m.alarm)}. Say undo to cancel it.")
        from nora import reversible
        reversible.record_action("set_wake_alarm", {"day": key}, "cancel_wake_alarm", {"day": key},
                                 f"Set a {_clock(m.alarm)} alarm on the phone for {m.event.summary}")
    due = _due_tomorrow(tomorrow)
    if due:
        lines.append(due)
    if lines:
        _tell(" ".join(lines), kind="alarm", say_at_home=False)
    return m is not None


# ── the wake alarms on the phone ─────────────────────────────────────────────

def _wake_phone() -> str:
    from nora.hub import server
    hub = server.get()
    if hub is None:
        return ""
    for device_id in hub.sessions():
        if hub.offers(device_id, WAKE_CAPABILITY):
            return device_id
    return ""


def set_alarm(day: str, at: datetime, title: str, say: str) -> None:
    with store.transaction() as conn:
        conn.execute("INSERT INTO wake_alarms (day, at, title, say, status, updated_at) "
                     "VALUES (?,?,?,?, 'set', ?) ON CONFLICT(day) DO UPDATE SET at=excluded.at, "
                     "title=excluded.title, say=excluded.say, status='set', updated_at=excluded.updated_at",
                     (day, at.timestamp(), title[:120], say[:600], time.time()))
    sync_all()


def cancel_alarm(day: str = "") -> str | None:
    """Cancel the alarm for `day` (ISO), or the next one. Its time, or None."""
    rows = (store.query("SELECT day, at FROM wake_alarms WHERE day = ? AND status = 'set'", (day,))
            if day else
            store.query("SELECT day, at FROM wake_alarms WHERE status = 'set' AND at > ? "
                        "ORDER BY at LIMIT 1", (time.time(),)))
    if not rows:
        return None
    with store.transaction() as conn:
        conn.execute("UPDATE wake_alarms SET status = 'cancelled', updated_at = ? WHERE day = ?",
                     (time.time(), rows[0]["day"]))
    sync_all()
    return _clock(datetime.fromtimestamp(rows[0]["at"]))


def alarms() -> list[dict]:
    """What the phone should have set: alarms still to come."""
    rows = store.query("SELECT day, at, title, say FROM wake_alarms WHERE status = 'set' AND at > ? "
                       "ORDER BY at", (time.time(),))
    return [{"id": r["day"], "at_ms": int(r["at"] * 1000), "title": r["title"], "say": r["say"]}
            for r in rows]


async def sync_device(hub, device_id: str) -> None:
    """Hand one device the alarm list. Runs on the hub's loop."""
    if not hub.offers(device_id, WAKE_CAPABILITY):
        return
    res = await hub.invoke(device_id, WAKE_CAPABILITY, {"alarms": alarms()})
    if res.success:
        logger.info("%s has %s wake alarm(s)", device_id, res.data.get("set", "?"))
    else:
        logger.warning("wake alarms on %s: %s", device_id, res.message)
        if res.error_code == "PERMISSION_DENIED":
            from nora import delivery
            delivery.deliver(res.message, device=device_id, kind="alarm")


def sync_all() -> None:
    from nora.hub import server
    hub = server.get()
    if hub is None or hub.loop is None:
        return
    for device_id in hub.sessions():
        asyncio.run_coroutine_threadsafe(sync_device(hub, device_id), hub.loop)


# ── saying it ────────────────────────────────────────────────────────────────

_speak: Callable[[str], None] | None = None


def _tell(text: str, *, kind: str, say_at_home: bool) -> None:
    """To every connected phone; said on the laptop too when the user is
    home (not in quiet hours or on a call). Nobody connected: the laptop's
    notification, so it is at least on a screen."""
    from nora import calls, delivery, focus, triggers
    logger.info("%s: %s", kind, text)
    sent = [d for d in delivery.connected() if delivery.deliver(text, device=d, kind=kind)]
    at_home = triggers.where(str(((get_config().get("triggers", {}) or {}).get("home", {}) or {})
                                 .get("place", "home"))) != triggers.OUTSIDE
    spoke = False
    if (say_at_home and at_home and _speak is not None and not calls.quiet_now()
            and focus.current() != focus.FocusState.CALL):
        try:
            _speak(text)
            spoke = True
        except Exception as e:
            logger.debug("saying %s failed: %s", kind, e)
    if not sent and not spoke:
        calls.notify_laptop("NORA", text)


# ── the tick ─────────────────────────────────────────────────────────────────

_thread: threading.Thread | None = None
_stop = threading.Event()


def tick(now: datetime | None = None) -> None:
    for rule in (check_leave_by, check_night_before):
        try:
            rule(now)
        except Exception as e:
            logger.warning("calendar rule %s failed: %s", rule.__name__, e)


def start(speak: Callable[[str], None] | None = None) -> None:
    global _thread, _speak
    _speak = speak
    if _thread is not None or not _cfg()["enabled"]:
        return

    def loop() -> None:
        while not _stop.wait(60):
            tick()

    _stop.clear()
    _thread = threading.Thread(target=loop, daemon=True, name="nora-calendar-rules")
    _thread.start()


def stop() -> None:
    global _thread
    _stop.set()
    _thread = None

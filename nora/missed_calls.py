"""Missed calls, in context (NORA_CALLS_PLAN §3.2).

The phone keeps its notifications in memory and tells the core about them only
when asked (plan §7.9), so the core never stores who called. It asks the
phone's `phone.missed_calls`, which reads the dialer's own missed-call
notifications (no call-log permission), and adds what only the core knows:
the calendar. That turns "Mom called twice" into "Mom called twice while you
were in Design review", which is what makes it worth saying.

Three places use it:

  - "Did anyone call?" / "who called while I was out": `who_called` lists the
    phone's missed calls, then any unknown numbers screening caught that the
    dialer doesn't show (blocked ones), from `nora.screened_calls`.
  - After a timed calendar event ends, the calls watcher asks for calls
    missed during it that are still unread on the phone, and sends one line
    to the phone ("Mom called twice while you were in Design review").
  - The morning briefing says who called overnight.

Whoever NORA last mentioned is what "call her back" calls, through
`phone.call_contact`, so it is approved on the phone like any other call.
Caller names are notification text, so results are marked untrusted.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from nora import screened_calls

logger = logging.getLogger("nora.missed_calls")

CAPABILITY = "phone.missed_calls"

_ROOT = Path(__file__).resolve().parent.parent
_STATE_PATH = _ROOT / "nora_missed_calls_state.json"

# What "call her back" means stays this long after NORA said it.
_MENTION_TTL_SEC = 3 * 3600
# An event that ended this recently is checked for calls missed during it.
_ENDED_WITHIN_MIN = 15
_UNKNOWN = "An unknown caller"


@dataclass
class Missed:
    caller: str
    count: int
    first_at: float
    last_at: float
    on_screen: bool = True

    @property
    def number(self) -> str:
        """The caller when the dialer showed a number rather than a name."""
        return self.caller if _looks_like_number(self.caller) else ""


def _looks_like_number(s: str) -> bool:
    return bool(re.fullmatch(r"[+\d][\d\s()\-.]{4,}", s.strip())) and sum(c.isdigit() for c in s) >= 5


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)[-10:]


# ── Asking the phone ─────────────────────────────────────────────────────────

def _parse(data: dict, now: float) -> list[Missed]:
    out = []
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        try:
            last = now - float(item.get("minutes_ago", 0)) * 60
            first = now - float(item.get("first_minutes_ago", item.get("minutes_ago", 0))) * 60
            count = max(1, int(item.get("count", 1)))
        except (TypeError, ValueError):
            continue
        caller = " ".join(str(item.get("caller") or "").split())[:60] or _UNKNOWN
        out.append(Missed(caller, count, first, last, bool(item.get("on_screen", True))))
    return sorted(out, key=lambda m: m.last_at, reverse=True)


async def fetch(hours: float = 24, *, include_seen: bool = True) -> list[Missed] | None:
    """The phone's missed calls, newest caller first. None when no phone offers it."""
    from nora.hub import server
    minutes = max(1, min(int(hours * 60), 1440))
    result = await server.call_capability(CAPABILITY, {"since_minutes": minutes,
                                                       "include_seen": include_seen})
    if not result.success:
        logger.info("No missed calls from the phone: %s", result.message[:80])
        return None
    return _parse(result.data, time.time())


def fetch_sync(hours: float = 24, *, include_seen: bool = True) -> list[Missed] | None:
    """`fetch` from a worker thread (the briefing, the calls watcher)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(fetch(hours, include_seen=include_seen))
    except Exception as e:
        logger.warning("Asking the phone for missed calls failed: %s", e)
        return None
    finally:
        loop.close()


def _offered() -> bool:
    from nora import command_engine
    meta = command_engine.get_action_meta(CAPABILITY)
    return meta is not None and bool(meta.device)


# ── The calendar around them ─────────────────────────────────────────────────

def _span(ev: dict) -> tuple[float, float] | None:
    """A timed event's (start, end) as timestamps; None for all-day ones."""
    try:
        start = datetime.fromisoformat(ev["start"]["dateTime"]).timestamp()
        end = datetime.fromisoformat(ev["end"]["dateTime"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None
    return (start, end) if end > start else None


def _events_around(since: float, until: float) -> list[dict]:
    """Timed events overlapping [since, until], or [] without a calendar."""
    try:
        from nora.commands import google_services as gs
        if not gs._TOKEN_FILE.exists():
            return []
        days, day = [], datetime.fromtimestamp(since).date()
        while day <= datetime.fromtimestamp(until).date() and len(days) < 3:
            days.append(day)
            day += timedelta(days=1)
        events = []
        for d in days:
            events += gs._events_between(*gs._day_window(datetime.combine(d, datetime.min.time())))
    except Exception as e:
        logger.debug("No calendar for missed calls: %s", e)
        return []
    return [ev for ev in events if (s := _span(ev)) and s[0] <= until and s[1] >= since]


def during(call: Missed, events: list[dict]) -> str:
    """The event the user was in when this caller called, or ""."""
    for ev in events:
        span = _span(ev)
        if span and span[0] <= call.last_at <= span[1] and span[0] <= call.first_at <= span[1]:
            return " ".join(str(ev.get("summary") or "").split())[:60] or "a meeting"
    return ""


# ── Saying them ──────────────────────────────────────────────────────────────

_TIMES = {1: "", 2: " twice"}


def _clock(at: float, now: datetime) -> str:
    then = datetime.fromtimestamp(at)
    clock = then.strftime("%-I:%M %p")
    if then.date() == now.date():
        return f"at {clock}"
    if (now.date() - then.date()).days == 1:
        return f"yesterday at {clock}"
    return f"on {then.strftime('%A')} at {clock}"


def _line(c: Missed, events: list[dict], now: datetime) -> str:
    times = _TIMES.get(c.count, f" {c.count} times")
    where = during(c, events)
    if where:
        return f"{c.caller} called{times} while you were in {where}"
    when = _clock(c.last_at, now)
    return f"{c.caller} called{times}, {'last ' if c.count > 1 else ''}{when}"


def describe(calls: list[Missed], events: list[dict], now: datetime | None = None,
             *, limit: int = 4) -> str:
    """Spoken: who called, newest first."""
    now = now or datetime.now()
    lines = [_line(c, events, now) for c in calls[:limit]]
    more = len(calls) - limit
    tail = f", and {more} other{'s' if more != 1 else ''} called too" if more > 0 else ""
    return "; ".join(lines) + tail + "."


def _callback_hint(calls: list[Missed]) -> str:
    if len(calls) == 1 and calls[0].caller != _UNKNOWN:
        return " Say 'call back' to return it."
    return ""


async def who_called(hours: float = 24) -> tuple[str, bool]:
    """(what to say, whether it came from the phone and so is untrusted)."""
    calls = await fetch(hours) if _offered() else None
    unknown = screened_calls.recent(hours)
    if calls is None:
        said = screened_calls.describe(unknown, hours)
        if not _offered():
            said += " Your phone isn't connected, so I can't see calls from your contacts."
        return said, False
    # Screening already logs unknown numbers; those the dialer shows too are
    # said once, from the dialer. Blocked numbers never reach the dialer.
    shown = {_digits(c.number) for c in calls if c.number}
    extra = [s for s in unknown if s.action == "rejected" and _digits(s.number) not in shown]
    if not calls and not extra:
        span = f"{hours:g} hours" if hours < 48 else f"{round(hours / 24)} days"
        return f"No missed calls in the last {span}.", True
    events = _events_around(min((c.first_at for c in calls), default=time.time()), time.time())
    parts = []
    if calls:
        parts.append(describe(calls, events) + _callback_hint(calls))
        remember(calls[0])
    if extra:
        n = len(extra)
        parts.append(f"{'One blocked number' if n == 1 else f'{n} blocked numbers'} called too.")
    return " ".join(parts), True


# ── "Call her back" ──────────────────────────────────────────────────────────

_lock = threading.Lock()
_mentioned: tuple[Missed, float] | None = None


def remember(call: Missed) -> None:
    global _mentioned
    with _lock:
        _mentioned = (call, time.time())


def mentioned() -> Missed | None:
    with _lock:
        if _mentioned and time.time() - _mentioned[1] < _MENTION_TTL_SEC:
            return _mentioned[0]
    return None


def callback_params(call: Missed) -> dict:
    """What `phone.call_contact` needs to call this caller back."""
    return {"number": call.number} if call.number else {"name": call.caller}


async def who_to_call_back() -> Missed | None:
    """Who "call back" means: who NORA last mentioned, else the newest missed
    caller on the phone, else the last unknown number screening saw."""
    picked = mentioned()
    if picked is not None:
        return picked
    if _offered():
        calls = await fetch(24) or []
        calls = [c for c in calls if c.caller != _UNKNOWN]
        if calls:
            return calls[0]
    number = screened_calls.last_number()
    return Missed(number, 1, time.time(), time.time()) if number else None


# ── After a meeting ──────────────────────────────────────────────────────────

def _load_checked() -> dict[str, float]:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8")).get("events", {})
    except Exception:
        return {}


def _save_checked(checked: dict[str, float]) -> None:
    try:
        _STATE_PATH.write_text(json.dumps({"events": checked}, indent=1), encoding="utf-8")
    except Exception as e:
        logger.warning("Saving missed-call state failed: %s", e)


def just_ended(events: list[dict], now: float) -> list[tuple[str, dict, tuple[float, float]]]:
    """Timed events that ended within the last `_ENDED_WITHIN_MIN` minutes."""
    out = []
    for ev in events:
        span = _span(ev)
        if span and 0 <= now - span[1] <= _ENDED_WITHIN_MIN * 60:
            out.append((f"{ev.get('id', '')}@{ev['end']['dateTime']}", ev, span))
    return out


def after_meeting_line(calls: list[Missed], ev: dict, span: tuple[float, float]) -> str:
    """One line for the calls missed during `ev`, or "" when there were none."""
    inside = [c for c in calls if c.on_screen and span[0] <= c.first_at and c.last_at <= span[1]]
    if not inside:
        return ""
    name = " ".join(str(ev.get("summary") or "").split())[:60] or "your meeting"
    said = [f"{c.caller} called{_TIMES.get(c.count, f' {c.count} times')}" for c in inside[:3]]
    if len(inside) > 3:
        said.append(f"{len(inside) - 3} more")
    head = said[0] if len(said) == 1 else ", ".join(said[:-1]) + " and " + said[-1]
    return f"{head} while you were in {name}." + _callback_hint(inside)


def check_meetings(now: float | None = None) -> int:
    """Tell the phone who called during an event that just ended. How many lines went out."""
    from nora import calls as _calls
    if not _offered() or _calls.quiet_now():
        return 0
    now = now or time.time()
    events = _events_around(now - (_ENDED_WITHIN_MIN + 1) * 60, now)
    checked = _load_checked()
    fresh = {k: v for k, v in checked.items() if v > now - 2 * 86400}
    due = [(k, ev, span) for k, ev, span in just_ended(events, now) if k not in fresh]
    if not due:
        if fresh != checked:
            _save_checked(fresh)
        return 0
    for key, _, _ in due:
        fresh[key] = now
    _save_checked(fresh)              # before asking: a crash mustn't say it twice
    longest = max(now - span[0] for _, _, span in due) / 3600
    missed = fetch_sync(min(24.0, longest + 0.1), include_seen=False)
    if not missed:
        return 0
    from nora import delivery
    sent = 0
    for _, ev, span in due:
        line = after_meeting_line(missed, ev, span)
        if not line:
            continue
        inside = [c for c in missed if span[0] <= c.first_at and c.last_at <= span[1]]
        remember(inside[0])
        delivery.broadcast(line, kind="reminder")
        logger.info("Missed calls during an event: %d caller(s)", len(inside))
        sent += 1
    return sent


# ── The briefing ─────────────────────────────────────────────────────────────

def briefing_line(hours: float = 12) -> str:
    """Who the user missed overnight, still unread on the phone. Without the
    phone, or with nothing unread there, the unknown numbers screening saw."""
    calls = fetch_sync(hours, include_seen=False) if _offered() else None
    if not calls:
        return screened_calls.briefing_line(hours)
    remember(calls[0])
    events = _events_around(min(c.first_at for c in calls), time.time())
    return describe(calls, events, limit=3)


def reset_for_tests() -> None:
    global _mentioned
    with _lock:
        _mentioned = None

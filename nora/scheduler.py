"""Scheduler — time-triggered work, spoken when it lands.

`nora.proactive` already speaks unprompted, but only reactively: it watches the
behavioural model and fires when the moment looks right. There was nothing that
fired because a *clock* said so, which meant NORA could not hold the single most
ordinary instruction anyone gives a voice assistant — "remind me at six",
"brief me every morning".

This is that missing half. A schedule is a durable row in `nora_schedules.json`;
a ticker thread wakes every `_TICK_SEC`, and anything due is handed to
`nora.jobs`, which runs it off-turn and speaks the result through the
focus-gated channel. The scheduler decides *when*; jobs owns *running* and
*saying*. Neither knows about the other's problem.

Specs are parsed from speech, not from crontab syntax, because the input
arrives out loud: "every day at 7", "in 20 minutes", "every Monday at 9am",
"every half hour". A literal five-field cron string is also accepted — it costs
almost nothing to support and is the natural thing to type into config.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("nora.scheduler")

_ROOT = Path(__file__).resolve().parent.parent
_SCHED_PATH = _ROOT / "nora_schedules.json"

# How often the ticker checks for due work. Schedules are minute-resolution at
# best (nobody says "remind me at 6:03:20"), so a 20s tick is three chances to
# catch every minute without spinning.
_TICK_SEC = 20.0

# Marks a schedule whose payload is a sentence to speak, not a command to run.
_REMINDER_PREFIX = "remind: "

_DAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}


@dataclass
class Schedule:
    id: str
    spec: str                 # the user's own words, spoken back on "what's scheduled"
    what: str                 # the instruction to carry out when it fires
    next_run: float
    recurring: bool = False
    # Recurrence, resolved at parse time. Exactly one is meaningful:
    #   interval_sec > 0     -> every N seconds
    #   daily_at is not None -> (hour, minute), optionally narrowed by weekday
    interval_sec: float = 0.0
    daily_at: list[int] | None = None   # [hour, minute]
    weekday: int | None = None          # 0=Monday
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    last_run: float = 0.0
    run_count: int = 0

    def describe(self) -> str:
        when = datetime.fromtimestamp(self.next_run).strftime("%A at %-I:%M %p")
        return f"{self.what} — {self.spec} (next {when})"


_lock = threading.RLock()
_schedules: dict[str, Schedule] = {}
_thread: threading.Thread | None = None
_stop = threading.Event()
_loaded = False

# Injected so tests can run a schedule without a model or a microphone.
_runner: Callable[[str], str] | None = None


# ── Spec parsing ─────────────────────────────────────────────────────────────

def _next_daily(hour: int, minute: int, weekday: int | None, now: datetime) -> datetime:
    """The next datetime matching hour:minute, optionally on a given weekday."""
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if weekday is None:
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    days_ahead = (weekday - candidate.weekday()) % 7
    candidate += timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def _parse_clock(text: str) -> tuple[int, int] | None:
    """Pull an hour:minute out of '7', '7am', '7:30 pm', '19:00'.

    Bare hours are read the way people mean them out loud: "at 7" in the
    evening means 19:00, not 07:00 tomorrow morning. Anything 1-6 is assumed
    to be PM for the same reason; 7-11 resolves to whichever comes first.
    """
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", text)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    meridiem = (m.group(3) or "").replace(".", "").lower()

    if hour > 23 or minute > 59:
        return None
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    elif not meridiem and 1 <= hour <= 6:
        # "at 5" almost never means 05:00.
        hour += 12
    return hour, minute


def _parse_cron_field(spec: str) -> Schedule | None:
    """Accept a literal five-field crontab line for the minute/hour/weekday case."""
    parts = spec.split()
    if len(parts) != 5:
        return None
    minute, hour, dom, mon, dow = parts
    if not (minute.isdigit() and hour.isdigit()):
        return None
    if dom != "*" or mon != "*":
        return None  # day-of-month/month recurrence isn't supported
    weekday = None
    if dow != "*":
        if not dow.isdigit():
            return None
        # crontab counts Sunday as 0; Python's weekday() counts Monday as 0.
        weekday = (int(dow) - 1) % 7
    now = datetime.now()
    nxt = _next_daily(int(hour), int(minute), weekday, now)
    return Schedule(
        id="", spec=spec, what="", next_run=nxt.timestamp(), recurring=True,
        daily_at=[int(hour), int(minute)], weekday=weekday,
    )


def parse_spec(spec: str) -> Schedule | None:
    """Turn a spoken time expression into an unsaved `Schedule`.

    Returns None when nothing recognisable is in the string, which the caller
    surfaces as a question rather than guessing a time.
    """
    raw = spec.strip()
    text = raw.lower()
    now = datetime.now()

    cron = _parse_cron_field(raw)
    if cron is not None:
        return cron

    # "in 20 minutes" / "in an hour" — one-shot, relative.
    m = re.search(r"\bin\s+(a|an|\d+)\s*(second|minute|min|hour|hr|day)s?\b", text)
    if m:
        qty = 1 if m.group(1) in ("a", "an") else int(m.group(1))
        unit = m.group(2)
        seconds = qty * {
            "second": 1, "minute": 60, "min": 60,
            "hour": 3600, "hr": 3600, "day": 86400,
        }[unit]
        return Schedule(id="", spec=raw, what="", next_run=time.time() + seconds)

    # "every 30 minutes" / "every half hour" — recurring interval.
    if re.search(r"\bevery\s+half\s+(an\s+)?hour\b", text):
        return Schedule(id="", spec=raw, what="", next_run=time.time() + 1800,
                        recurring=True, interval_sec=1800)
    m = re.search(r"\bevery\s+(\d+)\s*(second|minute|min|hour|hr)s?\b", text)
    if m:
        seconds = int(m.group(1)) * {
            "second": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600,
        }[m.group(2)]
        if seconds < 30:
            seconds = 30  # floor: a voice assistant talking every 5s is a fault
        return Schedule(id="", spec=raw, what="", next_run=time.time() + seconds,
                        recurring=True, interval_sec=seconds)

    # A named weekday, with or without "every".
    weekday = next((idx for name, idx in _DAYS.items()
                    if re.search(rf"\b{name}\b", text)), None)

    clock = _parse_clock(text)
    recurring = bool(re.search(r"\bevery\b|\beach\b|\bdaily\b", text))

    if clock is None:
        # "every monday" with no time — default to 9am, the neutral morning slot.
        if weekday is None:
            return None
        clock = (9, 0)

    hour, minute = clock
    if "tomorrow" in text:
        base = now + timedelta(days=1)
        nxt = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    else:
        nxt = _next_daily(hour, minute, weekday, now)

    return Schedule(
        id="", spec=raw, what="", next_run=nxt.timestamp(),
        recurring=recurring or weekday is not None,
        daily_at=[hour, minute], weekday=weekday,
    )


def _advance(sched: Schedule) -> None:
    """Move a recurring schedule to its next occurrence; disable a one-shot."""
    if not sched.recurring:
        sched.enabled = False
        return
    now = datetime.now()
    if sched.interval_sec:
        # Anchor to now, not to the missed slot, so a laptop that was asleep
        # for six hours doesn't fire twelve catch-up runs on wake.
        sched.next_run = time.time() + sched.interval_sec
    elif sched.daily_at:
        hour, minute = sched.daily_at
        sched.next_run = _next_daily(hour, minute, sched.weekday, now).timestamp()
    else:
        sched.enabled = False


# ── Persistence ──────────────────────────────────────────────────────────────

def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not _SCHED_PATH.exists():
        return
    try:
        raw = json.loads(_SCHED_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Could not read %s: %s", _SCHED_PATH.name, e)
        return
    known = set(Schedule.__dataclass_fields__)
    now = time.time()
    for entry in raw.get("schedules", []):
        try:
            sched = Schedule(**{k: v for k, v in entry.items() if k in known})
        except Exception:
            continue
        # A due-time that passed while NORA was off fires once on the next
        # tick for a one-shot ("remind me at 6" still matters at 6:05), but a
        # recurring schedule rolls forward instead of replaying the backlog.
        if sched.enabled and sched.next_run < now and sched.recurring:
            _advance(sched)
        _schedules[sched.id] = sched
    logger.info("Loaded %d schedules", len(_schedules))


def _save() -> None:
    with _lock:
        payload = {"schedules": [asdict(s) for s in _schedules.values()]}
    try:
        tmp = _SCHED_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(_SCHED_PATH)
    except Exception as e:
        logger.warning("Could not save schedules: %s", e)


# ── Public API ───────────────────────────────────────────────────────────────

def add(spec: str, what: str) -> Schedule | None:
    """Create a schedule. Returns None when the time expression is unparseable."""
    sched = parse_spec(spec)
    if sched is None:
        return None
    sched.id = uuid.uuid4().hex[:8]
    sched.what = what.strip()
    with _lock:
        _load()
        _schedules[sched.id] = sched
    _save()
    logger.info("Scheduled [%s] %s :: %s", sched.id, sched.spec, sched.what)
    return sched


def remove(query: str) -> Schedule | None:
    """Cancel a schedule by id, or by word-overlap against what it does."""
    with _lock:
        _load()
        if query in _schedules:
            sched = _schedules.pop(query)
            _save()
            return sched
        words = {w for w in query.lower().split() if len(w) > 3}
        best: tuple[int, Schedule] | None = None
        for sched in _schedules.values():
            overlap = len(words & set(sched.what.lower().split()))
            if overlap and (best is None or overlap > best[0]):
                best = (overlap, sched)
        if best is None:
            return None
        _schedules.pop(best[1].id, None)
    _save()
    return best[1]


def listing() -> list[Schedule]:
    with _lock:
        _load()
        live = [s for s in _schedules.values() if s.enabled]
    return sorted(live, key=lambda s: s.next_run)


def set_runner(fn: Callable[[str], str] | None) -> None:
    """Override how a fired schedule's instruction is carried out (tests)."""
    global _runner
    _runner = fn


def _default_runner(what: str) -> str:
    """Carry out a schedule's instruction on the worker thread.

    Routed through the intent parser and command engine so a schedule can do
    anything a spoken command can — "play my focus playlist", "give me the
    daily brief" — rather than being limited to reminders. Anything that comes
    back without a real result degrades to speaking the instruction itself,
    which is the correct behaviour for the plain-reminder case.
    """
    import asyncio

    from nora import command_engine, intent_parser

    # `remind_me` stores its payload with this prefix. A reminder is not a
    # command to execute — running "buy milk" through the intent parser would
    # have it hunt for a matching action and fail. It is a sentence to say.
    if what.startswith(_REMINDER_PREFIX):
        message = what[len(_REMINDER_PREFIX):].strip()
        try:
            from nora.commands.notifications import _toast
            _toast("NORA Reminder", message)
        except Exception as e:
            logger.debug("reminder toast failed: %s", e)
        return f"Reminder, sir: {message}"

    try:
        intent = intent_parser.parse_intent(what, {})
    except Exception as e:
        logger.debug("scheduled parse failed (%s); speaking as reminder", e)
        return what
    if not intent.steps:
        return intent.response or what

    loop = asyncio.new_event_loop()
    try:
        results = loop.run_until_complete(command_engine.execute(intent))
    except Exception as e:
        logger.warning("scheduled run failed: %s", e)
        return what
    finally:
        loop.close()

    spoken = [r.message for r in results if r.message]
    return " ".join(spoken) if spoken else what


def _fire(sched: Schedule) -> None:
    from nora import jobs

    what = sched.what
    runner = _runner or _default_runner
    jobs.submit(
        what[:60],
        lambda: runner(what),
        kind="cron",
    )
    with _lock:
        sched.last_run = time.time()
        sched.run_count += 1
        _advance(sched)
    _save()
    logger.info("Fired schedule [%s] %s", sched.id, what[:60])


def tick(now: float | None = None) -> int:
    """Fire everything due. Returns how many fired. Exposed for tests."""
    now = now or time.time()
    with _lock:
        _load()
        due = [s for s in _schedules.values() if s.enabled and s.next_run <= now]
    for sched in due:
        try:
            _fire(sched)
        except Exception as e:
            logger.error("Schedule %s failed to fire: %s", sched.id, e)
    return len(due)


def _loop() -> None:
    while not _stop.wait(_TICK_SEC):
        try:
            tick()
        except Exception as e:
            logger.error("scheduler tick failed: %s", e)


def start() -> None:
    """Start the ticker. Safe to call twice."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _load()
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="nora-scheduler")
    _thread.start()
    logger.info("Scheduler started (%d schedules)", len(_schedules))


def stop() -> None:
    global _thread
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=2.0)
    _thread = None


def reset_for_tests() -> None:
    global _loaded, _runner
    stop()
    with _lock:
        _schedules.clear()
    _loaded = True   # skip disk entirely under test
    _runner = None

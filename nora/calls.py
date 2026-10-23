"""NORA ringing the phone on her own — the core's side of `phone.call`.

The phone rings like an incoming call (Calls.kt); answering speaks why she
called and then listens. This module decides when that happens and what
happens when nobody picks up:

  - `ring()` is the one way the core rings. Everything that rings goes
    through it: a reminder or wake-up call the user asked for, a calendar
    event about to start, a long background answer landing, a screen watch
    that found what it was told to look for.
  - A missed call is tried once more `retry_minutes` later. Missed again (or
    the phone can't be reached for the retry), it is left on the laptop as a
    desktop notification, beside the missed call the phone already shows, and
    a longer `say` (a briefing, an answer) is sent to the phone as a message,
    so what she called to say is still there to read. Declined is an answer:
    no retry.
  - Calls nobody asked for (calendar, long jobs, watches) stay quiet between
    `quiet_hours`; they go out as notifications instead. Calls the user asked
    for ("call me at 7") ring whatever the hour.

The phone reports how a call ended with a `call.ended` event, which the hub
hands to `ended()`. The phone rings one call at a time, so the outstanding
call per device is all the state there is to match it to.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from nora.config import get_config

logger = logging.getLogger("nora.calls")

CAPABILITY = "phone.call"

_ROOT = Path(__file__).resolve().parent.parent
_STATE_PATH = _ROOT / "nora_calls_state.json"


def _cfg() -> dict:
    return get_config().get("calls", {}) or {}


@dataclass
class _Call:
    reason: str
    say: str = ""
    # Rebuilds `say` for the retry, so a briefing missed at 7:00 isn't read
    # out stale at 7:05. None keeps the first one.
    refresh: Callable[[], str] | None = None
    retry: bool = True
    attempt: int = 1
    device: str = ""
    rang_at: float = field(default_factory=time.time)


_lock = threading.Lock()
_outstanding: dict[str, _Call] = {}
_timers: list[threading.Timer] = []


# ── Ringing ──────────────────────────────────────────────────────────────────

def _phone() -> str:
    """The device that offers `phone.call`, or "" when none is connected."""
    from nora import command_engine
    meta = command_engine.get_action_meta(CAPABILITY)
    return (meta.device or "") if meta else ""


def _invoke(params: dict) -> tuple[bool, str]:
    """Ring through the hub from any thread. (rang, what the phone said)."""
    import asyncio

    from nora.hub import server

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(server.call_capability(CAPABILITY, params))
    except Exception as e:
        logger.warning("Ringing the phone failed: %s", e)
        return False, ""
    finally:
        loop.close()
    return result.success and result.data.get("rang") is True, result.message or ""


def ring(reason: str, *, say: str = "", refresh: Callable[[], str] | None = None,
         retry: bool = True, asked: bool = True) -> bool:
    """Ring the phone. True when it rang.

    `reason` is what the ringing screen shows, and what she says on answering
    unless `say` is given — `say` is for something longer than a screen line,
    like a whole briefing. `asked` is False for calls the user didn't ask for,
    which stay quiet during `quiet_hours`. False back means it didn't ring
    (no phone, the user is already talking to her, quiet hours): the caller
    falls back to however it would have said it otherwise.
    """
    if not _cfg().get("enabled", True):
        return False
    if not asked and quiet_now():
        logger.info("Not ringing during quiet hours: %s", reason[:60])
        return False
    device = _phone()
    if not device:
        return False
    call = _Call(reason=reason.strip()[:300], say=say.strip(), refresh=refresh,
                 retry=retry, device=device)
    return _place(call)


def _place(call: _Call) -> bool:
    params = {"reason": call.reason}
    if call.say and call.say != call.reason:
        params["say"] = call.say[:3000]
    # Before the invoke: the phone could report the call ending before the
    # invoke's reply has made it back here.
    with _lock:
        _outstanding[call.device] = call
    rang, message = _invoke(params)
    if not rang:
        with _lock:
            if _outstanding.get(call.device) is call:
                del _outstanding[call.device]
    logger.info("Call (attempt %d) %s: %s", call.attempt, "rang" if rang else "not rung",
                message[:80])
    return rang


# ── How it ended ─────────────────────────────────────────────────────────────

def ended(device: str, outcome: str) -> None:
    """The phone's `call.ended`: answered, declined or missed."""
    with _lock:
        call = _outstanding.pop(device, None)
    if call is None or outcome != "missed":
        return
    minutes = float(_cfg().get("retry_minutes", 5))
    if call.retry and call.attempt == 1 and minutes > 0:
        logger.info("Missed call; trying again in %g min", minutes)
        timer = threading.Timer(minutes * 60, _retry, (call,))
        timer.daemon = True
        with _lock:
            _timers[:] = [t for t in _timers if t.is_alive()] + [timer]
        timer.start()
        return
    # Off the hub's loop, which is what calls this.
    threading.Thread(target=_missed, args=(call,), daemon=True).start()


def _retry(call: _Call) -> None:
    if call.refresh is not None:
        try:
            call.say = call.refresh() or call.say
        except Exception as e:
            logger.warning("Refreshing a call's message failed: %s", e)
    call.attempt += 1
    call.rang_at = time.time()
    call.device = _phone() or call.device
    if not _place(call):
        _missed(call)


def _missed(call: _Call) -> None:
    """Given up ringing: the laptop shows it, and the phone gets what she'd have said."""
    when = datetime.fromtimestamp(call.rang_at).strftime("%-I:%M %p")
    notify_laptop(f"Missed call from NORA ({when})", call.reason)
    if call.say and call.say != call.reason and call.device:
        from nora import delivery
        delivery.deliver(call.say, device=call.device, kind="call")


def notify_laptop(title: str, body: str) -> None:
    try:
        subprocess.run(["notify-send", "--app-name=NORA", "--urgency=critical", title, body],
                       capture_output=True, timeout=5)
    except Exception as e:
        logger.debug("Desktop notification failed: %s", e)


# ── Quiet hours ──────────────────────────────────────────────────────────────

def quiet_now(now: datetime | None = None) -> bool:
    """Inside `quiet_hours` ("23:00-07:00"), when unasked calls don't ring."""
    span = str(_cfg().get("quiet_hours", "") or "")
    try:
        start_s, end_s = span.split("-")
        sh, sm = (int(x) for x in start_s.strip().split(":"))
        eh, em = (int(x) for x in end_s.strip().split(":"))
    except ValueError:
        return False
    now = now or datetime.now()
    t, start, end = now.hour * 60 + now.minute, sh * 60 + sm, eh * 60 + em
    return start <= t < end if start <= end else t >= start or t < end


# ── Calendar heads-up ────────────────────────────────────────────────────────

def _load_rung() -> dict[str, float]:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8")).get("calendar", {})
    except Exception:
        return {}


def _save_rung(rung: dict[str, float]) -> None:
    try:
        _STATE_PATH.write_text(json.dumps({"calendar": rung}, indent=1), encoding="utf-8")
    except Exception as e:
        logger.warning("Saving call state failed: %s", e)


def due_events(events: list[dict], now: datetime, lead_min: float) -> list[tuple[str, dict, int]]:
    """Timed events starting within `lead_min` minutes: (key, event, minutes away).
    All-day events have no start to warn about and are skipped."""
    out = []
    for ev in events:
        start = (ev.get("start") or {}).get("dateTime")
        if not start:
            continue
        try:
            at = datetime.fromisoformat(start).astimezone().replace(tzinfo=None)
        except ValueError:
            continue
        away = (at - now).total_seconds() / 60
        if 0 <= away <= lead_min:
            out.append((f"{ev.get('id', '')}@{start}", ev, max(1, round(away))))
    return out


def check_calendar(now: datetime | None = None) -> int:
    """Ring for events about to start. Returns how many rang or were notified."""
    lead = float(_cfg().get("calendar_minutes", 10))
    if lead <= 0:
        return 0
    from nora.commands import google_services as gs
    if not gs._TOKEN_FILE.exists():
        return 0
    now = now or datetime.now()
    window = now + timedelta(minutes=lead)
    time_min, time_max = gs._day_window(now)
    events = gs._events_between(time_min, time_max)
    if window.date() != now.date():          # a 23:55 check sees tomorrow's 00:03 event
        events = events + gs._events_between(*gs._day_window(window))

    rung = _load_rung()
    fresh = {k: v for k, v in rung.items() if v > time.time() - 2 * 86400}
    count = 0
    for key, ev, away in due_events(events, now, lead):
        if key in fresh:
            continue
        fresh[key] = time.time()
        _save_rung(fresh)        # before ringing: a crash mid-call mustn't ring it twice
        summary = ev.get("summary") or "Something on your calendar"
        text = f"{summary} starts in {away} minute{'s' if away != 1 else ''}, at {gs._fmt_event_time(ev)}."
        if not ring(text, asked=False):
            _notify_instead(text)
        count += 1
    if fresh != rung:
        _save_rung(fresh)
    return count


def _notify_instead(text: str) -> None:
    """What didn't ring is still said: on the phone if connected, and the laptop."""
    from nora import delivery
    delivery.broadcast(text, kind="reminder")
    notify_laptop("NORA", text)


_thread: threading.Thread | None = None
_stop = threading.Event()


def start() -> None:
    """The calendar watcher. Off with `calls.enabled: false` or `calendar_minutes: 0`."""
    global _thread
    if _thread is not None or not _cfg().get("enabled", True):
        return
    if float(_cfg().get("calendar_minutes", 10)) <= 0:
        return

    def loop() -> None:
        while not _stop.wait(60):
            try:
                check_calendar()
            except Exception as e:
                logger.debug("calendar call check failed: %s", e)
            try:
                from nora import missed_calls
                missed_calls.check_meetings()
            except Exception as e:
                logger.debug("missed-call check failed: %s", e)

    _stop.clear()
    _thread = threading.Thread(target=loop, daemon=True, name="nora-calls")
    _thread.start()


def stop() -> None:
    global _thread
    _stop.set()
    with _lock:
        for t in _timers:
            t.cancel()
        _timers.clear()
    _thread = None


def reset_for_tests() -> None:
    stop()
    with _lock:
        _outstanding.clear()

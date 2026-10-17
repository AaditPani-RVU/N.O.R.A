"""Calls from numbers not in the phone's contacts (NORA_CALLS_PLAN §3.1).

Android asks the phone app holding the call-screening role about each
incoming call from a number that isn't a contact, before it rings. The phone
decides on its own (ring, silence, or reject a number the user blocked), so
screening works with the core unreachable, and tells the core afterwards
with a `call.screened` event. Those land here, so "who called while I was
out", "block the last number that called" and "call that number back" have
something to go on, and the briefing can say "two unknown numbers called".

Calls from contacts never reach a screening app, so they are never here.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime

from nora import store

logger = logging.getLogger("nora.screened_calls")

ACTIONS = ("rang", "silenced", "rejected")
_KEEP_DAYS = 30


@dataclass
class Screened:
    number: str
    action: str
    at: float

    @property
    def who(self) -> str:
        return self.number or "a withheld number"


def record(device_id: str, data: dict) -> None:
    number = str(data.get("number") or "").strip()[:32]
    action = str(data.get("action") or "")
    if action not in ACTIONS:
        logger.warning("call.screened with an unknown action %r", action[:20])
        return
    now = time.time()
    with store.transaction() as conn:
        conn.execute("INSERT INTO screened_calls (device_id, number, action, at) VALUES (?, ?, ?, ?)",
                     (device_id, number, action, now))
        conn.execute("DELETE FROM screened_calls WHERE at < ?", (now - _KEEP_DAYS * 86400,))
    logger.info("Screened call from %s: %s", number or "a withheld number", action)


def recent(hours: float = 24) -> list[Screened]:
    """Newest first."""
    rows = store.query("SELECT number, action, at FROM screened_calls WHERE at >= ? ORDER BY at DESC, id DESC",
                       (time.time() - hours * 3600,))
    return [Screened(r["number"], r["action"], r["at"]) for r in rows]


def last_number() -> str:
    """The last unknown caller whose number was shown, or ""."""
    rows = store.query("SELECT number FROM screened_calls WHERE number != '' ORDER BY at DESC, id DESC LIMIT 1")
    return rows[0]["number"] if rows else ""


def _when(at: float, now: datetime) -> str:
    then = datetime.fromtimestamp(at)
    clock = then.strftime("%-I:%M %p")
    if then.date() == now.date():
        return f"at {clock}"
    if (now.date() - then.date()).days == 1:
        return f"yesterday at {clock}"
    return f"on {then.strftime('%A')} at {clock}"


_HOW = {"rang": "", "silenced": ", silenced", "rejected": ", blocked"}
_COUNT = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five"}


def describe(calls: list[Screened], hours: float, now: datetime | None = None) -> str:
    """Spoken: who called, newest first, at most five."""
    now = now or datetime.now()
    span = f"in the last {hours:g} hours" if hours < 48 else f"in the last {round(hours / 24)} days"
    if not calls:
        return f"No unknown numbers have called {span}. Calls from your contacts aren't tracked."
    n = len(calls)
    head = f"{_COUNT.get(n, str(n))} unknown number{'s' if n != 1 else ''} called {span}"
    items = [f"{c.who} {_when(c.at, now)}{_HOW[c.action]}" for c in calls[:5]]
    more = f", and {n - 5} more" if n > 5 else ""
    return f"{head}: " + "; ".join(items) + more + "."


def briefing_line(hours: float = 12) -> str:
    calls = [c for c in recent(hours) if c.action != "rejected"]
    if not calls:
        return ""
    n = len(calls)
    return (f"{_COUNT.get(n, str(n))} unknown number{'s' if n != 1 else ''} called "
            f"in the last {hours:g} hours. Ask who called to hear them.")

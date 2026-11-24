"""Follow-through: open commitments climb a ladder until they're dealt with (NORA_ASSIST_PLAN B).

"Submit the assignment" sat overdue for 51 days and came up only when the user
asked for the agenda. A dated task now climbs, one rung at a time:

  rung 0  due today            the agenda and the session briefing (as before)
  rung 1  1+ days overdue      one phone notification a day, after `nudge_from`,
                               every late task in one message
  rung 2  3+ days overdue      raised in the nightly check-in
  rung 3  7+ days overdue,     NORA rings the phone (`calls.ring(asked=False)`,
          nudged 3+ times      so quiet hours hold), at most every 3 days

The **nightly check-in** at `checkin_at` says what's due tomorrow, what's late,
and the alarm if NORA set one. It's said on the laptop when the user is home
and sent to the phone otherwise. Replies are ordinary turns: "done with the
assignment" (close_task), "push it to Friday" (push_task), "drop it"
(drop_task). Only one check-in a night, and none when there's nothing to say.

Every rung is written to `nudges` (store migration 10) before it goes out, so
a restart never repeats one.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

from nora import days, store, task_ledger
from nora.config import get_config

logger = logging.getLogger("nora.follow_through")

_DEFAULTS = {
    "enabled": True,
    "nudge_from": "10:00",
    "checkin_at": "21:00",
    "checkin_until": "23:00",
    "checkin_after_days": 3,
    "ring_after_days": 7,
    "ring_after_nudges": 3,
    "ring_every_days": 3,
}


def _cfg() -> dict:
    return {**_DEFAULTS, **(get_config().get("follow_through", {}) or {})}


def _hhmm(text: str) -> tuple[int, int]:
    h, m = str(text).split(":")
    return int(h), int(m)


def _at(now: datetime, text: str) -> datetime:
    h, m = _hhmm(text)
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


# ── the nudge ledger ─────────────────────────────────────────────────────────

def _mark(task_id: str, rung: int, at: float | None = None) -> None:
    with store.transaction() as conn:
        conn.execute("INSERT INTO nudges (task_id, rung, at) VALUES (?, ?, ?)",
                     (task_id, rung, at if at is not None else time.time()))


def _last(task_id: str, rung: int) -> float:
    rows = store.query("SELECT MAX(at) AS at FROM nudges WHERE task_id = ? AND rung = ?",
                       (task_id, rung))
    return float(rows[0]["at"] or 0) if rows else 0.0


def _count(task_id: str) -> int:
    rows = store.query("SELECT COUNT(*) AS n FROM nudges WHERE task_id = ? AND rung >= 1",
                       (task_id,))
    return int(rows[0]["n"]) if rows else 0


def _day_start(now: datetime) -> float:
    return datetime.combine(now.date(), datetime.min.time()).timestamp()


# ── what's late ──────────────────────────────────────────────────────────────

def late(today: date) -> list[tuple[dict, int]]:
    """Open tasks past their day, with how many days late, latest-overdue first."""
    out = []
    for t in task_ledger.due_by(today - timedelta(days=1)):
        out.append((t, (today - date.fromisoformat(t["due_on"])).days))
    return sorted(out, key=lambda p: -p[1])


def _late_phrase(title: str, n: int) -> str:
    return f"{title} ({n} day{'s' if n != 1 else ''} late)"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


# ── rungs ────────────────────────────────────────────────────────────────────

def nudge(now: datetime) -> str | None:
    """Rung 1: one notification a day naming every late task. Returns what was sent."""
    from nora import assist, calls
    if now < _at(now, _cfg()["nudge_from"]) or calls.quiet_now(now):
        return None
    today_start = _day_start(now)
    fresh = [(t, n) for t, n in late(now.date()) if _last(t["id"], 1) < today_start]
    if not fresh:
        return None
    for t, _ in fresh:
        _mark(t["id"], 1, now.timestamp())
    text = "Still open: " + _join([_late_phrase(t["title"], n) for t, n in fresh[:4]])
    if len(fresh) > 4:
        text += f", and {len(fresh) - 4} more"
    text += ". Tell me when it's done, or say push it to another day."
    assist.tell(text, kind="reminder")
    return text


def ring(now: datetime) -> str | None:
    """Rung 3: ring about the longest-late task that has been nudged enough."""
    from nora import calls
    cfg = _cfg()
    for t, n in late(now.date()):
        if n < cfg["ring_after_days"] or _count(t["id"]) < cfg["ring_after_nudges"]:
            continue
        if now.timestamp() - _last(t["id"], 3) < cfg["ring_every_days"] * 86400:
            continue
        _mark(t["id"], 3, now.timestamp())
        reason = f"{t['title']} is {n} days late"
        say = (f"Calling about {t['title']}. It was due {n} days ago and it's still open. "
               "Is it done, should I move it, or drop it?")
        if calls.ring(reason, say=say, asked=False):
            return reason
        return None
    return None


def checkin_text(now: datetime) -> str | None:
    """What the nightly check-in says, or None when there's nothing worth a word."""
    from nora import calendar_rules
    today = now.date()
    tomorrow = today + timedelta(days=1)
    parts: list[str] = []
    due = [t["title"] for t in task_ledger.due_that_day(tomorrow)]
    if due:
        parts.append(f"Due tomorrow: {_join(due[:4])}.")
    today_due = [t["title"] for t in task_ledger.due_that_day(today)]
    if today_due:
        parts.append(f"Still open from today: {_join(today_due[:3])}.")
    over = [(t, n) for t, n in late(today) if n >= 1]
    if over:
        parts.append("Late: " + _join([_late_phrase(t["title"], n) for t, n in over[:4]]) + ".")
    if not parts:
        return None
    try:
        alarm = next((a for a in calendar_rules.alarms() if a.get("id") == tomorrow.isoformat()), None)
    except Exception:
        alarm = None
    if alarm:
        clock = datetime.fromtimestamp(alarm["at_ms"] / 1000).strftime("%-I:%M %p").lower()
        parts.append(f"Your alarm's set for {clock}.")
    parts.append("For each one: done, push it, or drop it?")
    return "Quick check before bed. " + " ".join(parts)


def checkin(now: datetime) -> str | None:
    """The nightly check-in, once a night inside its window."""
    from nora import assist
    cfg = _cfg()
    if not (_at(now, cfg["checkin_at"]) <= now < _at(now, cfg["checkin_until"])):
        return None
    key = f"checkin:{now.date().isoformat()}"
    if _last(key, 2):
        return None
    _mark(key, 2, now.timestamp())
    text = checkin_text(now)
    if text is None:
        return None
    for t, n in late(now.date()):
        if n >= cfg["checkin_after_days"]:
            _mark(t["id"], 2, now.timestamp())
    assist.tell(text, kind="checkin", say_at_home=True)
    return text


def check(now: datetime | None = None) -> None:
    """The assist tick's entry: every rung, in order."""
    if not _cfg()["enabled"]:
        return
    now = now or datetime.now()
    for rung in (nudge, ring, checkin):
        try:
            rung(now)
        except Exception as e:
            logger.warning("follow-through %s failed: %s", rung.__name__, e)


def briefing_line(today: date | None = None) -> str | None:
    """For the session briefing: what's late, plainly."""
    today = today or date.today()
    over = late(today)
    if not over:
        return None
    return "Overdue: " + _join([_late_phrase(t["title"], n) for t, n in over[:3]]) + "."


def push(task: dict, when: date, today: date | None = None) -> str:
    today = today or date.today()
    task_ledger.update_task(task["id"], due_on=when)
    task_ledger.append_log(task["id"], f"pushed to {when.isoformat()}")
    return f"Moved {task['title']} to {days.say(when, today)}."

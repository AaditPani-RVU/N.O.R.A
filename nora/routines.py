"""Routines: things done every day that NORA keeps a log of (NORA_ASSIST_PLAN C).

"I fed Milo" logs it; "did I feed Milo?" answers from the log; "remind me if
I haven't fed Milo by 9 am and 9 pm" makes it a routine, and a slot with
nothing logged by `grace_min` after its time gets one nudge to the phone.

The log and the question have to meet without a model call, so a routine's key
is its verb stem plus its object, with filler words dropped:

    "fed Milo" / "feed Milo" / "feeding Milo"   → "feed milo"
    "walked the dog"                             → "walk dog"
    "gave Milo his meds"                         → "give milo meds"

A log counts for a slot from three hours before it (an early breakfast still
counts as the morning feed) or from the previous slot, whichever is later.

Tables `routines` and `routine_log` (store migration 11); each slot's nudge
goes in `nudges` as "routine:<key>:<date>T<hh:mm>", once.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta

from nora import store
from nora.config import get_config

logger = logging.getLogger("nora.routines")

_STEMS = {
    "fed": "feed", "feeding": "feed", "feeds": "feed",
    "walked": "walk", "walking": "walk", "walks": "walk",
    "gave": "give", "given": "give", "giving": "give", "gives": "give",
    "took": "take", "taken": "take", "taking": "take", "takes": "take",
    "watered": "water", "watering": "water",
    "brushed": "brush", "brushing": "brush",
    "bathed": "bathe", "bathing": "bathe",
    "cleaned": "clean", "cleaning": "clean",
    "did": "do", "done": "do", "doing": "do",
    "had": "have", "having": "have", "ate": "eat", "eaten": "eat", "eating": "eat",
}
_PAST = {"feed": "fed", "walk": "walked", "give": "gave", "take": "took", "do": "did",
         "have": "had", "eat": "ate"}
_FILLER = {"the", "a", "an", "my", "his", "her", "their", "our", "its", "him", "them",
           "just", "already", "today", "tonight", "this", "morning", "evening", "yet", "been"}
_DEFAULT_GRACE = 45


def _cfg() -> dict:
    return get_config().get("routines", {}) or {}


def key(what: str) -> str:
    words = re.findall(r"[a-z0-9']+", (what or "").lower())
    out = [w.replace("'s", "") for w in words if w.replace("'s", "") not in _FILLER
           and w not in ("has", "was", "is", "got")]
    # The question's frame: "did I feed", "when did I last walk".
    while len(out) > 1 and out[0] in ("i", "we", "did", "do", "does", "have", "when", "last"):
        out.pop(0)
    out = [w for w in out if w not in ("i", "we")]
    if not out:
        return ""
    verbs = set(_STEMS.values())
    out[0] = _STEMS.get(out[0], out[0])
    out[-1] = _STEMS.get(out[-1], out[-1])
    # "Milo was fed" / "has Milo been fed": the verb came last.
    if len(out) >= 2 and out[-1] in verbs and out[0] not in verbs:
        out = [out[-1], *out[:-1]]
    return " ".join(out)


def _label(k: str) -> str:
    """'feed milo' → 'fed Milo', for saying back."""
    words = k.split()
    if not words:
        return k
    verb = _PAST.get(words[0], words[0] + "ed" if not words[0].endswith("e") else words[0] + "d")
    rest = [w.capitalize() if w not in ("dog", "cat", "meds", "plants", "medicine") else w
            for w in words[1:]]
    return " ".join([verb, *rest])


def _clock(ts: float, now: datetime) -> str:
    at = datetime.fromtimestamp(ts)
    clock = at.strftime("%-I:%M %p").replace(":00 ", " ").lower()
    if at.date() == now.date():
        part = "this morning" if at.hour < 12 else "this afternoon" if at.hour < 17 else "this evening"
        return f"{clock} {part}" if at.hour < 21 else f"{clock} tonight"
    if at.date() == now.date() - timedelta(days=1):
        return f"{clock} yesterday"
    return at.strftime("%-I:%M %p on %A").lower().replace(" on ", " on ").capitalize()


# ── log ──────────────────────────────────────────────────────────────────────

def log(what: str, at: float | None = None) -> str:
    k = key(what)
    if not k:
        return "What did you do?"
    with store.transaction() as conn:
        conn.execute("INSERT INTO routine_log (name, at, note) VALUES (?, ?, ?)",
                     (k, at if at is not None else time.time(), what.strip()[:200]))
    return f"Noted: {_label(k)}."


def last(what: str) -> float | None:
    k = key(what)
    rows = store.query("SELECT MAX(at) AS at FROM routine_log WHERE name = ?", (k,))
    return float(rows[0]["at"]) if rows and rows[0]["at"] else None


def answer(what: str, now: datetime | None = None) -> str:
    now = now or datetime.now()
    k = key(what)
    at = last(what)
    if at is None:
        return f"I don't have it logged that you {_label(k)}."
    said = _label(k)
    if datetime.fromtimestamp(at).date() == now.date():
        return f"Yes, you {said} at {_clock(at, now)}."
    return f"Not today. Last time you {said} was {_clock(at, now)}."


# ── routines ─────────────────────────────────────────────────────────────────

def parse_times(text: str) -> list[tuple[int, int]]:
    """'9 am and 9 pm', '8:30am, 7pm', 'morning and evening'."""
    from nora import scheduler
    out: list[tuple[int, int]] = []
    t = (text or "").lower()
    for word, hm in (("morning", (8, 0)), ("noon", (12, 0)), ("afternoon", (15, 0)),
                     ("evening", (19, 0)), ("night", (21, 0))):
        if re.search(rf"\b{word}\b", t) and not re.search(r"\d", t):
            out.append(hm)
    for piece in re.split(r",|\band\b|&", t):
        if re.search(r"\d", piece):
            hm = scheduler._parse_clock(piece.strip())
            if hm:
                out.append(hm)
    return sorted(set(out))


def add(what: str, times: str, grace_min: int | None = None) -> str:
    k = key(what)
    slots = parse_times(times)
    if not k or not slots:
        return "Tell me what and when, like: feed Milo at 8 am and 8 pm."
    grace = int(grace_min if grace_min is not None else _cfg().get("grace_min", _DEFAULT_GRACE))
    with store.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO routines (name, times, grace_min, created_at) "
                     "VALUES (?, ?, ?, ?)", (k, json.dumps(slots), grace, time.time()))
    said = " and ".join(datetime(2000, 1, 1, h, m).strftime("%-I:%M %p").replace(":00 ", " ").lower()
                        for h, m in slots)
    return f"Okay. If you haven't {_label(k)} by {grace} minutes after {said}, I'll remind you."


def remove(what: str) -> str:
    k = key(what)
    with store.transaction() as conn:
        n = conn.execute("DELETE FROM routines WHERE name = ?", (k,)).rowcount
    return f"Stopped the {k} reminders." if n else f"There's no routine called {k}."


def all_routines() -> list[dict]:
    try:
        rows = store.query("SELECT name, times, grace_min FROM routines ORDER BY name")
    except Exception:
        return []
    return [{"name": r["name"], "times": [tuple(x) for x in json.loads(r["times"])],
             "grace_min": int(r["grace_min"])} for r in rows]


def listing() -> str:
    rs = all_routines()
    if not rs:
        return "No routines yet. Try: remind me if I haven't fed Milo by 9 am and 9 pm."
    parts = []
    for r in rs:
        said = " and ".join(datetime(2000, 1, 1, h, m).strftime("%-I:%M %p").replace(":00 ", " ").lower()
                            for h, m in r["times"])
        parts.append(f"{r['name']} at {said}")
    return "Routines: " + "; ".join(parts) + "."


def due_slots(now: datetime) -> list[tuple[dict, datetime]]:
    """Today's slots past their grace with nothing logged in their window."""
    out = []
    for r in all_routines():
        slots = [now.replace(hour=h, minute=m, second=0, microsecond=0) for h, m in r["times"]]
        for i, slot in enumerate(slots):
            if now < slot + timedelta(minutes=r["grace_min"]):
                continue
            # Not slots long gone: a 08:00 slot isn't nagged about at 23:00.
            if now > slot + timedelta(hours=4):
                continue
            start = slot - timedelta(hours=3)
            if i > 0:
                start = max(start, slots[i - 1])
            rows = store.query("SELECT 1 FROM routine_log WHERE name = ? AND at >= ? LIMIT 1",
                               (r["name"], start.timestamp()))
            if not rows:
                out.append((r, slot))
    return out


def check(now: datetime | None = None) -> None:
    from nora import assist, calls, follow_through
    now = now or datetime.now()
    if calls.quiet_now(now):
        return
    for r, slot in due_slots(now):
        mark = f"routine:{r['name']}:{slot.strftime('%Y-%m-%dT%H:%M')}"
        if follow_through._last(mark, 1):
            continue
        follow_through._mark(mark, 1, now.timestamp())
        clock = slot.strftime("%-I:%M %p").replace(":00 ", " ").lower()
        assist.tell(f"Nothing logged for {r['name']} since the {clock} slot. "
                    f"Tell me when it's done: say \"I {_label(r['name'])}\".",
                    kind="reminder", say_at_home=True)


def vocabulary() -> list[str]:
    """Names in routines ("Milo"), for the recognisers."""
    names: list[str] = []
    for r in all_routines():
        names.extend(w.capitalize() for w in r["name"].split()[1:]
                     if w not in ("dog", "cat", "meds", "plants", "medicine", "water"))
    return names

"""Day words — "tomorrow", "on Friday", "by the 5th" — resolved to dates.

A task said on Tuesday as "submit the assignment tomorrow" and asked about on
Wednesday as "what do I need to do today" only matches if "tomorrow" became a
date when it was said. So the word is resolved once, at the moment it is
heard, against the local calendar, and the date is what is stored.

Deliberately strict: anything not recognised is left alone (and stays in the
task's title) rather than guessed at. `dateutil` is not used for this — it
reads "tomorrow" as nothing and "the 5th" as today, and a due date that is
silently wrong is worse than none. Every reply that sets a date says the
resolved day back ("due Friday, 2 October"), so a wrong reading is heard.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# Full names only: "sat", "wed" and "sun" are ordinary words at the end of a sentence.
_WEEKDAY = {d: i for i, d in enumerate(WEEKDAYS)}
_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")
_MONTH = {
    **{m: i + 1 for i, m in enumerate(_MONTHS)},
    **{m[:3]: i + 1 for i, m in enumerate(_MONTHS)},
    "sept": 9,
}
_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
            "seven": 7, "eight": 8, "nine": 9, "ten": 10, "a": 1}

_WD = "|".join(sorted(_WEEKDAY, key=len, reverse=True))
_MON = "|".join(sorted(_MONTH, key=len, reverse=True))
_ORD = r"(?P<{0}>\d{{1,2}})(?:st|nd|rd|th)?"

# One day expression, without the "on"/"by" in front of it.
_DAY = (
    r"(?P<rel>today|tonight|this\s+(?:morning|afternoon|evening)|end\s+of\s+(?:the\s+)?day"
    r"|tomorrow(?:\s+(?:morning|afternoon|evening|night))?|tmrw|tmr"
    r"|(?:the\s+)?day\s+after\s+tomorrow)"
    rf"|(?P<which>this\s+|next\s+|coming\s+)?(?P<wd>{_WD})"
    rf"(?:\s+(?:morning|afternoon|evening|night))?"
    rf"|(?:the\s+)?{_ORD.format('d1')}\s+(?:of\s+)?(?P<m1>{_MON})"
    rf"|(?P<m2>{_MON})\s+(?:the\s+)?{_ORD.format('d2')}"
    rf"|the\s+{_ORD.format('d3')}"
    r"|in\s+(?P<n>\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|a)\s+(?P<unit>days?|weeks?)"
    r"|(?P<iso>\d{4}-\d{2}-\d{2})"
)
_DAY_RE = re.compile(rf"^(?:{_DAY})$", re.I)
# A day expression at the end of a sentence, with what usually introduces it.
_TAIL_RE = re.compile(
    rf"^(?P<rest>.*?)[\s,]+(?:(?:is\s+)?(?:due\s+)?(?:on|by|for|before|until|due)\s+)?(?:{_DAY})\s*$",
    re.I)
# ... or at the start: "tomorrow I have to ..." / "by Friday, finish ..."
_HEAD_RE = re.compile(rf"^(?:(?:on|by|before)\s+)?(?:{_DAY})[\s,]+(?P<rest>.+)$", re.I)


def _from_match(m: re.Match, today: date) -> date | None:
    g = m.groupdict()
    if g.get("rel"):
        rel = g["rel"].lower()
        if "after" in rel:
            return today + timedelta(days=2)
        if rel.startswith(("tomorrow", "tmr")):
            return today + timedelta(days=1)
        return today
    if g.get("wd"):
        target = _WEEKDAY[g["wd"].lower()]
        ahead = (target - today.weekday()) % 7
        which = (g.get("which") or "").strip().lower()
        # "this Friday" said on a Friday is today; "Friday" / "on Friday" /
        # "next Friday" is the next one to come. "Next Friday" meaning the
        # one after that is a real reading too; the reply says the date.
        if ahead == 0 and which != "this":
            ahead = 7
        return today + timedelta(days=ahead)
    if g.get("iso"):
        try:
            return date.fromisoformat(g["iso"])
        except ValueError:
            return None
    if g.get("n"):
        raw = g["n"].lower()
        n = int(raw) if raw.isdigit() else _NUMBERS[raw]
        return today + timedelta(days=n * (7 if g["unit"].lower().startswith("week") else 1))
    day = g.get("d1") or g.get("d2") or g.get("d3")
    if day:
        month_word = g.get("m1") or g.get("m2")
        return _day_of(int(day), _MONTH[month_word.lower()] if month_word else None, today)
    return None


def _day_of(day: int, month: int | None, today: date) -> date | None:
    """The next such date that isn't in the past."""
    if month is None:
        # "the 5th": this month if it hasn't gone, else next month.
        for offset in range(0, 3):
            y, m = divmod(today.month - 1 + offset, 12)
            try:
                d = date(today.year + y, m + 1, day)
            except ValueError:
                continue
            if d >= today:
                return d
        return None
    for year in (today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            return None
        if d >= today:
            return d
    return None


def parse(text: str, today: date | None = None) -> date | None:
    """The date `text` names, if all of it is one day expression."""
    today = today or date.today()
    t = re.sub(r"^(?:on|by|for|before|due)\s+", "", (text or "").strip().rstrip(".!?"), flags=re.I)
    m = _DAY_RE.match(t)
    return _from_match(m, today) if m else None


def split(text: str, today: date | None = None) -> tuple[str, date | None]:
    """Take a trailing or leading day expression off `text`.

    "submit the assignment tomorrow" → ("submit the assignment", tomorrow).
    Without one, the text comes back unchanged with None.
    """
    today = today or date.today()
    t = (text or "").strip().rstrip(".!?")
    for rx in (_TAIL_RE, _HEAD_RE):
        m = rx.match(t)
        if m:
            when = _from_match(m, today)
            rest = m.group("rest").strip(" ,")
            if when is not None and rest:
                return rest, when
    return t, None


def say(d: date, today: date | None = None) -> str:
    """A day as said aloud, relative when that is clearer."""
    today = today or date.today()
    delta = (d - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    name = f"{WEEKDAYS[d.weekday()].capitalize()}, {d.day} {_MONTHS[d.month - 1].capitalize()}"
    if 1 < delta < 7:
        return name
    if delta < 0:
        return f"{name}, {-delta} days ago"
    return name if d.year == today.year else f"{name} {d.year}"

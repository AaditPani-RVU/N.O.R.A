"""Phrase tables for the fast path: one table per family (Sharp Phase B).

Each family is a list of phrasings and one builder. A phrasing is a regex
matched against the whole normalised utterance (fast_path anchors it), and
its named groups are the builder's inputs: `day`, `place`, `what`, `time`,
`n`/`unit`. A new way of saying something is a new line in a table, not new
code. The families come from the eval set's misses (evals/, Phase A): the
everyday questions that cost a model call, or got the wrong answer, before.

A builder returns None to step aside, and the turn goes on to the rules
after it and then the model: a phone-only family with no phone connected,
a title it can't find, a "place" that is plainly not one.
"""
from __future__ import annotations

import re
from typing import Callable

from nora.days import _TOMORROW

# ── shared pieces ────────────────────────────────────────────────────────────

DAY = (r"(?P<day>today|tonight|" + _TOMORROW + r"|yesterday|this\s+week|next\s+week"
       r"|this\s+(?:morning|afternoon|evening)|(?:on\s+)?(?:monday|tuesday|wednesday|thursday"
       r"|friday|saturday|sunday))")
DAY2 = DAY.replace("?P<day>", "?P<day2>")
# "my home address is ...", "my college is at ...": a place to save, not a fact.
_NOT_PLACE_FACT = r"(?!(?:[\w']+\s+){0,2}(?:address\b|is\s+at\s))"
WHATS = r"(?:what(?:'?s|\s+is|\s+are)|whats|what\s+do\s+i\s+have|what\s+have\s+i\s+got|tell\s+me\s+what(?:'?s|\s+is))"
MY = r"(?:my\s+|the\s+|our\s+)?"
CAL = r"(?:calendar|schedule|diary)"
PLACE = r"(?P<place>[\w .,'-]{2,60}?)"
TIME = r"(?P<time>\d{1,2}(?:[:.]\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?|noon|midnight)"
N = (r"(?P<n>\d+(?:\.\d+)?|an?|one|two|three|four|five|six|seven|eight|nine|ten|"
     r"fifteen|twenty|thirty|forty-five|ninety)")
UNIT = r"(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?)"


def _day(m: re.Match, default: str = "today") -> str:
    g = m.groupdict()
    d = (g.get("day") or g.get("day2") or default).lower()
    return re.sub(r"^on\s+", "", re.sub(r"\s+", " ", d))


# ── families ────────────────────────────────────────────────────────────────
# (name, phrasings, builder). Builders get the match and the fast_path module
# (for _intent, _chat_varied, _device_offers, _from_phone).

def _families(fp) -> list[tuple[str, list[str], Callable]]:
    I, chat = fp._intent, fp._chat_varied

    def phone_status(m):
        # "what's my battery" said to the phone means the phone's; said to the
        # laptop, without "phone", it means the laptop's.
        named = m.groupdict().get("phone") or m.groupdict().get("phone2")
        if fp._device_offers("device.status") and (named or fp._from_phone("device.status")):
            return I("phone status", "device.status", {})
        return None if named else I("get system info", "get_system_info", {})

    def calendar(m):
        day = _day(m)
        if day == "this week":
            return I("calendar this week", "calendar_week", {})
        return I("check calendar", "check_calendar", {"when": day})

    def weather(m):
        place = (m.groupdict().get("place") or "").strip(" ,.")
        # "rain in Tokyo tomorrow": the day may land on either side of the place.
        tail = re.search(r"\s+" + DAY2 + r"$", place, re.I)
        if tail:
            place = place[:tail.start()]
        place = re.sub(r"\s+(?:now|right\s+now|currently|at\s+the\s+moment)$", "", place, flags=re.I)
        day = (m.groupdict().get("day") or m.groupdict().get("day2")
               or (tail.group("day2") if tail else "")).lower()
        params = {"location": place} if place else {}
        if day and day not in ("today", "tonight"):
            params["day"] = re.sub(r"^on\s+", "", day)
        return I("weather", "get_weather", params)

    def place(m):
        p = re.sub(r"\s+(?:on|in)\s+(?:the\s+)?(?:dashboard|globe|map|screen)$", "", m.group("place").strip(" ,."),
                   flags=re.I)
        if _NOT_A_PLACE.match(p):
            return None
        return I(f"show {p}", "show_location", {"location": p})

    def add_event(m):
        return _event(fp, m.group("rest"))

    def alarm(m):
        if not fp._device_offers("phone.set_alarm"):
            return None
        hm = _clock(m.group("time"))
        if hm is None:
            return None
        return I("set alarm", "phone.set_alarm", {"hour": hm[0], "minute": hm[1]})

    def timer(m):
        if not fp._device_offers("phone.set_timer"):
            return None
        from nora.commands.notifications import duration_minutes
        minutes = duration_minutes(f"{m.group('n')} {m.group('unit')}")
        if not minutes:
            return None
        return I("set timer", "phone.set_timer", {"seconds": int(round(minutes * 60))})

    def ask_claude(m):
        q = (m.groupdict().get("q") or m.groupdict().get("pre") or "").strip(" ,.")
        if len(q) < 4:
            return None
        return I("ask claude", "ask_claude", {"question": q})

    def remember_fact(m):
        fact = m.group("fact").strip(" ,.")
        return I("remember", "inject_knowledge", {"text": fact})

    def playing_now(m):
        if fp._device_offers("phone.media_control") and (
                fp._from_phone("phone.media_control") or re.search(r"\bphone\b", m.string, re.I)):
            return I("what's playing", "phone.media_control", {"action": "status"})
        return I("now playing", "now_playing", {})

    return [
        # ── date (get_time says the date too) ─────────────────────────────
        ("date", [
            WHATS + r"\s+(?:the\s+|today'?s\s+)?date(?:\s+today)?",
            WHATS + r"\s+today(?:'?s\s+date)?",
            r"what\s+day\s+is\s+(?:it|today)(?:\s+today)?",
            WHATS + r"\s+(?:the\s+)?day(?:\s+today)?",
            r"(?:today'?s\s+)?date(?:\s+today)?",
        ], lambda m: I("get current time", "get_time", {})),

        # ── the phone's battery, or the laptop's ──────────────────────────
        ("battery", [
            r"(?:" + WHATS + r"\s+|how(?:'?s|\s+is)\s+|check\s+)?" + MY + r"(?P<phone>phone(?:'?s)?\s+)?"
            r"battery(?:\s+(?:level|percentage|status|life|looking\s+like))?",
            r"how\s+much\s+(?:battery|charge)\s+(?:do\s+i\s+have|is\s+left|have\s+i\s+got)(?:\s+on\s+my\s+(?P<phone2>phone))?",
        ], phone_status),

        # ── calendar ──────────────────────────────────────────────────────
        ("calendar", [
            r"(?:" + WHATS + r"|is\s+there\s+anything|anything|do\s+i\s+have\s+anything)\s+(?:on|in)\s+" + MY
            + CAL + r"(?:\s+for)?(?:\s+" + DAY + r")?",
            r"(?:what\s+(?:events|meetings)\s+do\s+i\s+have|what\s+are\s+(?:the|my)\s+(?:events|meetings))"
            r"(?:\s+(?:on|in)\s+" + MY + CAL + r")?(?:\s+for)?(?:\s+" + DAY + r")?(?:\s+on\s+" + MY + CAL + r")?",
            r"(?:try\s+)?(?:check(?:ing)?|show(?:\s+me)?|read|open|look\s+at|go\s+through)\s+" + MY + CAL
            + r"(?:\s+for)?(?:\s+" + DAY + r")?(?:[?.,]\s*(?:what\s+do\s+i\s+have|anything)(?:\s+" + DAY2 + r")?)?",
            r"(?:tell\s+me\s+)?(?:about\s+)?what(?:'?s|\s+is)\s+on\s+" + MY + CAL + r"(?:\s+" + DAY + r")?",
            r"(?:" + WHATS + r"\s+)?" + MY + CAL + r"\s+(?:for\s+)?" + DAY,
        ], calendar),

        ("calendar-add", [
            r"(?:add|create|put|schedule|make|set\s+up)\s+(?:an?\s+|a\s+new\s+)?(?:calendar\s+)?"
            r"(?:event|appointment|meeting)(?P<rest>\b.*)",
        ], add_event),

        # ── is something broken? ──────────────────────────────────────────
        ("diagnose", [
            r"(?:what(?:'?s|\s+is)\s+wrong\s+with|diagnose|is\s+something\s+wrong\s+with|why\s+(?:can'?t|cannot|won'?t)\s+you\s+"
            r"(?:check|see|read|open))\s+" + MY + r"(?:calendar|e-?mail|gmail|spotify|music|phone|weather)"
            r"(?:\s+(?:please\s+)?(?:diagnose|check)(?:\s+it)?)?",
            r"(?:what(?:'?s|\s+is)\s+broken|run\s+(?:a\s+)?(?:health\s+check|diagnostics?)|are\s+you\s+(?:ok|okay|healthy))",
        ], lambda m: I("health check", "health_check", {})),

        # ── weather ───────────────────────────────────────────────────────
        ("weather", [
            r"(?:" + WHATS + r"|how(?:'?s|\s+is))\s+(?:the\s+)?weather(?:\s+(?:going\s+to\s+be\s+)?like)?"
            r"(?:\s+" + DAY2 + r")?(?:\s+(?:in|at|for)\s+" + PLACE + r")?"
            r"(?:\s+(?:now|right\s+now|this\s+morning|this\s+evening|outside|" + DAY + r"))?(?:\s+like)?",
            WHATS + r"\s+(?:the\s+)?(?:current\s+)?temperature(?:\s+(?:in|at|outside)\s+" + PLACE + r")?(?:\s+(?:now|today))?",
            r"(?:is\s+it|will\s+it|is\s+it\s+going\s+to|it'?s\s+going\s+to)\s+(?:rain|snow|be\s+(?:hot|cold|sunny|windy))"
            r"(?:\s+(?:later|" + DAY + r"))?(?:\s+in\s+" + PLACE + r")?",
            r"how\s+(?:hot|cold|warm)\s+is\s+it(?:\s+(?:outside|today|now))?(?:\s+in\s+" + PLACE + r")?",
            r"(?:the\s+)?weather(?:\s+(?:in|for)\s+" + PLACE + r")?(?:\s+today)?",
        ], weather),

        # ── the screen ────────────────────────────────────────────────────
        ("screen", [
            r"(?:read|describe|look\s+at)\s+" + MY + r"screen",
            r"(?:tell\s+me\s+)?what(?:'?s|\s+is)\s+(?:currently\s+)?(?:going\s+on\s+|happening\s+)?on\s+" + MY + r"screen"
            r"(?:\s+(?:currently|right\s+now|now))*",
            r"what\s+do\s+you\s+see\s+on\s+" + MY + r"screen",
        ], lambda m: I("read screen", "read_screen", {})),

        # ── email ─────────────────────────────────────────────────────────
        ("email-important", [
            r"(?:is\s+there|are\s+there|do\s+i\s+have|any(?:thing)?)\s+(?:any\s+)?important\s+(?:e-?mails?|mails?|messages\s+in\s+my\s+inbox)"
            r"(?:\s+i\s+should\s+(?:prioriti[sz]e|read|look\s+at))?",
            r"(?:in\s+my\s+(?:e-?mail|inbox|gmail)[,]?\s+)?(?:do\s+you\s+see\s+)?anything\s+important(?:\s+(?:in\s+my\s+(?:e-?mail|inbox)|"
            r"that\s+i\s+haven'?t\s+read(?:\s+(?:till|until|so\s+far|yet)(?:\s+now)?)?))+",
        ], lambda m: I("important email", "gmail_important", {})),
        ("email", [
            r"(?:check|read|open|go\s+through)\s+" + MY + r"(?:e-?mails?|mails?|inbox|gmail)",
            r"(?:do\s+i\s+have\s+(?:any\s+)?|any\s+)(?:new\s+|unread\s+)?(?:e-?mails?|mails?)",
        ], lambda m: I("check email", "check_email", {})),

        # ── system ────────────────────────────────────────────────────────
        ("system", [
            r"show\s+(?:me\s+)?(?:the\s+|my\s+)?(?:running\s+)?processes",
        ], lambda m: I("show processes", "show_processes", {})),
        ("busy", [
            r"what(?:'?s|\s+is)\s+(?:eating|using|hogging|maxing)\s+" + MY + r"(?:cpu|processor)",
            r"why\s+is\s+" + MY + r"(?:machine|computer|laptop|pc|system)\s+(?:so\s+|being\s+)?slow",
            r"why\s+is\s+" + MY + r"fan\s+(?:so\s+)?(?:loud|noisy|spinning)",
        ], lambda m: I("why busy", "why_busy", {})),
        ("network", [
            r"(?:who(?:'?s|\s+is)|what(?:'?s|\s+is))\s+using\s+(?:the\s+most\s+|my\s+|all\s+my\s+)?(?:network|bandwidth|internet|data)",
        ], lambda m: I("top talkers", "top_talkers", {})),
        ("denoise", [
            r"(?:enable|turn\s+on|start)\s+(?:mic(?:rophone)?\s+)?(?:denoising|noise\s+(?:suppression|cancell?ation))",
        ], lambda m: I("denoise mic", "denoise_mic", {})),

        # ── the laptop's surfaces ─────────────────────────────────────────
        ("desktop", [r"show\s+(?:me\s+)?" + MY + r"desktop"], lambda m: I("show desktop", "show_desktop", {})),
        ("memory", [r"show\s+(?:me\s+)?(?:your\s+)?memor(?:y|ies)"], lambda m: I("show memory", "show_memory", {})),
        ("patterns", [r"show\s+(?:me\s+)?(?:my\s+)?(?:habits|patterns)"], lambda m: I("show patterns", "show_patterns", {})),
        ("buttons", [r"list\s+(?:the\s+|all\s+the\s+)?buttons?\s+(?:in|on)\s+(?:the\s+|this\s+)?window"],
         lambda m: I("list buttons", "list_buttons_in_window", {})),
        ("focus", [r"(?:focus\s+mode\s+on|(?:turn\s+on|start|enter|begin)\s+focus\s+mode)"],
         lambda m: I("focus mode", "focus_mode", {})),
        ("typing", [r"type\s+(?!of\b)(?P<what>(?:(?!\s(?:in|into)\s+(?:the|my)\s).){1,200})"],
         lambda m: I("type", "type_into_focused", {"text": m.group("what").strip()})),

        # ── facts to keep ─────────────────────────────────────────────────
        ("remember-fact", [
            # "my home address is ..." is a saved place (save_place), not a fact.
            r"(?:remember|don'?t\s+forget)\s+(?:that\s+)?(?P<fact>my\s+" + _NOT_PLACE_FACT + r".{3,200})",
            r"(?P<fact>my\s+" + _NOT_PLACE_FACT + r".{3,200}?)[.,!]*\s+(?:could|can|will)\s+you\s+remember\s+(?:that|this|it)",
        ], remember_fact),

        # ── the globe ─────────────────────────────────────────────────────
        ("globe", [
            r"show\s+me\s+" + r"(?P<place>.{2,60})",
            r"(?:take\s+me\s+to|fly\s+(?:me\s+)?to)\s+(?P<place>.{2,60})\s+on\s+the\s+(?:globe|map|dashboard)",
        ], place),

        # ── alarms and timers on the phone ────────────────────────────────
        ("alarm", [
            r"(?:set|make)\s+(?:an?\s+|my\s+)?alarm\s+(?:for|at|to\s+(?:wake\s+me\s+(?:up\s+)?|wake\s+up\s+)?at)\s+" + TIME
            + r"(?:\s+(?:tomorrow|today|in\s+the\s+morning))?",
            r"wake\s+me\s+(?:up\s+)?at\s+" + TIME + r"(?:\s+(?:tomorrow|in\s+the\s+morning))?",
        ], alarm),
        ("timer", [
            r"(?:set|start)\s+(?:an?\s+)?timer\s+(?:for\s+)?" + N + r"\s+" + UNIT,
            N + r"\s+" + UNIT + r"\s+timer",
        ], timer),

        # ── what's playing ────────────────────────────────────────────────
        ("playing", [
            r"what\s+(?:song|track|music)\s+(?:am\s+i\s+playing|is\s+(?:this|playing|on))(?:\s+right\s+now|\s+now)?",
            r"what(?:'?s|\s+is)\s+playing(?:\s+(?:right\s+)?now)?(?:\s+on\s+my\s+phone)?",
        ], playing_now),
        ("pause", [
            r"(?:try\s+)?paus(?:e|ing)\s+(?:the\s+)?(?:song|music|track)(?:\s+(?:i(?:'m|\s+am)\s+playing|that'?s\s+playing))?"
            r"(?:\s+on\s+spotify)?",
        ], lambda m: I("pause music", "pause_music", {})),

        # ── asking Claude ─────────────────────────────────────────────────
        ("claude", [
            r"(?:.*?\s)?ask\s+claude\s+(?:to\s+|about\s+|for\s+|if\s+|whether\s+)(?P<q>(?!it$|this$|that$).{4,300})",
            r"(?P<pre>.{4,300}?)[.,]?\s+(?:ask|check\s+with)\s+claude(?:\s+(?:for|about)\s+(?:it|this|that))?",
        ], ask_claude),

        # ── what NORA can do ──────────────────────────────────────────────
        ("capabilities", [
            r"what\s+(?:else\s+|all\s+|other\s+things\s+)?can\s+you\s+(?:do|help\s+(?:me\s+)?with)",
            r"what\s+are\s+(?:all\s+)?the\s+things\s+you\s+can\s+do",
            r"how\s+can\s+you\s+help(?:\s+me)?",
            r"what\s+are\s+you\s+capable\s+of",
        ], lambda m: chat("capabilities", _CAPABILITIES)),

        # ── small talk ────────────────────────────────────────────────────
        ("how-are-you", [
            r"(?:(?:hey|hi|hello)(?:\s+(?:there|nora|jarvis))?[,.!]?\s+)?(?:how\s+are\s+(?:you|u|ya)(?:\s+doing)?(?:\s+today)?"
            r"|how(?:'?s|\s+is)\s+it\s+going|how\s+do\s+you\s+do|what'?s\s+good|what'?s\s+up)"
            r"(?:[?,.!]*\s+how\s+are\s+you)*",
        ], lambda m: chat("how_are_you", "Doing well. What do you need?")),
        ("ack", [
            r"(?:okay|ok|sure|sure\s+thing|got\s+it|alright|all\s+right|m+-?h+m+|cool|nice|great|good\s+to\s+hear"
            r"|sounds\s+good|perfect|noted)(?:\s+(?:nora|jarvis))?",
        ], lambda m: chat("backchannel", "Mm-hm.")),
        ("thanks", [
            r"(?:no\s+)?thank\s+you[,]?\s+(?:you(?:'re|\s+are)\s+(?:amazing|the\s+best|great|awesome))",
        ], lambda m: chat("thanks_reply", "Any time.")),
    ]


# ── helpers for builders ─────────────────────────────────────────────────────

_NOT_A_PLACE = re.compile(
    r"^(?:my|your|our|me|us|him|her|them|it|this|that|these|those|what|how|why|who|where|when|which|a|an|some|any|all|"
    r"everything|something|anything)\b"
    r"|^(?:the\s+)?(?:screen|news|weather|time|date|files?|folders?|logs?|desktop|processes|patterns|memor(?:y|ies)|calendar|"
    r"agenda|notifications?|tasks?|list|code|errors?|results?|diff|menu|settings|window|way|door|options|"
    r"difference|answer|steps?|plan|reminders?|playlists?|songs?|music|photos?|pictures?|images?|videos?|"
    r"directions?|route|traffic|map|world|globe|dashboard|orb|status|stats|battery)\b", re.I)


def _clock(raw: str) -> tuple[int, int] | None:
    raw = raw.lower().replace(".", ":").replace(" ", "")
    if raw == "noon":
        return 12, 0
    if raw == "midnight":
        return 0, 0
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(a:?m:?|p:?m:?)?", raw)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    pm = (m.group(3) or "").startswith("p")
    if pm and h < 12:
        h += 12
    elif (m.group(3) or "").startswith("a") and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    return h, mi


def _event(fp, rest: str):
    """add_calendar_event from "called X for tomorrow at 4 pm", "for today 5 pm
    saying X", "to my calendar for tomorrow at 4pm saying X". None without a title."""
    s = " " + rest.strip(" ,.?!") + " "
    s = re.sub(r"\s(?:to|in|on)\s+(?:my\s+|the\s+)?calendar\b", " ", s, flags=re.I)
    day = re.search(r"\s(?:for\s+|on\s+)?" + DAY + r"(?=\s)", s, re.I)
    tm = re.search(r"\s(?:at\s+|for\s+)?" + TIME + r"(?=\s)", s, re.I)
    # A title runs until a day or a time: "study for pe exam" keeps its "for".
    when = r"(?:\s(?:for\s+|on\s+|at\s+)?(?:" + DAY + "|" + TIME.replace("?P<time>", "?:") + r")(?=\s))"
    title = re.search(r"\s(?:called|named|titled|saying|about)\s+(?P<t>.+?)(?=" + when.replace("?P<day>", "?:")
                      + r"|\s*$)", s, re.I)
    if not title:
        return None
    summary = title.group("t").strip(" ,.")
    if not summary or (tm and summary == tm.group("time")):
        return None
    params = {"summary": summary, "date": (_day(day) if day else "today")}
    if tm and tm.group("time") and re.search(r"\d", tm.group("time")):
        t = tm.group("time").replace(".", ":").replace(" ", "")
        if not re.search(r"[ap]", t, re.I) and ":" not in t and int(t) <= 12:
            return None                      # "at 5": morning or evening? let the model ask
        params["time"] = t
    return fp._intent("add calendar event", "add_calendar_event", params)


_CAPABILITIES = (
    "Quite a lot: your calendar, email and reminders, music on the laptop or "
    "your phone, the weather, news briefings, directions, timers and alarms on "
    "the phone, what's on your screen, places on the globe, and anything you "
    "want looked up or asked of Claude. What do you need?")


def register(fp) -> None:
    """Add every family's phrasings to fast_path's rule list, in table order."""
    for _name, phrasings, build in _families(fp):
        for p in phrasings:
            fp._rule(p, build)

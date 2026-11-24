"""Fast-path rules for the NORA Assist features (NORA_ASSIST_PLAN.md).

Kept beside `fast_path` rather than inside it: `fast_path._build_rules` calls
`register(_rule, _intent)` once, after the phone's rules, so these phrasings
are matched before the general music / open / search rules can claim them.
"""
from __future__ import annotations

import re

_SUBJECT = r"[\w][\w&+.' -]{0,40}?"


def register(rule, intent) -> None:
    # ── A: vocabulary ──────────────────────────────────────────────────────
    # "you misheard, I said Hormuz, not vermouth" / "I said Hormuz not vermouth"
    rule(r"(?:no[,.]?\s+)?(?:you\s+(?:misheard|heard\s+(?:that\s+)?wrong)(?:\s+me)?[,.]?\s+)?"
         r"i\s+said\s+(?P<word>[\w' -]{2,40}?)[,]?\s+not\s+(?P<heard>[\w' -]{2,40}?)",
         lambda m: intent("teach a word", "learn_word",
                          {"word": m["word"].strip(), "heard": m["heard"].strip()}))
    # "when I say vermouth I mean Hormuz"
    rule(r"when\s+i\s+say\s+(?P<heard>[\w' -]{2,40}?)[,]?\s+i\s+mean\s+(?P<word>[\w' -]{2,40}?)",
         lambda m: intent("teach a word", "learn_word",
                          {"word": m["word"].strip(), "heard": m["heard"].strip()}))
    rule(r"(?:learn|remember)\s+(?:the\s+)?(?:word|name|spelling)\s+(?P<word>[\w' -]{2,40})",
         lambda m: intent("teach a word", "learn_word", {"word": m["word"].strip()}))
    rule(r"forget\s+(?:the\s+)?(?:word|name)\s+(?P<word>[\w' -]{2,40})",
         lambda m: intent("forget a word", "forget_word", {"word": m["word"].strip()}))
    rule(r"(?:what|which)\s+words\s+(?:have\s+i\s+taught\s+you|do\s+you\s+know|have\s+you\s+learn(?:ed|t))",
         lambda m: intent("list words", "list_words"))

    # ── B: follow-through ──────────────────────────────────────────────────
    _day = (r"(?P<due>today|tonight|tomorrow|(?:next\s+)?(?:monday|tuesday|wednesday|thursday|friday"
            r"|saturday|sunday)|next\s+week|the\s+\d{1,2}(?:st|nd|rd|th)?|in\s+\w+\s+days?)")
    rule(r"(?:push|move|postpone|shift)\s+(?P<q>.{1,60}?)\s+(?:to|till|until)\s+" + _day,
         lambda m: intent("push a task", "push_task", {"query": m["q"], "due": m["due"]}))
    rule(r"(?:push|move|postpone)\s+(?P<q>it|that|that\s+one)",
         lambda m: intent("push a task", "push_task", {"query": "it", "due": "tomorrow"}))
    rule(r"(?:drop|forget\s+about|cancel)\s+(?P<q>it|that|that\s+one|the\s+task\s+.{1,60}|task\s+.{1,60})",
         lambda m: intent("drop a task", "drop_task",
                          {"query": re.sub(r"^(?:the\s+)?task\s+", "", m["q"])}))

    # ── C: routines ────────────────────────────────────────────────────────
    _care = r"(?:meds|medicine|medication|pills|tablets|vitamins|food|dinner|breakfast|bath|treat)"
    _done = (r"(?:fed|walked|watered|bathed|(?:gave|took)\s+(?:\w+\s+){0,2}?(?:(?:his|her|my|the)\s+)?"
             + _care + r"\b)")
    rule(r"i(?:'ve|\s+have)?\s+(?:just\s+|already\s+)?(?P<what>" + _done + r"(?:\s+.{1,60}?)?)(?:\s+(?:just\s+now|now))?",
         lambda m: intent("log a routine", "log_routine", {"what": m["what"]}))
    rule(r"(?P<who>\w+)\s+(?:has\s+been|was|got)\s+(?P<verb>fed|walked)(?:\s+(?:just\s+now|now))?",
         lambda m: intent("log a routine", "log_routine", {"what": f"{m['verb']} {m['who']}"}))
    rule(r"(?:did|have)\s+i\s+(?:already\s+)?(?P<what>(?:feed|walk|give|take|water|brush|bathe|clean|fed|walked|gave|took)"
         r"\s+.{1,60}?)(?:\s+(?:today|yet|this\s+morning|this\s+evening|tonight))?",
         lambda m: intent("check a routine", "last_routine", {"what": m["what"]}))
    rule(r"when\s+did\s+i\s+last\s+(?P<what>\w+\s+.{1,60}?)",
         lambda m: intent("check a routine", "last_routine", {"what": m["what"]}))
    rule(r"(?:has|was)\s+(?P<who>\w+)\s+(?:been\s+)?(?P<verb>fed|walked)(?:\s+(?:today|yet))?",
         lambda m: intent("check a routine", "last_routine", {"what": f"{m['verb']} {m['who']}"}))
    rule(r"remind\s+me\s+if\s+i\s+(?:haven't|have\s+not|didn't|did\s+not)\s+(?P<what>.{2,60}?)\s+"
         r"(?:by|at)\s+(?P<times>.{1,60})",
         lambda m: intent("add a routine", "add_routine", {"what": m["what"], "times": m["times"]}))
    rule(r"(?:what|which)\s+routines\s+(?:do\s+i\s+have|are\s+there)|list\s+(?:my\s+)?routines",
         lambda m: intent("list routines", "list_routines"))

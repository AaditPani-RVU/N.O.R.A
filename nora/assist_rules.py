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

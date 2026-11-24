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

    # ── D: story watch ─────────────────────────────────────────────────────
    rule(r"(?:keep\s+(?:an\s+)?eye\s+on|keep\s+(?:me\s+)?(?:posted|updated)\s+(?:on|about)"
         r"|(?:follow|track|watch)\s+(?:the\s+)?(?:news|story|stories|situation|developments)\s+(?:on|about|in|with|around))\s+"
         r"(?P<t>(?!(?:me|you|him|her|them|it|my\s+back)\b).{2,60}?)(?:\s+for\s+me)?",
         lambda m: intent("watch a story", "watch_story", {"topic": m["t"]}))
    rule(r"(?:stop|quit)\s+(?:watching|following|tracking)\s+(?P<t>.{2,60})",
         lambda m: intent("unwatch a story", "unwatch_story", {"topic": m["t"]}))
    rule(r"(?:what\s+stories\s+are\s+you\s+(?:watching|following)|which\s+stories\s+(?:am\s+i|are\s+you)\s+following)",
         lambda m: intent("list stories", "list_stories"))
    rule(r"any\s+(?:updates?|news)\s+on\s+(?:my\s+stories|the\s+stories\s+(?:you're|you\s+are)\s+watching)",
         lambda m: intent("story update", "story_update", {"topic": ""}))



def register_first(rule, intent) -> None:
    """Rules that must beat the phrase tables: "what's new on Hormuz" is the
    briefing's phrasing too, and a watched story should answer it with the
    change rather than the whole news grid."""
    def _story_or_none(m):
        # "What's new on Hormuz" is a story update only for a watched story;
        # anything else goes on to the news briefing as before.
        from nora import story_watch
        try:
            if story_watch._find(m["t"]) is None:
                return None
        except Exception:
            return None
        return intent("story update", "story_update", {"topic": m["t"]})
    rule(r"(?:what'?s\s+(?:new|the\s+latest|happening)|any\s+(?:updates?|news))\s+(?:on|with|about|in)\s+(?:the\s+)?(?P<t>.{2,60}?)",
         _story_or_none)

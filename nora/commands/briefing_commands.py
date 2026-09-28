"""The "what's going on in the world" takeover — headlines as a card grid.

Order of operations differs from the other takeovers on purpose. The
constellations push the visual first, because their data is already local.
This one has nothing to show until the feeds answer, so it fetches, pushes
the grid, and only then writes the sentence — you read the cards while she
talks over them.

The cards handed to the screen and the cards handed to the summariser are the
same list, passed once. That is the whole safeguard: a second fetch, or a
model left to its own knowledge, produces a spoken briefing describing stories
that are not on the screen.
"""
from __future__ import annotations

import datetime
import logging

from nora import briefing, ui_server
from nora.command_engine import register
from nora.config import get_config
from nora.conversation import for_speech

logger = logging.getLogger("nora.commands.briefing")


def _cfg() -> dict:
    return get_config().get("briefing", {}) or {}


def _format_cards(cards: list[dict]) -> str:
    lines = []
    for i, c in enumerate(cards, 1):
        lines.append(f"{i}. [{c['source']}] {c['title']}")
        if c.get("summary"):
            lines.append(f"   {c['summary'][:200]}")
    return "\n".join(lines)


def _summarise(label: str, cards: list[dict]) -> str:
    """Two spoken sentences describing *only* what is on the screen."""
    from nora.model_router import complete, AllCandidatesFailed

    today = datetime.date.today().strftime("%A, %d %B %Y")
    prompt = (
        f"Today is {today}. Below are the {len(cards)} headlines currently on "
        f"the user's screen, about {label}. In 2 or 3 concise spoken "
        f"sentences, tell them what's going on — the through-line if there is "
        f"one, otherwise the two or three that matter most. "
        f"Describe ONLY these headlines: they are what the user is looking "
        f"at, and mentioning anything else is wrong even if you know it. "
        f"No markdown, no filler words, no numbered lists — this is read "
        f"aloud. Name an outlet the way a person would ('the Guardian has'), "
        f"never a URL.\n\n{_format_cards(cards)}"
    )
    try:
        text, candidate = complete(
            "research",
            [{"role": "user", "content": prompt}],
            max_tokens=int(_cfg().get("summary_tokens", 300)),
            temperature=0.3,
        )
        logger.info("briefing summarised %d card(s) via %s", len(cards), candidate)
        return for_speech(text, max_sentences=3)
    except AllCandidatesFailed as e:
        logger.error("briefing summary failed on every candidate: %s", e)
    except Exception as e:
        logger.error("briefing summary error: %s", e)
    # The grid is already up and correct — read the top headline rather than
    # apologising at a screen full of news.
    top = cards[0]
    return f"Top of the list: {top['title']}, from {top['source']}."


@register(
    "show_briefing",
    sig="show_briefing(topic: str = '')",
    description=(
        'PREFERRED over tell_me_about whenever the user wants to be caught up '
        'on a subject rather than have one question answered. Puts a grid of '
        'current headlines with pictures on the dashboard and reads out what '
        'they add up to. Leave topic empty for their own interests; pass just '
        'the subject when they name one ("the AI world" -> "AI"). Use for '
        '"what\'s going on in the world", "what\'s the news", "catch me up", '
        '"what\'s new in the AI world", "anything happening with cars", '
        '"what\'s new in music", "what\'s the latest in F1". Use '
        'tell_me_about instead only for a specific question with a specific '
        'answer, like "who won the race".'
    ),
    category="knowledge",
)
def show_briefing(topic: str = "") -> str:
    data = briefing.collect(topic)
    cards = data["cards"]
    label = data["label"]

    if not cards:
        return (f"I couldn't get anything on {label} just now — "
                "the feeds aren't answering.")

    # Push the grid before the model is asked for a sentence: the summary is
    # the slow half, and there is no reason to stare at the orb through it.
    ui_server.notify_takeover(
        "briefing",
        cards=cards,
        label=label,
        hold_ms=int(_cfg().get("hold_ms", 45000)),
    )
    logger.info("briefing: %d card(s) on screen for %r in %dms (%d/%d feeds)",
                len(cards), label, data["took_ms"], data["sourced"], data["feeds"])

    return _summarise(label, cards)

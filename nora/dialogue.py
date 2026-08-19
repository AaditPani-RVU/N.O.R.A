"""Dialogue state — what was actually said, by both sides.

Before this module NORA had no record of its own speech.  ``context`` stored
*commands* (text → intent → actions → result), but conversational turns never
reached it at all: the pipeline spoke the reply and `continue`d straight past
``add_session_turn``.  The practical effect was that NORA forgot every word it
said the instant it finished saying it, so "why?", "say that again", "what
about the other one" had nothing to attach to.

Two things live here:

``Utterance`` store
    A rolling, thread-safe transcript of user and NORA turns, convertible to
    OpenAI-style ``messages`` for the conversation engine.

Dialogue-act classifier
    A cheap, deterministic pass that answers "is this conversation or is this
    a command?" *before* the action-planning LLM is consulted.  Routing chat to
    the chat path skips a 6 kB JSON-mode prompt and lands somewhere that can
    actually hold a thread.  It is deliberately conservative: anything that
    smells like an instruction returns ``Act.COMMAND`` and takes the normal
    path, because a misrouted command is a far worse failure than a misrouted
    pleasantry.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("nora.dialogue")

_lock = threading.RLock()

# Turns kept in the rolling transcript. ~24 turns ≈ 12 exchanges, which is
# well past the point where voice users expect recall and still small enough
# to prepend to every chat call without eating the context budget.
_MAX_TURNS = 24


# ── Transcript ───────────────────────────────────────────────────────────────

@dataclass
class Utterance:
    """One thing that was said, by one party."""

    speaker: str                 # "user" | "nora"
    text: str
    kind: str = "chat"           # chat | command | result | proactive | system
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "speaker": self.speaker,
            "text": self.text,
            "kind": self.kind,
            "ts": self.ts,
        }


_turns: deque[Utterance] = deque(maxlen=_MAX_TURNS)


def record_user(text: str, kind: str = "chat") -> None:
    """Append something the user said."""
    if not text or not text.strip():
        return
    with _lock:
        _turns.append(Utterance(speaker="user", text=text.strip(), kind=kind))


def record_nora(text: str, kind: str = "chat") -> None:
    """Append something NORA said.

    Every spoken line should land here — chat replies, action summaries, and
    proactive nudges alike.  Follow-ups routinely reference an action result
    ("did that work?"), not just conversation.
    """
    if not text or not text.strip():
        return
    with _lock:
        _turns.append(Utterance(speaker="nora", text=text.strip(), kind=kind))


def history(n: int = _MAX_TURNS) -> list[Utterance]:
    """Most recent ``n`` utterances in chronological order."""
    with _lock:
        turns = list(_turns)
    return turns[-n:] if n > 0 else []


def as_messages(n: int = 12) -> list[dict[str, str]]:
    """Recent turns as OpenAI-style chat messages.

    Consecutive *assistant* turns are merged — a command speaks a confirmation
    and then a result summary, and back-to-back assistant messages are rejected
    or mishandled by many chat endpoints.

    Consecutive user turns are deliberately left separate. Gluing them together
    makes two unrelated utterances read as one thought, and the model answers a
    question nobody asked.
    """
    messages: list[dict[str, str]] = []
    for turn in history(n):
        role = "user" if turn.speaker == "user" else "assistant"
        if role == "assistant" and messages and messages[-1]["role"] == "assistant":
            messages[-1]["content"] += "\n" + turn.text
        else:
            messages.append({"role": role, "content": turn.text})
    return messages


def as_transcript(n: int = 8) -> str:
    """Recent turns as a plain-text transcript, for prompt injection."""
    lines = []
    for turn in history(n):
        who = "User" if turn.speaker == "user" else "NORA"
        lines.append(f"{who}: {turn.text}")
    return "\n".join(lines)


def last_nora_reply() -> str:
    """The last thing NORA said, or "" if it hasn't spoken yet."""
    for turn in reversed(history()):
        if turn.speaker == "nora":
            return turn.text
    return ""


def last_user_utterance(skip: int = 0) -> str:
    """The user's most recent utterance, skipping ``skip`` of the newest."""
    seen = 0
    for turn in reversed(history()):
        if turn.speaker == "user":
            if seen >= skip:
                return turn.text
            seen += 1
    return ""


def turn_count() -> int:
    with _lock:
        return len(_turns)


def has_context() -> bool:
    """True once there is a prior exchange for a follow-up to attach to."""
    return bool(last_nora_reply())


def clear() -> None:
    """Drop the transcript (new session, or explicit "forget this conversation")."""
    with _lock:
        _turns.clear()


# ── Topic tracking ───────────────────────────────────────────────────────────

_current_topic: str = ""

# Words too generic to be worth remembering as "the topic"
_STOPWORDS = {
    "what", "whats", "how", "why", "when", "where", "who", "which", "that",
    "this", "there", "here", "the", "a", "an", "is", "are", "was", "were",
    "do", "does", "did", "can", "could", "would", "should", "about", "tell",
    "me", "you", "your", "my", "it", "its", "and", "but", "for", "with",
    "please", "sir", "nora", "of", "to", "in", "on", "at", "so", "just",
    "think", "feel", "guess", "wonder", "reckon", "know", "mean", "say",
    "really", "actually", "maybe", "some", "any",
}


def set_topic(text: str) -> None:
    """Record what the conversation is currently about."""
    global _current_topic
    tokens = [w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in _STOPWORDS]
    if len(tokens) >= 1:
        with _lock:
            _current_topic = " ".join(tokens[:8])


def get_topic() -> str:
    with _lock:
        return _current_topic


# ── Dialogue acts ────────────────────────────────────────────────────────────

class Act(str, Enum):
    """What the user is doing with this utterance."""

    COMMAND = "command"          # wants something done — take the action path
    SMALL_TALK = "small_talk"    # greetings, pleasantries, "how are you"
    BACKCHANNEL = "backchannel"  # "mhm", "cool", "ok" — acknowledgement, no answer needed
    FOLLOW_UP = "follow_up"      # attaches to the previous turn ("why?", "go on")
    QUESTION = "question"        # open question wanting a spoken answer
    META = "meta"                # about NORA herself
    UNKNOWN = "unknown"          # no confident read — let the action planner decide


CONVERSATIONAL_ACTS = frozenset({
    Act.SMALL_TALK, Act.BACKCHANNEL, Act.FOLLOW_UP, Act.QUESTION, Act.META,
})


def is_conversational(act: Act) -> bool:
    return act in CONVERSATIONAL_ACTS


# Verbs and nouns that mean "do a thing on this machine". If any of these show
# up the utterance goes down the action path, no matter how chatty it looks.
_COMMAND_SIGNALS = (
    r"\bopen\b", r"\blaunch\b", r"\bstart\b", r"\brun\b", r"\bclose\b", r"\bquit\b",
    r"\bkill\b", r"\bplay\b", r"\bpause\b", r"\bresume\b", r"\bskip\b", r"\bmute\b",
    r"\bunmute\b", r"\bvolume\b", r"\bscreenshot\b", r"\bscreen\s*shot\b",
    r"\bdelete\b", r"\bremove\b", r"\bmove\b", r"\brename\b", r"\bcopy\b",
    r"\bpaste\b", r"\btype\b", r"\bclick\b", r"\bpress\b", r"\bscroll\b",
    r"\bsearch\b", r"\bgoogle\b", r"\bemail\b", r"\bsend\b", r"\bschedule\b",
    r"\bremind\b", r"\bsnapshot\b", r"\broll\s*back\b", r"\bshut\s*down\b",
    r"\brestart\b", r"\block\b", r"\bwifi\b", r"\bbluetooth\b", r"\bbrightness\b",
    r"\bfocus\s+mode\b", r"\btake\s+a\b", r"\bset\b", r"\bturn\s+(?:on|off|up|down)\b",
    r"\bcommit\b", r"\bpush\b", r"\bpull\b", r"\bdeploy\b", r"\binstall\b",
    r"\bcreate\b", r"\bmake\s+(?:a|an|the)\b", r"\bwrite\b", r"\bsave\b",
    r"\bshow\s+me\b", r"\bfind\b", r"\bnext\s+(?:song|track)\b", r"\brecall\b",
)
_COMMAND_RE = re.compile("|".join(_COMMAND_SIGNALS), re.I)

# Questions whose honest answer needs a lookup. Nobody is asking NORA to *do*
# anything here, so these aren't commands in the sense above — but they can't
# be answered from a language model's weights either, and a model in chat mode
# asked "is it going to rain" produces a confident forecast rather than
# admitting it has no idea. So they take the action path, where the planner
# has web_search, get_time and get_system_info.
#
# Matching the bare nouns ("weather", "news", "temperature") was the first
# attempt and it failed in both directions: almost nobody says "weather" out
# loud — they say "do I need an umbrella" — while "that's good news" and "do
# you like cold weather" were being dragged onto the action path. These
# patterns match the question *form*, not just the topic.
_LOOKUP_SIGNALS = (
    # Weather, however it actually gets phrased
    r"\bweather\b", r"\bforecast\b",
    r"\b(?:rain|snow|sleet|hail)(?:ing|s)?\b",
    r"\bhow\s+(?:hot|cold|warm|windy|humid)\b",
    r"\b(?:temperature|humidity|wind)\s+(?:outside|today|tomorrow|right\s+now)\b",
    r"\bwhat'?s\s+it\s+like\s+outside\b", r"\bhow'?s\s+it\s+(?:look\s+)?outside\b",
    r"\bneed\s+(?:an?\s+)?(?:umbrella|jacket|coat)\b",
    # Clock and calendar
    r"\bwhat\s+time\b", r"\bwhat'?s\s+the\s+time\b",
    r"\bwhat(?:'?s)?\s+(?:the\s+|today'?s\s+)?date\b",
    r"\bwhat\s+day\s+is\s+it\b", r"\bday\s+of\s+the\s+week\b",
    r"\bcalendar\b", r"\bmy\s+(?:email|inbox|schedule|meetings?)\b",
    # News, sport and markets — current state, unknowable from weights
    r"\b(?:the|any|latest|breaking|top|world|today'?s)\s+news\b",
    r"\bnews\s+(?:today|headlines?)\b", r"\bheadlines\b",
    r"\bwhat'?s\s+(?:happening|going\s+on)\s+in\s+the\s+world\b",
    r"\bwho\s+won\b", r"\bwhat'?s\s+the\s+score\b", r"\bfinal\s+score\b",
    r"\b(?:stock|share)\s+price\b", r"\btrading\s+at\b", r"\bexchange\s+rate\b",
    # This machine's current state
    r"\bbattery\b", r"\bdisk\s+space\b", r"\b(?:cpu|gpu)\s+temp(?:erature)?\b",
    r"\b(?:cpu|ram|memory)\s+usage\b", r"\bhow\s+much\s+(?:ram|memory|disk)\b",
    r"\buptime\b",
)
_LOOKUP_RE = re.compile("|".join(_LOOKUP_SIGNALS), re.I)

# "any news on that", "what's the forecast for it" — a topic word pointing at
# something already said is a follow-up, not a fresh lookup. The action path
# has no transcript, so sending these there produces a search for nothing.
_BACK_REFERENCE_RE = re.compile(
    r"\b(?:on|about|with|for|to)\s+(?:that|it|this|those|them)\b", re.I
)

# Filler Whisper likes to prepend; strip before classifying.
# "hey" and "yo" are deliberately absent — they're greetings, not noise, and
# stripping them turned "hey nora" into a bare address that classified as
# UNKNOWN instead of small talk.
_FILLER_RE = re.compile(
    r"^(?:(?:uh+|um+|er+|ah+|hmm+|so|well|okay|ok|alright)[,.\s]+)+", re.I
)
_ADDRESS_RE = re.compile(r"^(?:hey\s+|okay\s+|ok\s+)?nora[,.\s]+", re.I)

_GREETING_RE = re.compile(
    r"^(?:hi|hello|hey|yo|hiya|howdy|greetings|good\s+(?:morning|afternoon|evening|day)"
    r"|morning|evening)\b", re.I
)
_HOW_ARE_YOU_RE = re.compile(
    r"^(?:how(?:'s| is| are)\s+(?:you|it going|things|everything|your day)"
    r"|you\s+(?:good|okay|ok|alright)|what'?s\s+up|sup|how\s+goes\s+it)\b", re.I
)
_THANKS_RE = re.compile(
    r"^(?:thanks?|thank\s+you|ty|cheers|appreciate\s+it|nice\s+one|good\s+(?:job|work)"
    r"|well\s+done|perfect|awesome|excellent|great)\b", re.I
)

# Pure acknowledgement — nothing is being asked
_BACKCHANNEL_RE = re.compile(
    r"^(?:mm+|mhm+|uh\s*huh|uh|um+|hm+|ah+|oh+|ok(?:ay)?|right|sure|yeah|yep|yup|yes|no|nope"
    r"|cool|nice|neat|sweet|fine|got\s+it|i\s+see|makes\s+sense|fair\s+enough"
    r"|interesting|wow|damn|huh|true|indeed|exactly)"
    r"(?:\s+(?:then|good|great|cool|thanks?|sir))?$", re.I
)

# Attaches to whatever was just said — meaningless without prior context
_FOLLOW_UP_RE = re.compile(
    r"^(?:why(?:\s+(?:not|is\s+that|though))?|how\s+come|and\s*\?*$|and\s+(?:then|what|why|how)"
    r"|go\s+on|keep\s+going|continue|tell\s+me\s+more|say\s+more|more\s+(?:on\s+)?(?:that|it)"
    r"|elaborate|explain(?:\s+(?:that|it|more))?|what\s+do\s+you\s+mean"
    r"|(?:are\s+you|you)\s+sure|really\??|seriously\??|is\s+that\s+(?:right|true|correct)"
    r"|that'?s\s+(?:wrong|not\s+right|incorrect)|no\s+it'?s\s+not"
    r"|say\s+(?:that\s+)?again|repeat\s+that|come\s+again|what\s+was\s+that"
    r"|what\s+about\b|how\s+about\b|and\s+the\b|what\s+if\b"
    r"|which\s+one|the\s+(?:first|second|last|other)\s+one"
    r"|wait\b|hold\s+on|hang\s+on|actually\b|never\s*mind)", re.I
)

# About NORA herself
_META_RE = re.compile(
    r"(?:who\s+are\s+you|what\s+are\s+you|what\s+can\s+you\s+do|your\s+name"
    r"|are\s+you\s+(?:there|awake|alive|listening|real|human|an?\s+ai)"
    r"|what\s+(?:do|can)\s+you\s+(?:think|feel)|do\s+you\s+(?:like|feel|think|remember)"
    r"|how\s+do\s+you\s+work|what\s+model|about\s+yourself)", re.I
)

# Open questions and discussion openers that want words back, not actions
_DISCUSSION_RE = re.compile(
    r"^(?:what\s+do\s+you\s+think|thoughts\s+on|your\s+(?:take|opinion)|opinion\s+on"
    r"|do\s+you\s+(?:think|reckon|agree)|should\s+i\b|would\s+you\b|is\s+it\s+worth"
    r"|i\s+(?:think|feel|wonder|reckon|guess)\b|i'?m\s+(?:thinking|wondering)\b"
    r"|let'?s\s+talk|can\s+we\s+talk|talk\s+to\s+me)", re.I
)

_QUESTION_WORD_RE = re.compile(r"^(?:what|who|when|where|why|how|is|are|was|were|do|does|did|can|could|would|should)\b", re.I)


def normalise(text: str) -> str:
    """Strip wake-address and filler so classification sees the real utterance."""
    t = _ADDRESS_RE.sub("", text.strip())
    t = _FILLER_RE.sub("", t)
    return t.strip().rstrip(".,!?;").strip()


def classify(text: str, *, has_prior_turn: bool | None = None) -> Act:
    """Classify an utterance as conversation or command.

    ``has_prior_turn`` overrides the transcript check (tests, replay). When
    False, follow-up patterns can't fire — "why?" with nothing to attach to is
    not a follow-up, it's an unknown.
    """
    if not text or not text.strip():
        return Act.UNKNOWN

    t = normalise(text)
    if not t:
        # Pure filler ("uh", "hmm") — an acknowledgement at most.
        return Act.BACKCHANNEL

    lower = t.lower()
    prior = has_context() if has_prior_turn is None else has_prior_turn

    # 1. Backchannels first: "no" and "right" are single words that would
    #    otherwise trip the question-word test below.
    if _BACKCHANNEL_RE.match(lower):
        return Act.BACKCHANNEL

    if _THANKS_RE.match(lower) and len(lower.split()) <= 4:
        return Act.SMALL_TALK

    # 2. Commands win over everything that follows. A misrouted command is a
    #    much worse failure than a misrouted pleasantry, so this gate is early
    #    and deliberately broad.
    if _COMMAND_RE.search(lower):
        return Act.COMMAND

    # 3. Greetings and pleasantries.
    if _HOW_ARE_YOU_RE.match(lower):
        return Act.SMALL_TALK
    if _GREETING_RE.match(lower) and len(lower.split()) <= 6:
        return Act.SMALL_TALK

    # 4. Discussion openers, before the META check — "what do you think about
    #    Rust" is a conversation about Rust, not a question about NORA, even
    #    though it shares the "what do you think" stem.
    if _DISCUSSION_RE.match(lower):
        return Act.QUESTION

    # 5. Questions about NORA herself.
    if _META_RE.search(lower):
        return Act.META

    # 6. Follow-ups — only meaningful when there's something to follow up on.
    if prior and _FOLLOW_UP_RE.match(lower):
        return Act.FOLLOW_UP

    # 7. Questions that need a lookup. After the follow-up and meta checks, so
    #    "any news on that" stays a follow-up and "do you like cold weather"
    #    stays a question about NORA — but before the short-question rule
    #    below, which would otherwise send "is it raining" off to be answered
    #    from imagination.
    if _LOOKUP_RE.search(lower) and not (prior and _BACK_REFERENCE_RE.search(lower)):
        return Act.COMMAND

    # 8. Short questions with no command verb: conversational, not actionable.
    #    Longer ones ("how do I reverse a linked list") are research requests
    #    and belong to the action planner, which routes them to ask_claude.
    if (t.endswith("?") or _QUESTION_WORD_RE.match(lower)) and len(lower.split()) <= 7:
        return Act.QUESTION

    # 9. Bare pronoun references with prior context — "that one", "the blue one".
    if prior and len(lower.split()) <= 5 and re.search(r"\b(?:that|it|this|those|them|they)\b", lower):
        return Act.FOLLOW_UP

    return Act.UNKNOWN

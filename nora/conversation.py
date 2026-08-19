"""Conversation engine — NORA's talking, as opposed to NORA's doing.

Conversation used to be a side-effect of intent parsing.  Every utterance,
chat included, went through the action-planner: a ~6 kB execution-engine
system prompt ("You are NOT a chatbot. You are an execution engine."), JSON
mode, temperature 0.1, and an instruction that spoken replies be "1-2
sentences maximum".  A model told to be a JSON emitter, then handed a slot
called ``response``, fills that slot the way it fills every other slot — with
the shortest token sequence that satisfies the schema.  That's where the
stilted, clipped, faintly hostile replies came from.  It wasn't the model.  It
was the frame.

This module gives conversation its own frame:

* its own system prompt, written for speech rather than for JSON
* real multi-turn history from ``nora.dialogue`` — NORA can see what it said
* a warmer sampling temperature
* the ``chat`` role in ``nora.model_router``, so it gets the 70B model with
  cloud→local fallback rather than the small local intent model
* output shaped for a text-to-speech engine on the way out

The action path is untouched.  Commands still go to the intent parser; this
handles the turns where the right response is words.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime

from nora import dialogue, phrasing
from nora.config import get_config
from nora.dialogue import Act

logger = logging.getLogger("nora.conversation")


# ── Identity ─────────────────────────────────────────────────────────────────

_IDENTITY = """You are NORA — a voice-based AI assistant running locally on the \
user's Linux machine. You have real capabilities: you control applications, \
media, files, the window manager and the system itself, and you keep long-term \
memory of what the user does.

You are speaking out loud. Everything you write is going straight into a \
text-to-speech engine and into someone's ears.

HOW TO TALK
- Talk like a person who is good at their job, not like a product. Warm, dry, \
unhurried, occasionally funny. You have opinions and you can say them.
- Never use markdown, bullet points, numbered lists, headers, asterisks, emoji \
or code blocks. None of it survives being spoken.
- Write the way people speak: contractions, short clauses, natural rhythm. \
Read your reply back in your head — if it sounds like documentation, rewrite it.
- Vary how you open. Do not begin consecutive replies the same way, and do not \
start every answer by restating the question.
- Do not narrate yourself. No "Certainly!", no "As an AI", no "I'd be happy \
to", no announcing what you're about to say before you say it.
- Silence is allowed. If the user says "cool" or "mhm", a two-word \
acknowledgement is the correct response, not a paragraph.

BEING IN A CONVERSATION
- You can see the recent transcript. Use it. "Why?" means "why what you just \
said". "The other one" refers to something real. Resolve it instead of asking.
- If you genuinely don't know what they meant, ask one short natural question. \
Never say "please provide more context" or "could you clarify" — that is a \
form, not a conversation.
- Don't re-explain things you already said this session, and don't re-introduce \
yourself.
- Make each point once. Never restate a sentence in different words inside the \
same reply, and never start your answer over. Say it, then stop.
- If you're uncertain or wrong, say so plainly and briefly. If the user \
corrects you, take the correction and move on — no grovelling.
- Never invent facts about the user's machine, files or history. If you'd need \
to run something to know, say that you'd need to check.
- The same goes for the state of the world. You do not know today's weather, \
the news, scores, prices or anything else that changes by the hour. The date \
and time below are real; everything else current is not. If a question turns \
on one of those, say you'll need to look it up rather than producing a \
plausible-sounding answer."""


# Sentence budgets by dialogue act. The old prompt applied a flat "1-2
# sentences maximum" to everything, which is right for "hey" and badly wrong
# for "what do you think about this design".
_LENGTH_GUIDE: dict[Act, str] = {
    Act.BACKCHANNEL: "Reply in at most five words. Often the right answer is a single word.",
    Act.SMALL_TALK:  "Keep it to one short sentence, maybe two. This is a pleasantry, not a topic.",
    Act.META:        "Two or three sentences. Be specific about what you can actually do, not abstract.",
    Act.FOLLOW_UP:   "Answer the follow-up directly in one to three sentences. Don't restate the earlier answer.",
    Act.QUESTION:    "Two to four sentences. Enough to actually answer; stop when you have.",
    Act.UNKNOWN:     "Two to three sentences.",
    Act.COMMAND:     "One sentence.",
}

_MAX_TOKENS: dict[Act, int] = {
    Act.BACKCHANNEL: 32,
    Act.SMALL_TALK:  96,
    Act.META:        220,
    Act.FOLLOW_UP:   260,
    Act.QUESTION:    320,
    Act.UNKNOWN:     240,
    Act.COMMAND:     96,
}


# ── Instant replies ──────────────────────────────────────────────────────────
#
# Some turns don't deserve a network round-trip. Answering "mhm" in 20 ms with
# "Mm-hm." feels more alive than answering it in 900 ms with a sentence.

_INSTANT: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^(?:thanks?|thank\s+you|ty|cheers|appreciate\s+it)\b", re.I), "thanks_reply"),
    (re.compile(r"^(?:hi|hello|hey|yo|hiya|howdy|greetings)\b", re.I), "greeting"),
    (re.compile(r"^(?:are\s+you\s+there|you\s+there|still\s+there|nora)\??$", re.I), "presence"),
]


def _instant_reply(text: str, act: Act) -> str | None:
    """Return a canned-but-varied reply for turns not worth an LLM call."""
    t = dialogue.normalise(text).lower()

    if act is Act.BACKCHANNEL:
        # "cool, thanks" arrives as a backchannel but is really gratitude —
        # answering it with "Mm-hm." reads as a shrug.
        if re.search(r"\b(?:thanks?|thank\s+you|cheers|appreciate)\b", t):
            return phrasing.get("thanks_reply")
        return phrasing.get("backchannel")

    for pattern, category in _INSTANT:
        if pattern.match(t) and len(t.split()) <= 4:
            return phrasing.get(category)

    if act is Act.SMALL_TALK and re.match(
        r"^(?:how(?:'s| is| are)\s+(?:you|it going|things)|what'?s\s+up|sup)", t
    ):
        return phrasing.get("how_are_you")

    return None


# ── Prompt assembly ──────────────────────────────────────────────────────────

def _time_of_day() -> str:
    hour = datetime.now().hour
    if hour < 5:
        return "the middle of the night"
    if hour < 12:
        return "morning"
    if hour < 17:
        return "afternoon"
    if hour < 22:
        return "evening"
    return "late evening"


def _situation_block(memory_ctx: dict | None) -> str:
    """Ambient facts NORA can reference naturally — never recite wholesale."""
    from nora import context

    lines: list[str] = [
        f"Right now it is {datetime.now().strftime('%A %-d %B %Y, %H:%M')} "
        f"({_time_of_day()}).",
    ]

    try:
        music = context.get_music()
        if music.get("status") == "playing" and music.get("track"):
            artist = f" by {music['artist']}" if music.get("artist") else ""
            lines.append(f"Music is playing: {music['track']}{artist}.")
    except Exception:
        pass

    try:
        apps = context.active_apps()
        if apps:
            lines.append(f"Apps you opened for the user this session: {', '.join(apps[:6])}.")
    except Exception:
        pass

    try:
        recent = context.recent_commands()[:3]
        intents = [c.get("intent", "") for c in recent if c.get("intent")]
        if intents:
            lines.append(f"Recent things you did: {'; '.join(intents)}.")
    except Exception:
        pass

    if memory_ctx:
        relevant = memory_ctx.get("relevant_context") or []
        if relevant:
            lines.append("Possibly relevant from long-term memory: " + " | ".join(relevant[:2]))

    topic = dialogue.get_topic()
    if topic:
        lines.append(f"The conversation has been about: {topic}.")

    lines.append(
        "These are background facts, not a script. Mention them only if they're "
        "actually relevant to what was asked."
    )
    return "SITUATION\n" + "\n".join(f"- {line}" for line in lines)


def _persona_block() -> str:
    try:
        from nora import persona
        return persona.format_for_prompt()
    except Exception:
        return ""


def _user_block() -> str:
    try:
        from nora.user_model import format_user_card_for_prompt
        return format_user_card_for_prompt() or ""
    except Exception:
        return ""


def build_system_prompt(act: Act = Act.UNKNOWN, memory_ctx: dict | None = None) -> str:
    """Assemble the full conversational system prompt."""
    parts = [_IDENTITY, _situation_block(memory_ctx)]

    user_block = _user_block()
    if user_block:
        parts.append("ABOUT THE USER (context, not a topic to bring up)\n" + user_block)

    persona_block = _persona_block()
    if persona_block:
        parts.append(persona_block)

    parts.append("LENGTH\n" + _LENGTH_GUIDE.get(act, _LENGTH_GUIDE[Act.UNKNOWN]))

    last = dialogue.last_nora_reply()
    if last:
        parts.append(
            "Your previous reply was: \"" + last[:200] + "\"\n"
            "Do not repeat it, and do not open this reply the same way."
        )

    return "\n\n".join(p for p in parts if p)


# ── Output shaping ───────────────────────────────────────────────────────────

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_ORPHAN_THINK_RE = re.compile(r"^.*?</think>", re.S | re.I)
_CODE_FENCE_RE = re.compile(r"```[\w+-]*\n?(.*?)```", re.S)
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_ITALIC_RE = re.compile(r"(\*{1,3}|_{2,3})(.+?)\1")
_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BULLET_RE = re.compile(r"^\s*(?:[-*•+]|\d{1,2}[.)])\s+", re.M)
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL_RE = re.compile(r"https?://\S+")
_EMOJI_RE = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF" "\U00002B00-\U00002BFF" "️" "]+"
)
_STOCK_OPENER_RE = re.compile(
    r"^(?:certainly|sure thing|of course|absolutely|great question|good question"
    r"|that'?s a great question|i'?d be happy to(?: help)?|i would be happy to(?: help)?"
    r"|happy to help|as an ai(?: assistant)?|as your assistant"
    r"|i understand(?: that)?|let me (?:help|explain)|no problem)\b[,!.…\s]*",
    re.I,
)


# Typographic characters models love and TTS engines mispronounce or spell out.
# gpt-oss in particular emits U+2011 non-breaking hyphens ("zero‑cost",
# "compile‑time") which some voices read as "zero unknown cost".
_PUNCT_MAP = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": " - ",
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"',
    "…": ".", " ": " ", " ": " ", " ": " ", "​": "",
    "′": "'", "″": '"', "﻿": "",
})


def for_speech(text: str, max_sentences: int = 0) -> str:
    """Turn model output into something a TTS engine should read aloud.

    Strips reasoning traces, markdown, emoji and URLs, normalises typographic
    punctuation, drops the stock assistant openers, and optionally caps the
    sentence count.
    """
    if not text:
        return ""

    text = text.translate(_PUNCT_MAP)

    # Reasoning traces from hybrid models (qwen3, nemotron) — including the
    # case where the opening tag was truncated away and only </think> remains.
    text = _THINK_RE.sub(" ", text)
    if "</think>" in text.lower():
        text = _ORPHAN_THINK_RE.sub(" ", text)

    text = _CODE_FENCE_RE.sub(lambda m: " " + m.group(1).strip() + " ", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub("that link", text)
    text = _HEADER_RE.sub("", text)
    text = _BULLET_RE.sub("", text)
    text = _BOLD_ITALIC_RE.sub(r"\2", text)
    text = _EMOJI_RE.sub("", text)

    # Models sometimes wrap the whole reply in quotes, which TTS reads as a pause.
    text = text.strip()
    if len(text) > 1 and text[0] in "\"'“" and text[-1] in "\"'”":
        text = text[1:-1].strip()

    # Openers stack ("Certainly! I'd be happy to help..."), so strip until the
    # text stops starting with one — a single sub only matches at position 0.
    for _ in range(3):
        stripped = _STOCK_OPENER_RE.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    # Re-capitalise if stripping an opener left a lowercase start.
    if text and text[0].islower():
        text = text[0].upper() + text[1:]

    # Former list items are now bare lines. Terminate each one before joining,
    # or TTS reads "install ripgrep Second run it" as a single breathless clause.
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    if len(lines) > 1:
        text = " ".join(ln if ln[-1] in ".!?;:," else ln + "." for ln in lines)
    else:
        # Single line: leave it alone. Adding a full stop here would disguise a
        # genuinely truncated tail as a finished sentence.
        text = lines[0] if lines else ""
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"\.{2,}", ".", text)
    text = re.sub(r"(?:\.\s*){2,}", ". ", text)
    # Cosmetic for the UI transcript (TTS ignores case): an ellipsis collapsed
    # to a full stop leaves the next word lowercase.
    text = re.sub(r"(?<=[.!?])(\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)

    # Restore the space after a sentence boundary. Models occasionally emit two
    # channels glued together ("...light rain.London's looking...") and without
    # the space nothing downstream — dedup here, sentence-splitting in the
    # speaker — can see the boundary. Guarded on a preceding lowercase/digit so
    # initialisms like "U.S.A." survive.
    text = re.sub(r"(?<=[a-z0-9])([.!?])([A-Z])", r"\1 \2", text)

    text = _strip_analysis(text)
    text = _collapse_repetition(text)

    if max_sentences > 0:
        sentences = re.split(r"(?<=[.!?])\s+", text)
        if len(sentences) > max_sentences:
            text = " ".join(sentences[:max_sentences])

    return text.strip()


# Sentences that are the model reasoning about its own task rather than
# answering. Configuring the provider to hide its reasoning channel is the real
# fix (see llm_router extra_body in config.yaml); this is the net underneath,
# because a local or newly-added model can leak the same way.
_ANALYSIS_RE = re.compile(
    r"^\s*(?:"
    r"we (?:need|should|must|have) to\b"
    r"|we (?:responded|answered|said|replied)\b"
    r"|the (?:user|guidelines?|instructions?|system prompt|prompt) (?:asked|says?|said|wants?|is|are)\b"
    r"|user (?:asked|said|wants?)\b"
    r"|(?:so )?the answer (?:should|must|needs to) be\b"
    r"|let'?s (?:think|consider|analyze|analyse)\b"
    r"|(?:i|we) (?:should|need to) (?:keep|answer|respond|reply|make sure)\b"
    r"|according to the (?:guidelines?|instructions?|persona)\b"
    r"|per the (?:guidelines?|instructions?)\b"
    r"|that'?s fine\.?\s*$"
    r")",
    re.I,
)


def _strip_analysis(text: str) -> str:
    """Drop leaked chain-of-thought sentences from a spoken reply."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(sentences) < 2:
        return text
    kept = [s for s in sentences if not _ANALYSIS_RE.match(s)]
    # If the filter would eat everything, the match was spurious — keep the
    # original rather than answering with silence.
    return " ".join(kept) if kept else text


def _collapse_repetition(text: str) -> str:
    """Remove degenerate repetition and truncated tails.

    Degenerate repetition is a normal LLM failure mode and is nearly invisible
    in written output — you skim past it. Spoken aloud, hearing the identical
    sentence twice is the most jarring thing an assistant can do, and hearing
    the whole answer start over is worse.

    Two shapes are handled. An adjacent duplicate sentence is dropped. A
    sentence that repeats one already seen is treated as the model looping back
    to the top, so everything from there on is discarded — including the
    half-sentence left behind when the token budget cut the loop short.
    """
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(sentences) < 2:
        return text.strip()

    def key_of(sentence: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", sentence.lower())

    kept: list[str] = []
    seen: list[str] = []
    for sentence in sentences:
        key = key_of(sentence)
        if not key:
            continue
        if kept and key == key_of(kept[-1]):
            continue
        # Only long sentences are trustworthy loop markers — a short "Yes." can
        # legitimately appear twice in one answer.
        if len(key) >= 20 and any(
            key == s or s.startswith(key) or key.startswith(s) for s in seen
        ):
            break
        seen.append(key)
        kept.append(sentence)

    # A trailing fragment with no terminal punctuation is a token-budget
    # casualty; speaking half a sentence sounds like a dropped call.
    if len(kept) > 1 and kept[-1] and kept[-1][-1] not in ".!?":
        kept.pop()

    return " ".join(kept).strip()


# ── Generation ───────────────────────────────────────────────────────────────

def _conv_cfg() -> dict:
    return get_config().get("conversation", {}) or {}


def _generate(system: str, messages: list[dict], act: Act) -> str:
    """Call the chat model, falling back through router → intent LLM."""
    cfg = _conv_cfg()
    temperature = float(cfg.get("temperature", 0.7))
    max_tokens = int(cfg.get("max_tokens") or _MAX_TOKENS.get(act, 240))
    timeout_sec = float(cfg.get("timeout_sec", 20))

    payload = [{"role": "system", "content": system}] + messages

    # 1. The router — chat role, cloud-first with local fallback built in.
    try:
        from nora import model_router
        text, candidate = model_router.complete(
            role="chat",
            messages=payload,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_sec=timeout_sec,
        )
        logger.info("conversation: replied via %s", candidate)
        return text
    except Exception as exc:
        logger.warning("conversation: model_router failed — %s", exc)

    # 2. Whatever the intent parser is configured to use.
    try:
        from nora import intent_parser
        return intent_parser._call_llm(system, messages)
    except Exception as exc:
        logger.warning("conversation: intent_parser fallback failed — %s", exc)

    return ""


def respond(text: str, memory_ctx: dict | None = None, act: Act | None = None) -> str:
    """Produce a spoken reply for a conversational utterance.

    Records both sides of the exchange into ``nora.dialogue`` so the next turn
    can see this one.  Always returns something speakable — a model outage
    degrades to a varied apology, never to silence.
    """
    started = time.monotonic()
    act = act or dialogue.classify(text)

    # The pipeline records the user's turn centrally as soon as it's heard;
    # only record here if it hasn't already landed (direct/API callers).
    if dialogue.last_user_utterance().strip() != text.strip():
        dialogue.record_user(text, kind="chat")
    if act in (Act.QUESTION, Act.META, Act.UNKNOWN):
        dialogue.set_topic(text)

    # Cheap turns skip the network entirely.
    instant = _instant_reply(text, act)
    if instant is not None:
        logger.info("conversation: instant reply for act=%s", act.value)
        dialogue.record_nora(instant, kind="chat")
        return instant

    system = build_system_prompt(act, memory_ctx)
    history = dialogue.as_messages(int(_conv_cfg().get("history_turns", 12)))
    if not history or history[-1]["role"] != "user":
        history = history + [{"role": "user", "content": text}]

    raw = _generate(system, history, act)
    reply = for_speech(raw, max_sentences=int(_conv_cfg().get("max_sentences", 6)))

    if not reply:
        reply = phrasing.get("chat_unavailable", "I'm having trouble reaching my models right now.")
        logger.warning("conversation: empty reply, using fallback")

    dialogue.record_nora(reply, kind="chat")
    logger.info("conversation: act=%s %.0fms — %s", act.value,
                (time.monotonic() - started) * 1000, reply[:80])
    return reply


def should_handle(act: Act) -> bool:
    """Whether the conversation engine owns this act, per config."""
    if not _conv_cfg().get("enabled", True):
        return False
    return dialogue.is_conversational(act)

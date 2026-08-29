"""A headless turn — text in, text out, no microphone and no speaker.

`pipeline.run_turn` cannot be reused here, and the reason is worth stating so
nobody tries. That function is a loop body around a live audio device: it
records, transcribes, speaks, drives the UI stage indicator, and asks for voice
confirmation by listening for the word "yes". Every one of those is meaningless
over a chat message, and the confirmation flow is worse than meaningless — it
would block forever on a microphone nobody is speaking into.

So this is a deliberately smaller turn with the same guards. Fast path first,
conversation routing second, intent parser third, then the same
`nora.security` check and the same `nora.command_engine.execute`. What it does
*not* do is confirm: anything the security policy flags is refused with an
explanation rather than run, because a remote message is exactly the context in
which "are you sure?" cannot be answered honestly. Destructive actions stay in
the room.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("nora.gateway.core")

# Kept short: a gateway reply lands in a chat window, where a wall of text is
# as unwelcome as it is in speech.
_MAX_REPLY_CHARS = 1200


async def handle_text(text: str, *, source: str = "gateway") -> str:
    """Run one utterance through NORA and return what it would have said."""
    from nora import (
        command_engine, context, conversation, dialogue, fast_path,
        intent_parser, memory, security,
    )

    text = (text or "").strip()
    if not text:
        return ""

    dialogue.record_user(text, kind=source)

    # 1. Deterministic fast path — no model call at all.
    intent = fast_path.resolve(text)
    if intent is not None and not intent.steps and intent.response:
        dialogue.record_nora(intent.response, kind=source)
        return intent.response

    # 2. Conversation, when the utterance is talk rather than instruction.
    if intent is None:
        act = dialogue.classify(text)
        if conversation.should_handle(act):
            loop = asyncio.get_running_loop()
            reply = await loop.run_in_executor(
                None, conversation.respond, text, memory.get_context_summary(), act
            )
            dialogue.record_nora(reply, kind=source)
            return reply[:_MAX_REPLY_CHARS]

        # 3. Action path.
        loop = asyncio.get_running_loop()
        try:
            intent = await loop.run_in_executor(
                None, intent_parser.parse_intent, text, memory.get_context_summary()
            )
        except Exception as e:
            logger.warning("gateway intent parse failed: %s", e)
            return "I couldn't work out what you wanted there."

    if intent is None or not intent.steps:
        return (intent.response if intent and intent.response
                else "I couldn't work out what you wanted there.")

    blocked, needs_confirm = security.check_steps(intent.steps)
    if blocked:
        return "That's blocked by the security policy."
    if needs_confirm or intent.requires_confirmation:
        # No voice channel to confirm on, and a chat "yes" is a weaker signal
        # than a spoken one from someone demonstrably in the room.
        actions = ", ".join(s.action for s in intent.steps)
        return (f"That needs confirming out loud ({actions}), so I've left it. "
                f"Ask me in the room.")

    results = await command_engine.execute(intent)
    reply = " ".join(r.message for r in results if r.message).strip() or "Done."

    context.add_session_turn(
        text=text, intent=intent.intent,
        actions=[s.action for s in intent.steps],
        result_summary=reply, success=all(r.success for r in results), reply=reply,
    )
    dialogue.record_nora(reply, kind=source)
    return reply[:_MAX_REPLY_CHARS]


def decode_audio(data: bytes, *, sample_rate: int = 16000):
    """Decode arbitrary compressed audio to the mono float32 array Whisper wants.

    Telegram voice memos arrive as Opus in an OGG container, which neither
    `soundfile` nor `wave` will open. Shelling out to ffmpeg is the pragmatic
    answer: it is already a de facto dependency of every audio path on Linux,
    and it converts, resamples and downmixes in one pass.

    Returns None when ffmpeg is missing or the audio is unreadable, which the
    caller reports rather than crashing the poll loop.
    """
    import shutil
    import subprocess

    import numpy as np

    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg not found — voice memos cannot be transcribed")
        return None

    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-i", "pipe:0",
             "-f", "f32le", "-ac", "1", "-ar", str(sample_rate), "pipe:1"],
            input=data, capture_output=True, timeout=60,
        )
    except Exception as e:
        logger.warning("ffmpeg failed on voice memo: %s", e)
        return None

    if proc.returncode != 0 or not proc.stdout:
        logger.warning("ffmpeg could not decode voice memo: %s",
                       proc.stderr[:200].decode("utf-8", "replace"))
        return None
    return np.frombuffer(proc.stdout, dtype=np.float32)

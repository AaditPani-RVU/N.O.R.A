"""A headless turn — text in, text out, no microphone and no speaker.

This used to be its own smaller turn: fast path, conversation, intent parser,
security check, execute. "The same guards" was the claim, and it was not
quite true — it skipped both NeuroSym guards and the risk/autonomy/confidence
ladder, so a Telegram message faced fewer checks than the same words spoken
in the room.

Now it is `pipeline.handle_turn` on a text `Channel`: one turn, one set of
guards, whichever device asked. What still differs is only what the channel
can do. A text channel has no `confirm`, so anything the policy wants
confirmed is refused with an explanation rather than run — a remote message
is exactly the context in which "are you sure?" cannot be answered honestly.
Paired devices that *can* ask their user (plan §5, Confirmation) pass their
own `confirm`.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

logger = logging.getLogger("nora.gateway.core")

# Kept short: a gateway reply lands in a chat window, where a wall of text is
# as unwelcome as it is in speech.
_MAX_REPLY_CHARS = 1200

# One tracker across gateway turns, like the voice loop's one tracker across
# spoken turns — frustration is a pattern over turns, not within one.
_frustration = None


async def handle_text(
    text: str,
    *,
    source: str = "gateway",
    device_id: str | None = None,
    confirm: Callable[..., Awaitable[bool]] | None = None,
) -> str:
    """Run one utterance through NORA and return what it would have said."""
    global _frustration
    from nora import pipeline, wiring
    from nora.channel import Channel, Collector
    from nora.frustration import FrustrationTracker

    text = (text or "").strip()
    if not text:
        return ""

    said = Collector()
    if _frustration is None:
        _frustration = FrustrationTracker()
    deps = wiring.build(listener=None, frustration=_frustration, speak=said)
    channel = Channel(device_id=device_id or source, kind="text",
                      speak=said, confirm=confirm)
    await pipeline.handle_turn(text, deps, channel=channel)
    return said.text()[:_MAX_REPLY_CHARS]


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

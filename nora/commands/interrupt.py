"""Interruption handling â€” stop/cancel/pause-everything.

Triggered by "stop", "cancel", "pause everything". Terminates TTS,
stops music, and signals the pipeline to drop any pending steps.
"""
from __future__ import annotations

import logging

from nora import context, speaker
from nora.command_engine import register

logger = logging.getLogger("nora.commands.interrupt")


@register("stop_all")
def stop_all() -> str:
    """Kill TTS + music and signal pipeline cancellation. Fast, no-confirm."""
    logger.info("stop_all invoked â€” halting TTS, music, pending steps")

    # 1. Stop TTS immediately
    try:
        speaker.stop()
    except Exception as e:
        logger.warning("speaker.stop failed: %s", e)

    # 2. Stop all music (local + iTunes/Apple Music COM)
    try:
        from nora.commands.music import stop_music
        stop_music()
    except Exception as e:
        logger.warning("stop_music failed: %s", e)

    # 3. Signal pipeline to drop pending steps
    context.request_cancel()

    return ""  # silent â€” don't speak a response to "stop"


@register("speaker_stop")
def speaker_stop() -> str:
    """Stop only the current TTS utterance."""
    speaker.stop()
    return ""

"""Pre-synthesized acknowledgement tokens for sub-300ms first phoneme response.

At startup, a background thread generates a small pool of short audio clips
("Mm.", "Sure.", "Got it.", etc.) using edge-tts and loads them into pygame
Sound objects. When the user finishes speaking, speak_ack() plays one
immediately — before Whisper or the LLM has had a chance to respond — so
NORA feels present rather than silently processing.
"""
from __future__ import annotations

import asyncio
import logging
import random
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger("nora.ack")

ACK_PHRASES = ["Mm.", "Sure.", "Got it.", "One sec.", "On it.", "Yep."]

_ack_sounds: dict[str, object] = {}  # phrase -> pygame.mixer.Sound
_loaded = threading.Event()
_lock = threading.Lock()
_ack_channel: object | None = None  # pygame.mixer.Channel(1)


def _synth(phrase: str, voice: str, rate: str) -> Path:
    path = Path(tempfile.gettempdir()) / f"nora_ack_{phrase.replace(' ', '_').replace('.', '').lower()}.mp3"
    if path.exists() and path.stat().st_size > 512:
        return path
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_do_synth(phrase, voice, rate, str(path)))
    finally:
        loop.close()
    return path


async def _do_synth(text: str, voice: str, rate: str, path: str) -> None:
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    await communicate.save(path)


def preload(voice: str = "en-GB-SoniaNeural", rate: str = "+40%") -> None:
    """Synthesize and cache all ack tokens. Thread-safe; call once at startup."""
    global _ack_channel
    import pygame

    with _lock:
        if _loaded.is_set():
            return
        if not pygame.mixer.get_init():
            pygame.mixer.init(frequency=24000, size=-16, channels=1, buffer=2048)
        pygame.mixer.set_num_channels(8)
        _ack_channel = pygame.mixer.Channel(1)  # dedicated channel, separate from TTS (ch 0)

        for phrase in ACK_PHRASES:
            try:
                path = _synth(phrase, voice, rate)
                _ack_sounds[phrase] = pygame.mixer.Sound(str(path))
                logger.debug("Ack token cached: %r", phrase)
            except Exception as exc:
                logger.warning("Failed to preload ack %r: %s", phrase, exc)

        _loaded.set()
        logger.info("Ack tokens ready: %d/%d", len(_ack_sounds), len(ACK_PHRASES))


def speak_ack() -> None:
    """Play a random ack token immediately on the dedicated ack channel. Non-blocking."""
    if not _loaded.is_set() or not _ack_sounds:
        return
    try:
        phrase = random.choice(list(_ack_sounds.keys()))
        sound = _ack_sounds[phrase]
        if _ack_channel is not None:
            _ack_channel.play(sound)  # type: ignore[attr-defined]
        else:
            sound.play()  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning("speak_ack failed: %s", exc)

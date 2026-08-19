"""Acknowledgement tokens — the "mm-hm" that says NORA heard you.

The point of an ack is to fill the gap between "you stopped talking" and "NORA
starts answering".  Done well it's the difference between a assistant that
feels present and one that feels dead.  Done badly it's worse than silence.

Three things made the old behaviour grating, all fixed here:

*Too fast.*  Tokens were synthesized at ``+40%`` on top of a voice already
running at ``+20%`` — a compounded ~60% over baseline.  A clipped, chipmunky
"uh huh" reads as a glitch, not a person.  Acks now synthesize at a *slower*
rate than normal speech (see ``_ACK_RATE``), because that's how humans say
them: a real "mm-hm" is lower and lazier than the sentence around it.

*Too often.*  Every single utterance got one.  Nobody says "Got it!" before
every reply; constant acknowledgement is exactly what makes a voice agent feel
like a machine executing a callback.  Acks are now **deferred** — scheduled a
few hundred milliseconds out and cancelled the moment real speech begins.  You
only hear one when NORA is genuinely taking a while, which is precisely when a
person would fill the silence.  A cooldown stops back-to-back acks.

*Too loud, too repetitive.*  Acks now play below the main voice level and
never draw the same token twice in a row.
"""
from __future__ import annotations

import asyncio
import logging
import random
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

logger = logging.getLogger("nora.ack")

# Soft, natural fillers. Trailing punctuation matters — edge-tts and Kokoro
# both use it for prosody, and "Mm-hm." lands far more naturally than "Mm."
ACK_PHRASES = ["Mm-hm.", "Mm.", "Sure.", "Okay.", "Right.", "One sec."]

# Acks are quieter and *slower* than normal speech. This is the fix for the
# chipmunk effect: it's a relative-to-baseline rate, not stacked on the
# speaker's configured rate.
_ACK_RATE = "-10%"
_ACK_VOLUME = 0.55

# Don't ack until the silence is actually awkward. Under this, the real
# response usually arrives first and the ack is cancelled unheard.
_DEFAULT_DELAY_SEC = 0.55

# Minimum gap between two audible acks.
_COOLDOWN_SEC = 6.0

_ack_sounds: dict[str, object] = {}  # phrase -> pygame.mixer.Sound
_loaded = threading.Event()
_lock = threading.RLock()
_ack_channel: object | None = None  # pygame.mixer.Channel(1)

_pending_timer: threading.Timer | None = None
_last_played_at: float = 0.0
_recent: deque[str] = deque(maxlen=2)


def _cfg() -> dict:
    try:
        from nora.config import get_config
        return get_config().get("ack", {}) or {}
    except Exception:
        return {}


def _synth(phrase: str, voice: str, rate: str) -> Path:
    slug = phrase.replace(" ", "_").replace(".", "").replace("-", "").lower()
    path = Path(tempfile.gettempdir()) / f"nora_ack_{slug}_{rate.replace('%', '').replace('+', 'p').replace('-', 'm')}.mp3"
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


def preload(voice: str = "en-GB-SoniaNeural", rate: str | None = None) -> None:
    """Synthesize and cache all ack tokens. Thread-safe; call once at startup.

    ``rate`` defaults to the slower ack rate rather than inheriting the
    speaker's (already boosted) conversational rate.
    """
    global _ack_channel
    import pygame

    cfg = _cfg()
    if not cfg.get("enabled", True):
        logger.info("Ack tokens disabled by config")
        return

    rate = rate or cfg.get("rate", _ACK_RATE)
    volume = float(cfg.get("volume", _ACK_VOLUME))
    phrases = cfg.get("phrases") or ACK_PHRASES

    with _lock:
        if _loaded.is_set():
            return
        if not pygame.mixer.get_init():
            pygame.mixer.init(frequency=24000, size=-16, channels=1, buffer=2048)
        pygame.mixer.set_num_channels(8)
        _ack_channel = pygame.mixer.Channel(1)  # dedicated channel, separate from TTS (ch 0)

        for phrase in phrases:
            try:
                path = _synth(phrase, voice, rate)
                sound = pygame.mixer.Sound(str(path))
                sound.set_volume(volume)
                _ack_sounds[phrase] = sound
                logger.debug("Ack token cached: %r", phrase)
            except Exception as exc:
                logger.warning("Failed to preload ack %r: %s", phrase, exc)

        _loaded.set()
        logger.info("Ack tokens ready: %d/%d @ rate=%s vol=%.2f",
                    len(_ack_sounds), len(phrases), rate, volume)


def _pick() -> str | None:
    """Choose a token, avoiding the ones just used."""
    with _lock:
        available = [p for p in _ack_sounds if p not in _recent] or list(_ack_sounds)
        if not available:
            return None
        phrase = random.choice(available)
        _recent.append(phrase)
        return phrase


def _play_now(force: bool = False) -> None:
    """Actually play a token. Respects the cooldown unless ``force``."""
    global _last_played_at

    if not _loaded.is_set() or not _ack_sounds:
        return

    cooldown = float(_cfg().get("cooldown_sec", _COOLDOWN_SEC))
    with _lock:
        if not force and time.monotonic() - _last_played_at < cooldown:
            logger.debug("Ack suppressed (cooldown)")
            return
        _last_played_at = time.monotonic()

    try:
        phrase = _pick()
        if phrase is None:
            return
        sound = _ack_sounds[phrase]
        if _ack_channel is not None:
            _ack_channel.play(sound)  # type: ignore[attr-defined]
        else:
            sound.play()  # type: ignore[attr-defined]
        logger.debug("Ack played: %r", phrase)
    except Exception as exc:
        logger.warning("ack playback failed: %s", exc)


def speak_ack(delay: float | None = None, force: bool = False) -> None:
    """Schedule an ack token. Non-blocking, and cancellable.

    The ack fires only if ``cancel_ack()`` hasn't been called within ``delay``
    seconds — i.e. only if NORA hasn't started speaking for real yet.  For a
    fast-path command the response beats the timer and nothing is heard, which
    is the desired behaviour: silence, then the answer.

    ``force=True`` plays immediately and ignores the cooldown. Use it for the
    wake cue ("I heard my name, go ahead"), which is a genuine signal the user
    is waiting on — unlike the thinking filler, which should stay rare.
    """
    global _pending_timer

    if not _loaded.is_set() or not _ack_sounds:
        return

    cfg = _cfg()
    if not cfg.get("enabled", True):
        return

    delay = 0.0 if force else float(
        delay if delay is not None else cfg.get("delay_sec", _DEFAULT_DELAY_SEC)
    )

    with _lock:
        if _pending_timer is not None:
            _pending_timer.cancel()
        if delay <= 0:
            _pending_timer = None
            _play_now(force=force)
            return
        _pending_timer = threading.Timer(delay, _play_now)
        _pending_timer.daemon = True
        _pending_timer.start()


def cancel_ack() -> None:
    """Cancel a scheduled ack — real speech is about to start.

    Called from ``speaker.speak()``. Also stops an ack already in flight so it
    doesn't overlap the first syllable of the actual response.
    """
    global _pending_timer

    with _lock:
        if _pending_timer is not None:
            _pending_timer.cancel()
            _pending_timer = None
    try:
        if _ack_channel is not None and _ack_channel.get_busy():  # type: ignore[attr-defined]
            _ack_channel.stop()  # type: ignore[attr-defined]
    except Exception:
        pass

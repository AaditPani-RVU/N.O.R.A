from __future__ import annotations

import asyncio
import io
import logging
import tempfile
import threading
from pathlib import Path

import edge_tts
import pygame

from nora.config import get_config

logger = logging.getLogger("nora.speaker")

# UI server integration â€” imported lazily so speaker works even without the UI
def _ui_notify(speaking: bool, text: str = "") -> None:
    try:
        from nora import ui_server
        ui_server.notify(speaking=speaking, text=text)
    except Exception:
        pass

_lock = threading.Lock()
_initialized = False
_tts_channel: "pygame.mixer.Channel | None" = None


def _init_pygame() -> None:
    """Initialise pygame.mixer with enough channels for music + TTS."""
    global _initialized, _tts_channel
    if not _initialized:
        # 2 channels: channel 0 = TTS, channel 1+ free for sound effects
        pygame.mixer.init(frequency=24000, size=-16, channels=1, buffer=2048)
        pygame.mixer.set_num_channels(8)
        _tts_channel = pygame.mixer.Channel(0)
        _initialized = True


def speak(text: str) -> None:
    """Speak text using Edge TTS with a British male voice."""
    logger.info(f"Speaking: {text}")
    _ui_notify(speaking=True, text=text)
    t = threading.Thread(target=_speak_sync, args=(text,), daemon=True)
    t.start()
    t.join()
    _ui_notify(speaking=False)


def _speak_sync(text: str) -> None:
    """Synthesise speech and play it on the dedicated TTS channel.

    Uses pygame.mixer.Sound + Channel(0) so that background music playing
    on pygame.mixer.music is NEVER stopped or unloaded by TTS.
    """
    with _lock:
        try:
            _init_pygame()
            cfg = get_config().get("speaker", {})
            voice = cfg.get("voice", "en-GB-RyanNeural")
            rate = cfg.get("rate", "+20%")  # Speed boost for snappy NORA feel

            # Generate speech to a temp file
            tmp = Path(tempfile.gettempdir()) / "nora_tts.mp3"

            # Run edge-tts async generation in a new event loop
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_generate_audio(text, voice, rate, str(tmp)))
            finally:
                loop.close()

            # Play on the dedicated TTS channel â€” does NOT touch mixer.music
            sound = pygame.mixer.Sound(str(tmp))
            channel = _tts_channel or pygame.mixer.find_channel(True)
            channel.stop()  # stop any previous TTS utterance on this channel
            channel.play(sound)

            # Wait for this utterance to finish
            while channel.get_busy():
                pygame.time.wait(50)

        except Exception as e:
            logger.error(f"TTS error: {e}", exc_info=True)
            # Fallback to pyttsx3 if edge-tts fails
            _speak_fallback(text)


async def _generate_audio(text: str, voice: str, rate: str, output_path: str) -> None:
    """Generate speech audio using edge-tts."""
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    await communicate.save(output_path)


def _speak_fallback(text: str) -> None:
    """Fallback to pyttsx3 if edge-tts is unavailable."""
    try:
        import pyttsx3
        engine = pyttsx3.init("sapi5")
        engine.setProperty("rate", 185)
        engine.say(text)
        engine.runAndWait()
    except Exception as e:
        logger.error(f"Fallback TTS also failed: {e}")


def stop() -> None:
    """Stop TTS playback only â€” does not affect background music."""
    with _lock:
        try:
            if _initialized and _tts_channel:
                _tts_channel.stop()
        except Exception:
            pass

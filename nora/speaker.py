from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import queue
import re
import tempfile
import threading
from pathlib import Path

import edge_tts
import pygame

from nora.config import get_config

logger = logging.getLogger("nora.speaker")

def _ui_notify(speaking: bool, text: str = "") -> None:
    try:
        from nora import ui_server
        ui_server.notify(speaking=speaking, text=text)
    except Exception:
        pass

_lock = threading.Lock()
_initialized = False
_tts_channel: "pygame.mixer.Channel | None" = None
_stop_requested = False

# ── Mood → rate delta mapping ──────────────────────────────────────────────
# Deltas are percentage-point offsets added to the configured base rate.
_MOOD_DELTA: dict[str, int] = {
    "ack":          20,   # very fast — short ack tokens
    "urgent":       10,   # snappy alerts
    "info":          0,   # neutral status reports (base rate)
    "chat":          0,   # conversational
    "proactive":    -5,   # slightly warmer, less rushed
    "confirmation": -8,   # clear and deliberate
    "error":       -15,   # slower, easier to parse under stress
}


def _get_rate(base_rate: str, mood: str | None) -> str:
    """Return an edge-tts rate string adjusted for the given mood."""
    m = re.match(r"^([+-]?)(\d+)%$", base_rate.strip())
    if not m:
        return base_rate
    sign = -1 if m.group(1) == "-" else 1
    val = sign * int(m.group(2))
    delta = _MOOD_DELTA.get(mood or "info", 0)
    new_val = val + delta
    return f"+{new_val}%" if new_val >= 0 else f"{new_val}%"


def _init_pygame() -> None:
    global _initialized, _tts_channel
    if not _initialized:
        pygame.mixer.init(frequency=24000, size=-16, channels=1, buffer=2048)
        pygame.mixer.set_num_channels(8)
        _tts_channel = pygame.mixer.Channel(0)
        _initialized = True


def _split_sentences(text: str) -> list[str]:
    """Split text into sentence-sized chunks for pipelined playback."""
    parts = re.split(r'(?<=[.!?;])\s+', text.strip())
    return [p.strip() for p in parts if p.strip()]


def speak(text: str, mood: str | None = None) -> None:
    """Speak text with sentence-level pipelining and optional mood-aware prosody.

    mood: "info" | "error" | "urgent" | "confirmation" | "proactive" | "chat" | None
    Each mood shifts the TTS rate relative to the configured base rate.
    """
    global _stop_requested
    if not text or not text.strip():
        return
    logger.info(f"Speaking [{mood or 'info'}]: {text}")
    _stop_requested = False
    _ui_notify(speaking=True, text=text)
    t = threading.Thread(target=_speak_streaming, args=(text, mood), daemon=True)
    t.start()
    t.join()
    _ui_notify(speaking=False)


def _gen_chunk(sentence: str, voice: str, rate: str, path: str) -> None:
    """Generate one sentence's audio file. Designed to run in a thread pool."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_generate_audio(sentence, voice, rate, path))
    finally:
        loop.close()


def _speak_streaming(text: str, mood: str | None = None) -> None:
    """Pipeline TTS: generate sentence N+1 while sentence N is playing.

    For short single-sentence text this behaves identically to the old path.
    For long responses (ask_claude, tell_me_about, multi-step summaries) the
    user hears the first sentence ~500 ms after speak() is called instead of
    waiting for the entire response to be synthesised first.
    """
    global _stop_requested

    sentences = _split_sentences(text)
    if not sentences:
        return

    cfg = get_config().get("speaker", {})
    voice = cfg.get("voice", "en-GB-RyanNeural")
    base_rate = cfg.get("rate", "+20%")
    rate = _get_rate(base_rate, mood)
    tmp_dir = Path(tempfile.gettempdir())

    with _lock:
        _init_pygame()

    # ── Barge-in monitor ─────────────────────────────────────────────────────
    bi_cfg = get_config().get("barge_in", {})
    barge_in_enabled = bi_cfg.get("enabled", True)
    bi_threshold = float(bi_cfg.get("threshold_rms", 0.06))
    bi_delay = float(bi_cfg.get("delay_sec", 0.35))

    def _barge_in_monitor() -> None:
        """Watch the mic. If sustained speech is detected, stop TTS playback."""
        import time as _t
        import sounddevice as _sd
        import numpy as _np

        _t.sleep(bi_delay)  # let TTS audio settle before watching mic
        window: list[float] = []

        def cb(indata: _np.ndarray, *_: object) -> None:
            mono = indata[:, 0] if indata.ndim > 1 else indata.flatten()
            window.append(float(_np.max(_np.abs(mono))))
            if len(window) > 8:
                window.pop(0)

        try:
            with _sd.InputStream(channels=1, dtype="float32", callback=cb,
                                 blocksize=512, samplerate=16000):
                while True:
                    with _lock:
                        ch = _tts_channel
                    if ch is None or not ch.get_busy():
                        break
                    if _stop_requested:
                        break
                    # Require 5+ consecutive frames above threshold (≈ 50 ms of speech)
                    if len(window) >= 5 and all(v >= bi_threshold for v in window[-5:]):
                        logger.info("Barge-in detected — suppressing TTS")
                        stop()
                        break
                    _t.sleep(0.01)
        except Exception as exc:
            logger.debug("Barge-in monitor error: %s", exc)

    if barge_in_enabled:
        threading.Thread(target=_barge_in_monitor, daemon=True, name="nora-barge-in").start()

    ready: queue.Queue[tuple[int, str | None]] = queue.Queue()

    def gen(idx: int) -> None:
        path = str(tmp_dir / f"nora_tts_{idx}.mp3")
        try:
            _gen_chunk(sentences[idx], voice, rate, path)
            ready.put((idx, path))
        except Exception as e:
            logger.warning("TTS generation failed for chunk %d: %s", idx, e)
            ready.put((idx, None))

    n = len(sentences)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    submitted = 0
    pending: dict[int, str | None] = {}

    # Pre-generate first two sentences so playback starts immediately
    for _ in range(min(2, n)):
        executor.submit(gen, submitted)
        submitted += 1

    try:
        for i in range(n):
            if _stop_requested:
                break

            # Collect completed chunks until sentence i is ready
            while i not in pending:
                try:
                    idx, path = ready.get(timeout=15.0)
                    pending[idx] = path
                except Exception:
                    pending[i] = None
                    break

            path = pending.pop(i, None)
            if not path or _stop_requested:
                continue

            try:
                sound = pygame.mixer.Sound(path)
                with _lock:
                    channel = _tts_channel or pygame.mixer.find_channel(True)
                    channel.stop()
                    channel.play(sound)

                # Kick off the next sentence generation while this one plays
                if submitted < n:
                    executor.submit(gen, submitted)
                    submitted += 1

                # Wait for playback, checking the stop flag every 50 ms
                while channel.get_busy():
                    if _stop_requested:
                        channel.stop()
                        break
                    pygame.time.wait(50)

            except Exception as e:
                logger.warning("TTS playback failed for chunk %d: %s", i, e)
                _speak_fallback(sentences[i])

    finally:
        executor.shutdown(wait=False)


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
    """Stop TTS playback only — does not affect background music."""
    global _stop_requested
    _stop_requested = True
    with _lock:
        try:
            if _initialized and _tts_channel:
                _tts_channel.stop()
        except Exception:
            pass

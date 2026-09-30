"""NORA's voice on a device: a spoken turn's answer, synthesised here and streamed.

A voice session on the phone does its own speech recognition (on-device, plan
§9.3) and sends the text as an ordinary `utterance`, marked `voice`. If it asks
for the core's voice (`voice.tts == "core"`), each line NORA says in that turn
is also synthesised here with the same Kokoro voice the laptop speaks in, and
streamed to the phone as raw PCM while the rest is still being made:

    say        {text, mood, turn_id, audio: {stream, format, rate}}
    [binary]   0x01 · stream (u32, big-endian) · PCM s16le mono      (repeated)
    audio.end  {stream, ok, cancelled}

`audio` is left out when the core can't synthesise (Kokoro off or broken);
the phone then speaks the line with its own text-to-speech. `ok: false` after
no audio at all means the same. The first chunk of a line is cut short (the
first clause) so the first audio leaves as early as it can.

`voice.barge_in` from the device (corr = the utterance) stops synthesis for
that turn. The turn itself runs on: what was asked may already be happening,
and its lines still arrive as text.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from pathlib import Path
from typing import Callable

logger = logging.getLogger("nora.hub.voice")

TAG_PCM = 0x01
FORMAT = "pcm_s16le"
KOKORO_RATE = 24000
# Under protocol.MAX_FRAME with room to spare; ~0.7 s of audio per frame.
FRAME_BYTES = 32 * 1024
# The first chunk of a line is cut at a clause break past this many
# characters, if the first sentence is longer than FIRST_MAX.
# Kokoro on the core's CPU costs ~250 ms plus ~20 ms a character (measured
# 2026-09-30: "Done." 0.28 s, a 49-character sentence 1.9 s), so the first
# chunk is kept to a clause.
FIRST_MIN = 12
FIRST_MAX = 40

_SENTENCE = re.compile(r"(?<=[.!?;:])\s+")
_CLAUSE = re.compile(r"(?<=[,;:—–])\s+|\s+(?=—|–)")


def chunks(text: str) -> list[str]:
    """Sentences, with the first one cut at its first clause break when long.

    Every sentence is its own chunk: synthesis is local, so there is no
    round trip to amortise by merging them (unlike edge-tts in `speaker`).
    """
    parts = [p.strip() for p in _SENTENCE.split(text.strip()) if p.strip()]
    if not parts:
        return []
    first = parts[0]
    if len(first) > FIRST_MAX:
        for m in _CLAUSE.finditer(first):
            if FIRST_MIN <= m.start() <= FIRST_MAX:
                return [first[:m.start()].strip(), first[m.end():].strip()] + parts[1:]
    return parts


def pcm_frame(stream: int, pcm: bytes) -> bytes:
    return bytes([TAG_PCM]) + stream.to_bytes(4, "big") + pcm


def parse_frame(frame: bytes) -> tuple[int, bytes]:
    """(stream, pcm). Raises ValueError on anything else."""
    if len(frame) < 5 or frame[0] != TAG_PCM:
        raise ValueError("not a PCM frame")
    return int.from_bytes(frame[1:5], "big"), frame[5:]


Synth = Callable[[str, str], "tuple[bytes, int] | None"]


def _kokoro(text: str, rate: str) -> tuple[bytes, int] | None:
    from nora import tts_local
    return tts_local.synth_pcm(text, rate)


def core_voice_available() -> bool:
    from nora import tts_local
    try:
        return tts_local.available()
    except Exception:
        return False


def rate_for(mood: str | None) -> str:
    from nora.config import get_config
    from nora.speaker import _get_rate
    return _get_rate(get_config().get("speaker", {}).get("rate", "+20%"), mood)


class VoiceOut:
    """One voice turn's speech to one device, synthesised in order.

    `line()` may be called from any thread (the pipeline speaks from
    executors); synthesis runs one chunk at a time on the executor, and every
    frame goes out through the session's single writer, after the `say` that
    announced its stream.
    """

    def __init__(self, session, *, synth: Synth | None = None,
                 rate: Callable[[str | None], str] | None = None,
                 sample_rate: int = KOKORO_RATE) -> None:
        self.session = session
        self.synth = synth
        self.rate = rate
        self.sample_rate = sample_rate
        self.cancelled = False
        self._on_done: Callable[[], object] | None = None
        self._lock = threading.Lock()
        self._queue: asyncio.Queue = asyncio.Queue()
        # Built on the hub's loop, inside the turn.
        self._task = asyncio.ensure_future(self._run())

    def line(self, text: str, mood: str | None) -> dict | None:
        """Queue one line; returns the `audio` block for its `say`, or None when cancelled."""
        with self._lock:
            if self.cancelled:
                return None
            stream = self.session.next_stream()
        self.session.loop.call_soon_threadsafe(self._queue.put_nowait, (stream, text, mood))
        return {"stream": stream, "format": FORMAT, "rate": self.sample_rate}

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True

    def close(self, on_done: Callable[[], object] | None = None) -> None:
        """No more lines; the worker finishes what is queued, calls `on_done`, and stops."""
        self._on_done = on_done
        self.session.loop.call_soon_threadsafe(self._queue.put_nowait, None)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            item = await self._queue.get()
            if item is None:
                if self._on_done is not None:
                    self._on_done()
                return
            stream, text, mood = item
            sent = False
            ok = True
            try:
                rate = (self.rate or rate_for)(mood)
                synth = self.synth or _kokoro
                for chunk in chunks(text):
                    if self.cancelled:
                        break
                    out = await loop.run_in_executor(None, synth, chunk, rate)
                    if out is None:
                        ok = False
                        break
                    pcm, _sr = out
                    for i in range(0, len(pcm), FRAME_BYTES):
                        if self.cancelled:
                            break
                        self.session.send_bytes_nowait(pcm_frame(stream, pcm[i:i + FRAME_BYTES]))
                        sent = True
            except Exception as e:
                logger.warning("voice synthesis failed: %s", e)
                ok = False
            self.session.send_nowait("audio.end", {"stream": stream, "ok": ok and not self.cancelled,
                                                   "sent": sent, "cancelled": self.cancelled})


# ── latency measurements from devices ────────────────────────────────────────

LATENCY_PATH = Path(__file__).resolve().parents[2] / "nora_voice_latency.jsonl"
_FIELDS = ("speech_end_ms", "stt_ms", "core_first_say_ms", "first_audio_ms", "tts", "route")


def record_turn(device_id: str, data: dict) -> dict:
    """A `voice.turn` event: log it and keep it in `nora_voice_latency.jsonl`."""
    import json
    import time

    row = {"ts": time.time(), "device": device_id}
    for k in _FIELDS:
        v = data.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            row[k] = int(v)
        elif isinstance(v, str):
            row[k] = v[:40]
    logger.info("voice turn on %s: %s", device_id,
                ", ".join(f"{k}={row[k]}" for k in _FIELDS if k in row))
    try:
        with LATENCY_PATH.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as e:
        logger.debug("could not keep voice latency: %s", e)
    return row

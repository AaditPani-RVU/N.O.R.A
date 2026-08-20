"""Tee NORA's spoken audio to connected WebSocket clients.

The laptop still plays every reply through pygame exactly as before -- this
mirrors the same MP3 chunks to the dashboard, so a phone on the tailnet hears
NORA at the same time. Nothing here can affect local playback: every failure
path is swallowed, because a browser that isn't listening must never be able to
stall or silence the speaker on the machine actually running the pipeline.

Chunks arrive sentence-by-sentence from `speaker._speak_streaming`, which is
what keeps remote playback roughly in step with local playback rather than
waiting for a whole reply to finish synthesising.
"""
from __future__ import annotations

import base64
import logging
import threading
from pathlib import Path

logger = logging.getLogger("nora.audio_relay")

# A sentence of edge-tts MP3 is tens of KB. Anything past this is a bug or a
# pathological reply; dropping it beats blocking the event loop on a big frame.
_MAX_CHUNK_BYTES = 2 * 1024 * 1024

_seq = 0
_seq_lock = threading.Lock()
_enabled: bool | None = None


def sniff_mime(raw: bytes) -> str:
    """Identify the container from its magic bytes.

    The filename is not evidence: speaker.py writes every chunk to
    `nora_tts_N.mp3` regardless of backend, and the local Kokoro path fills it
    with RIFF/WAVE. pygame sniffs content so local playback never noticed, but a
    browser hands `data:audio/mpeg` straight to its MP3 decoder, which rejects a
    WAV header and fails the play() promise -- silently, since autoplay
    rejections look identical.
    """
    if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        return "audio/wav"
    if raw[:4] == b"OggS":
        return "audio/ogg"
    if raw[:4] == b"fLaC":
        return "audio/flac"
    if raw[4:8] == b"ftyp":
        return "audio/mp4"
    if raw[:3] == b"ID3" or (raw[:1] == b"\xff" and raw[1:2] in (b"\xfb", b"\xf3", b"\xf2", b"\xfa")):
        return "audio/mpeg"
    return "audio/mpeg"


def enabled() -> bool:
    """Config gate, resolved once. `audio_relay.enabled` under websocket_api."""
    global _enabled
    if _enabled is None:
        try:
            from nora.config import get_config
            ws_cfg = (get_config().get("websocket_api") or {})
            _enabled = bool(ws_cfg.get("enabled", True)) and bool(
                ws_cfg.get("relay_audio", True)
            )
        except Exception:
            _enabled = False
    return _enabled


def _next_seq() -> int:
    global _seq
    with _seq_lock:
        _seq += 1
        return _seq


def push_chunk(path: str | Path, text: str = "") -> None:
    """Mirror one synthesised MP3 chunk to WebSocket clients."""
    if not enabled():
        return
    try:
        from nora import ui_server
        if not ui_server.has_ws_clients():
            return  # nobody listening -- skip the base64 entirely

        raw = Path(path).read_bytes()
        if not raw or len(raw) > _MAX_CHUNK_BYTES:
            logger.debug("Skipping relay of %d-byte chunk", len(raw))
            return

        ui_server.ws_push({
            "type": "audio",
            "seq": _next_seq(),
            "mime": sniff_mime(raw),
            "text": text,
            "data": base64.b64encode(raw).decode("ascii"),
        })
    except Exception as e:
        logger.debug("Audio relay failed (local playback unaffected): %s", e)


def push_stop() -> None:
    """Tell remote listeners to drop whatever is queued -- barge-in or stop()."""
    if not enabled():
        return
    try:
        from nora import ui_server
        if ui_server.has_ws_clients():
            ui_server.ws_push({"type": "audio_stop", "seq": _next_seq()})
    except Exception:
        pass

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
            "mime": "audio/mpeg",
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

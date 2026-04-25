"""Ambient transcription and personal knowledge base.

Runs a background thread that captures mic audio in 5s chunks and logs
any detected speech to nora_knowledge.json. All NORA commands are
also logged regardless of ambient mode.

Enabled via config.yaml: ambient.enabled: true
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import sounddevice as sd

from nora import transcriber
from nora.config import get_config

logger = logging.getLogger("nora.ambient")

_PATH = Path(__file__).resolve().parent.parent / "nora_knowledge.json"
_lock = threading.RLock()
_running = False
_thread: threading.Thread | None = None


# â”€â”€ Storage â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _read() -> dict[str, Any]:
    try:
        if _PATH.exists():
            return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"entries": []}


def log_entry(text: str, source: str = "command", tags: list[str] | None = None) -> None:
    """Append a transcribed entry to the knowledge base."""
    text = text.strip()
    if not text:
        return
    with _lock:
        data = _read()
        data["entries"].insert(0, {
            "text": text,
            "source": source,   # "command" | "ambient"
            "tags": tags or [],
            "ts": time.time(),
        })
        del data["entries"][2000:]
        try:
            _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("Knowledge base write failed: %s", e)


def search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Keyword search over the knowledge base. Returns most relevant entries."""
    with _lock:
        data = _read()
    entries = data.get("entries", [])
    if not entries:
        return []

    terms = [t for t in query.lower().split() if len(t) > 2]
    if not terms:
        return entries[:limit]

    scored = []
    for entry in entries:
        text_lower = entry["text"].lower()
        score = sum(1 for t in terms if t in text_lower)
        if score > 0:
            scored.append((score, entry["ts"], entry))

    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [e for _, _, e in scored[:limit]]


def entry_count() -> int:
    with _lock:
        return len(_read().get("entries", []))


# â”€â”€ Background ambient recording loop â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _ambient_loop(sample_rate: int, chunk_sec: float) -> None:
    global _running
    logger.info("Ambient transcription loop started (chunk=%.1fs)", chunk_sec)

    while _running:
        frames: list[np.ndarray] = []

        def cb(indata: np.ndarray, frames_count: int, time_info: Any, status: Any) -> None:
            frames.append(indata.copy())

        try:
            with sd.InputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="float32",
                callback=cb,
                blocksize=1024,
                latency="low",
            ):
                time.sleep(chunk_sec)
        except Exception as e:
            logger.debug("Ambient stream error (skipping chunk): %s", e)
            time.sleep(2.0)
            continue

        if not frames:
            continue

        audio = np.concatenate(frames, axis=0).flatten()
        rms = float(np.sqrt(np.mean(audio ** 2)))

        if rms < 0.005:
            continue

        try:
            text = transcriber.transcribe(audio)
            if text and len(text.strip()) > 4:
                log_entry(text.strip(), source="ambient")
                logger.debug("Ambient logged: %s", text[:80])
        except Exception as e:
            logger.debug("Ambient transcription failed: %s", e)

    logger.info("Ambient transcription loop stopped")


def start() -> None:
    """Start the ambient background thread if enabled in config."""
    global _running, _thread
    if _running:
        return

    cfg = get_config().get("ambient", {})
    if not cfg.get("enabled", False):
        logger.debug("Ambient transcription disabled (set ambient.enabled: true to enable)")
        return

    sample_rate = cfg.get("sample_rate", 16000)
    chunk_sec = float(cfg.get("chunk_sec", 5.0))

    _running = True
    _thread = threading.Thread(
        target=_ambient_loop,
        args=(sample_rate, chunk_sec),
        daemon=True,
        name="nora-ambient",
    )
    _thread.start()
    logger.info("Ambient transcription started")


def stop() -> None:
    global _running
    _running = False

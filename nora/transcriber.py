from __future__ import annotations

import logging
import re

import numpy as np
from faster_whisper import WhisperModel

from nora.config import get_config

logger = logging.getLogger("nora.transcriber")

_model: WhisperModel | None = None


def _get_model() -> WhisperModel:
    global _model
    if _model is None:
        cfg = get_config().get("transcriber", {})
        model_size = cfg.get("model_size", "base.en")
        device = cfg.get("device", "cpu")
        compute_type = cfg.get("compute_type", "int8")
        logger.info(f"Loading Whisper model: {model_size} on {device} ({compute_type})")
        _model = WhisperModel(model_size, device=device, compute_type=compute_type)
        logger.info("Whisper model loaded.")
    return _model


def _transcribe_remote(audio: np.ndarray, cfg: dict) -> str:
    """Transcribe via an OpenAI-compatible /audio/transcriptions endpoint.

    Measured against the local distil-small.en on CUDA with identical clips:
    287ms median remote vs 1316ms local, with the same words out. The remote
    model (whisper-large-v3-turbo) is also far larger than the local one, so
    it should hold up better on a poor microphone, not worse — which is the
    opposite of the usual cloud-vs-local tradeoff and the reason this is
    worth the network dependency.

    Raises on any failure so ``transcribe`` can fall back to the local model.
    """
    import io
    import os
    import wave

    api_key = os.environ.get(cfg.get("api_key_env", "GROQ_API_KEY"), "")
    if not api_key:
        raise EnvironmentError(f"{cfg.get('api_key_env', 'GROQ_API_KEY')} not set")

    # The endpoint wants a file, not an array. 16-bit PCM in memory — writing
    # to disk for a two-second clip costs more than the encode does.
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(get_config().get("listener", {}).get("sample_rate", 16000)))
        w.writeframes((np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes())
    buf.seek(0)
    buf.name = "speech.wav"  # the SDK infers the mime type from the filename

    from openai import OpenAI
    client = OpenAI(
        api_key=api_key,
        base_url=cfg.get("base_url", "https://api.groq.com/openai/v1"),
        timeout=float(cfg.get("timeout_sec", 10)),
    )
    resp = client.audio.transcriptions.create(
        model=cfg.get("remote_model", "whisper-large-v3-turbo"),
        file=buf,
        language="en",
    )
    return (resp.text or "").strip()


def transcribe(audio: np.ndarray) -> str:
    """Transcribe a float32 numpy audio array to text.

    Uses the remote endpoint when ``transcriber.backend`` is "remote", falling
    back to the local model on any failure — a flaky network degrades the turn
    to a slower one rather than a lost one.
    """
    # faster-whisper expects float32 numpy array
    if audio.dtype != np.float32:
        audio = audio.astype(np.float32)

    cfg = get_config().get("transcriber", {})
    if cfg.get("backend", "local") == "remote":
        try:
            text = _transcribe_remote(audio, cfg)
            if text:
                text = re.sub(r"\s+", " ", text).strip()
                logger.info(f"Transcribed (remote): '{text}'")
                return text
            logger.warning("Remote transcription returned nothing — trying local")
        except Exception as e:
            logger.warning(f"Remote transcription failed ({e}) — falling back to local")

    model = _get_model()

    segments, info = model.transcribe(
        audio,
        beam_size=1,
        language="en",
        vad_filter=True,
        condition_on_previous_text=False,
    )

    text_parts = []
    for segment in segments:
        text_parts.append(segment.text.strip())

    text = " ".join(text_parts).strip()

    # Clean up filler words and extra whitespace
    text = re.sub(r"\s+", " ", text)
    text = text.strip()

    logger.info(f"Transcribed: '{text}'")
    return text

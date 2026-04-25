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


def transcribe(audio: np.ndarray) -> str:
    """Transcribe a float32 numpy audio array to text."""
    model = _get_model()

    # faster-whisper expects float32 numpy array
    if audio.dtype != np.float32:
        audio = audio.astype(np.float32)

    segments, info = model.transcribe(audio, beam_size=5, language="en")

    text_parts = []
    for segment in segments:
        text_parts.append(segment.text.strip())

    text = " ".join(text_parts).strip()

    # Clean up filler words and extra whitespace
    text = re.sub(r"\s+", " ", text)
    text = text.strip()

    logger.info(f"Transcribed: '{text}'")
    return text

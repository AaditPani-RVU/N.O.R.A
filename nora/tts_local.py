"""Local TTS backend — Kokoro-82M via kokoro-onnx (see STACK.md).

Kokoro-82M (Apache 2.0) is the best free *local* TTS as of 2026: 82M
params, faster than realtime on CPU, quality comparable to cloud voices.
edge-tts stays the default (zero install, excellent quality) — this
backend exists so NORA can speak with the network down, or fully
offline by choice.

Enable:
  pip install kokoro-onnx
  # model + voices (~340 MB total, one-time):
  #   https://github.com/thewh1teagle/kokoro-onnx/releases  →  kokoro-v1.0.onnx, voices-v1.0.bin
  config.yaml:
    speaker:
      backend: "kokoro"
      kokoro:
        model_path: "~/models/kokoro-v1.0.onnx"
        voices_path: "~/models/voices-v1.0.bin"
        voice: "bf_emma"        # British female, matches NORA's persona

Fail-soft: any missing piece logs once and speaker.py falls back to
edge-tts for that chunk and every later one.
"""
from __future__ import annotations

import logging
import re
import threading
import wave
from pathlib import Path

from nora.config import get_config

logger = logging.getLogger("nora.tts_local")

_lock = threading.Lock()
_engine = None          # kokoro_onnx.Kokoro, lazily constructed
_failed = False         # once true, never retry this process (log once, stay quiet)


def _kokoro_cfg() -> dict:
    return get_config().get("speaker", {}).get("kokoro", {})


def enabled() -> bool:
    return get_config().get("speaker", {}).get("backend", "edge") == "kokoro"


def _get_engine():
    global _engine, _failed
    with _lock:
        if _engine is not None or _failed:
            return _engine
        try:
            from kokoro_onnx import Kokoro
            cfg = _kokoro_cfg()
            model = Path(cfg.get("model_path", "~/models/kokoro-v1.0.onnx")).expanduser()
            voices = Path(cfg.get("voices_path", "~/models/voices-v1.0.bin")).expanduser()
            if not model.exists() or not voices.exists():
                raise FileNotFoundError(f"kokoro model files missing ({model}, {voices})")
            _engine = Kokoro(str(model), str(voices))
            logger.info("Kokoro local TTS loaded: %s", model.name)
        except Exception as e:
            _failed = True
            logger.warning("Kokoro backend unavailable (%s) — falling back to edge-tts", e)
        return _engine


def _rate_to_speed(rate: str) -> float:
    """Map an edge-tts rate string ("+28%") to a Kokoro speed multiplier."""
    m = re.match(r"^([+-]?)(\d+)%$", rate.strip())
    if not m:
        return 1.0
    val = int(m.group(2)) * (-1 if m.group(1) == "-" else 1)
    return max(0.5, min(2.0, 1.0 + val / 100))


def synth_if_enabled(text: str, rate: str, out_path: str) -> bool:
    """Synthesize locally when the kokoro backend is selected and healthy.

    Returns True if out_path now holds playable audio; False means the
    caller should use edge-tts as usual.
    """
    if not enabled():
        return False
    engine = _get_engine()
    if engine is None:
        return False
    try:
        voice = _kokoro_cfg().get("voice", "bf_emma")
        samples, sample_rate = engine.create(text, voice=voice, speed=_rate_to_speed(rate))
        pcm = (samples.clip(-1.0, 1.0) * 32767).astype("int16")
        with wave.open(out_path, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm.tobytes())
        return True
    except Exception as e:
        logger.warning("Kokoro synthesis failed (%s) — falling back to edge-tts", e)
        return False

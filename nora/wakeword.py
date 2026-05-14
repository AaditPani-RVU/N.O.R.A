"""Always-on wakeword detection using openWakeWord.

Runs a continuous microphone stream in a background thread. When the
configured wakeword is detected, it fires an event that the pipeline
picks up in the next listen() call.

Default model: hey_jarvis_v0.1 (closest available pre-trained model).
Set wakeword.model in config.yaml to any openWakeWord model name.

openWakeWord is optional. If not installed, this module degrades silently
and is_enabled() returns False so the rest of NORA is unaffected.
"""
from __future__ import annotations

import logging
import queue
import threading
import time

import numpy as np

from nora.config import get_config

logger = logging.getLogger("nora.wakeword")

_enabled = False
_detector: "_WakewordDetector | None" = None


def is_enabled() -> bool:
    return _enabled and _detector is not None


def start() -> None:
    """Start the wakeword detector if enabled in config. Call once at startup."""
    global _enabled, _detector
    cfg = get_config().get("wakeword", {})
    if not cfg.get("enabled", False):
        logger.debug("Wakeword detection disabled in config.")
        return
    try:
        import openwakeword  # noqa: F401
    except ImportError:
        logger.warning(
            "openwakeword not installed — wakeword detection unavailable. "
            "Run: pip install openwakeword"
        )
        return

    model = cfg.get("model", "hey_jarvis_v0.1")
    sensitivity = float(cfg.get("sensitivity", 0.5))
    cooldown = float(cfg.get("cooldown_sec", 1.5))

    try:
        _detector = _WakewordDetector(model, sensitivity, cooldown)
        _detector.start()
        _enabled = True
        logger.info("Wakeword detector started (model=%s, sensitivity=%.2f)", model, sensitivity)
    except Exception as exc:
        logger.error("Failed to start wakeword detector: %s", exc)


def stop() -> None:
    global _enabled
    _enabled = False
    if _detector is not None:
        _detector.stop()


def wait_for_trigger(timeout: float = 0.1) -> bool:
    """Return True if a wakeword event was detected within timeout seconds."""
    if not is_enabled() or _detector is None:
        return False
    return _detector.wait(timeout=timeout)


class _WakewordDetector:
    def __init__(self, model_name: str, sensitivity: float, cooldown: float) -> None:
        self._model_name = model_name
        self._sensitivity = sensitivity
        self._cooldown = cooldown
        self._event = threading.Event()
        self._running = False
        self._thread: threading.Thread | None = None
        self._audio_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=20)

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="nora-wakeword")
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def wait(self, timeout: float) -> bool:
        triggered = self._event.wait(timeout=timeout)
        if triggered:
            self._event.clear()
        return triggered

    def _run(self) -> None:
        import sounddevice as sd
        from openwakeword.model import Model

        logger.debug("Loading wakeword model: %s", self._model_name)
        try:
            model = Model(wakeword_models=[self._model_name], inference_framework="onnx")
        except Exception as exc:
            logger.error("openWakeWord model load failed: %s", exc)
            return

        CHUNK = 1280  # 80 ms at 16 kHz — openwakeword's required frame size
        last_trigger = 0.0

        def _mic_callback(
            indata: np.ndarray, frames: int, time_info: object, status: object
        ) -> None:
            if not self._running:
                return
            mono = indata[:, 0] if indata.ndim > 1 else indata.flatten()
            try:
                self._audio_q.put_nowait(mono.copy())
            except queue.Full:
                pass

        try:
            with sd.InputStream(
                samplerate=16000,
                channels=1,
                dtype="float32",
                blocksize=CHUNK,
                callback=_mic_callback,
            ):
                logger.info("Wakeword mic stream open — listening for %r", self._model_name)
                while self._running:
                    try:
                        chunk = self._audio_q.get(timeout=0.5)
                    except queue.Empty:
                        continue

                    # openWakeWord expects int16 audio
                    chunk_int16 = (chunk * 32767).astype(np.int16)
                    try:
                        prediction = model.predict(chunk_int16)
                    except Exception as exc:
                        logger.debug("Wakeword predict error: %s", exc)
                        continue

                    now = time.monotonic()
                    for score in prediction.values():
                        val = float(score) if not hasattr(score, "__iter__") else float(max(score))
                        if val >= self._sensitivity and (now - last_trigger) >= self._cooldown:
                            logger.info(
                                "Wakeword triggered! score=%.3f model=%s",
                                val, self._model_name,
                            )
                            last_trigger = now
                            self._event.set()
                            break

        except Exception as exc:
            logger.error("Wakeword detector crashed: %s", exc)

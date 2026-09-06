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
from pathlib import Path
from typing import Callable

import numpy as np

from nora.config import get_config

logger = logging.getLogger("nora.wakeword")

_enabled = False
_detector: "_WakewordDetector | None" = None
_wake_callbacks: list[Callable] = []


def register_on_wake_callback(cb: Callable[[], None]) -> None:
    """Register a callback that fires each time the wakeword is triggered.

    Used by the PipeWire ducker (F5) to duck music on wake. No-op when no
    subscriber is registered; wakeword behavior is otherwise unchanged.
    """
    _wake_callbacks.append(cb)


def is_enabled() -> bool:
    return _enabled and _detector is not None


def _display(model: str) -> str:
    """The name to log for a model, whether it is a path or a pretrained id.

    Not Path.stem: a pretrained id carries its version in what looks like a
    suffix, so stem turns "hey_jarvis_v0.1" into "hey_jarvis_v0" and reports a
    model nobody configured. Only a real model-file extension is stripped.
    """
    name = model.rsplit("/", 1)[-1]
    for ext in (".onnx", ".tflite"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def _resolve_models(spec) -> list[str]:
    """Turn the `wakeword.model` config value into what openWakeWord accepts.

    Two kinds of value are allowed and they are told apart by whether the
    string looks like a path. A bare name ("hey_jarvis_v0.1") is a model that
    ships inside the wheel and is handed through untouched; anything with a
    separator or an .onnx suffix is a custom-trained model on disk.

    Custom models have to be resolved here because openWakeWord does not do it:
    it treats a path it cannot open as a *pretrained name*, so a `~/` that was
    never expanded fails with "Could not find pretrained model for model name
    '~/hey_nora.onnx'" — which points at the wrong problem entirely. Relative
    paths resolve against the project root rather than the launch directory,
    for the same reason claude_logs does it.

    A list is accepted so several phrasings ("hey nora", "hi nora") can run
    together; openWakeWord scores every loaded model on the same frame, so the
    cost of a second one is a second small matmul, not a second mic stream.
    """
    specs = spec if isinstance(spec, (list, tuple)) else [spec]
    root = Path(__file__).resolve().parent.parent
    out: list[str] = []
    for item in specs:
        name = str(item).strip()
        if not name:
            continue
        if "/" not in name and "\\" not in name and not name.endswith(".onnx"):
            out.append(name)  # pretrained, shipped in the wheel
            continue
        path = Path(name).expanduser()
        if not path.is_absolute():
            path = root / path
        if not path.is_file():
            logger.error(
                "Wakeword model not found: %s (from config wakeword.model=%r)",
                path, item,
            )
            continue
        out.append(str(path))
    return out


def _resolve_device(spec) -> int | None:
    """Turn `wakeword.input_device` into a sounddevice index, or None for default.

    Worth a config knob because the system default is not always the microphone
    that can hear you. This machine has two internal inputs and the default one
    delivers a clipping DC offset with no energy above 4 kHz — the wake word ran
    for hours against it and could not have fired once. `mic_probe.py --scan`
    names the input that hears speech; this is what accepts that name.

    An int is used as-is. A string is matched case-insensitively as a substring
    of the device name — but note these are the names *sounddevice* reports
    ("HD-Audio Generic: ALC245 Analog (hw:2,0)"), not the friendlier ones from
    `wpctl status`, which PortAudio cannot see. A PipeWire node name will not
    match anything here; use `wpctl set-default` for those, or an index.

    Whatever comes out is then checked against the format the detector actually
    opens — 16 kHz mono float32. That check is the point of this function as
    much as the lookup is: a raw ALSA device can match by name and still refuse
    16 kHz (hw:2,0 on this machine does), and without the check that lands as a
    stream error on the detector thread, which kills the wake word outright. A
    device we cannot open is worth less than the default, so we say why and fall
    back rather than take the configured value on faith.
    """
    if spec is None or spec == "":
        return None

    try:
        import sounddevice as sd
    except Exception as exc:
        logger.error("Could not load sounddevice (%s) — using the default input.", exc)
        return None

    if isinstance(spec, int):
        index = spec
    else:
        try:
            want = str(spec).lower()
            matches = [
                i for i, d in enumerate(sd.query_devices())
                if d["max_input_channels"] > 0 and want in d["name"].lower()
            ]
        except Exception as exc:
            logger.error("Could not enumerate input devices (%s) — using the default.", exc)
            return None
        if not matches:
            # Falling back to the default is right: a typo should cost the tuned
            # device, not the wake word entirely.
            logger.error("No input device matches %r — falling back to the system default.", spec)
            return None
        if len(matches) > 1:
            logger.warning("input_device %r matches %d devices; using the first.", spec, len(matches))
        index = matches[0]

    try:
        sd.check_input_settings(
            device=index, samplerate=16000, channels=1, dtype="float32",
        )
    except Exception as exc:
        logger.error(
            "Input device %r (index %d) cannot capture 16 kHz mono (%s) — falling back "
            "to the system default. If that default is the wrong microphone, point "
            "PipeWire at the right one with `wpctl set-default <id>` instead; see "
            "training/wakeword/mic_probe.py --scan.",
            spec, index, exc,
        )
        return None
    return index


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

    models = _resolve_models(cfg.get("model", "hey_jarvis_v0.1"))
    sensitivity = float(cfg.get("sensitivity", 0.5))
    cooldown = float(cfg.get("cooldown_sec", 1.5))
    device = _resolve_device(cfg.get("input_device"))

    if not models:
        # Every configured model failed to resolve. Starting anyway would load
        # openWakeWord's full pretrained set and wake on "alexa".
        logger.error("No usable wakeword model — detection stays off.")
        return

    try:
        _detector = _WakewordDetector(models, sensitivity, cooldown, device)
        _detector.start()
        _enabled = True
        logger.info("Wakeword detector started (models=%s, sensitivity=%.2f, device=%s)",
                    ", ".join(_display(m) for m in models), sensitivity,
                    "default" if device is None else device)
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
    def __init__(self, model_names: list[str], sensitivity: float, cooldown: float,
                 device: int | None = None) -> None:
        self._model_names = list(model_names)
        self._sensitivity = sensitivity
        self._cooldown = cooldown
        self._device = device
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

        logger.debug("Loading wakeword models: %s", self._model_names)
        try:
            model = Model(wakeword_models=self._model_names, inference_framework="onnx")
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
                device=self._device,
                callback=_mic_callback,
            ):
                logger.info("Wakeword mic stream open — listening for %s",
                            ", ".join(_display(m) for m in self._model_names))
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
                    for fired, score in prediction.items():
                        val = float(score) if not hasattr(score, "__iter__") else float(max(score))
                        if val >= self._sensitivity and (now - last_trigger) >= self._cooldown:
                            logger.info(
                                "Wakeword triggered! score=%.3f model=%s", val, fired,
                            )
                            last_trigger = now
                            self._event.set()
                            for _cb in _wake_callbacks:
                                threading.Thread(target=_cb, daemon=True).start()
                            break

        except Exception as exc:
            logger.error("Wakeword detector crashed: %s", exc)

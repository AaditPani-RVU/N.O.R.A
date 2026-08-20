"""Background perception loop — who is in front of the camera.

Mirrors `nora/ambient.py`: a daemon thread that samples a sensor, filters
low-signal frames, and publishes to shared state. Started from
`pipeline.run()`, gated by `vision.enabled` in config.yaml.

Three decisions worth knowing about before editing this file:

1. **Detection is per-frame; embedding is not.** Running the 512-d ArcFace
   embedding on every face on every frame pins a GPU permanently in order
   to say "Hey Aadit" once. A face is embedded when it first appears and
   not again — identity is then carried by bounding-box continuity, and
   re-embedded only on track loss.

2. **Greeting fires on presence transition, not once per process.** A
   greeted-set that never resets is wrong for a long-lived daemon: greeted
   at 09:00, step away, return at 18:00, silence until restart. A person
   becomes greetable again once they've been unseen for
   `regreet_after_seconds`. Set it to 0 for strict once-per-process.

3. **Unknown faces leave no trace.** They're matched and discarded — no
   embedding written, no thumbnail, no knowledge-base entry. Frames never
   touch disk at all.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from nora.vision import camera, faces

logger = logging.getLogger("nora.vision.perception")

_lock = threading.RLock()
_running = False
_thread: threading.Thread | None = None

# Frames a face must be seen / missed before presence is declared or withdrawn.
# Stops a blink or a turned head from retriggering a greeting.
_CONFIRM_FRAMES = 3
_ABSENT_FRAMES = 5

# Minimum bbox overlap to treat two detections as the same person across frames.
_IOU_MATCH = 0.3

_UNKNOWN = ""      # track name for a face that matched nobody


@dataclass
class _Track:
    """One face followed across frames. Identity is resolved once, then carried."""
    bbox: tuple[float, float, float, float]
    name: str | None = None      # None = not yet embedded; "" = known-unknown
    score: float = 0.0
    hits: int = 1
    misses: int = 0
    confirmed: bool = False


_tracks: list[_Track] = []
_greeted_at: dict[str, float] = {}
_speak_hook: Any = None          # injected by pipeline; falls back to speaker.speak


def _cfg() -> dict:
    from nora.config import get_config
    return get_config().get("vision", {}) or {}


def _face_cfg() -> dict:
    return _cfg().get("face", {}) or {}


def _iou(a: tuple, b: tuple) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _speak(text: str) -> None:
    try:
        if _speak_hook is not None:
            _speak_hook(text)
            return
        from nora import speaker
        speaker.speak(text)
    except Exception as e:
        logger.debug("Greeting speech failed: %s", e)


def set_speak_hook(fn) -> None:
    """Let the pipeline route greetings through its focus-gated speaker."""
    global _speak_hook
    _speak_hook = fn


# ── Tracking ─────────────────────────────────────────────────────────────────

def _match_tracks(detections: list[dict[str, Any]]) -> list[tuple[_Track, dict | None]]:
    """Greedily pair existing tracks with this frame's detections by IoU."""
    unmatched = list(detections)
    pairs: list[tuple[_Track, dict | None]] = []

    for track in _tracks:
        best, best_iou = None, _IOU_MATCH
        for det in unmatched:
            overlap = _iou(track.bbox, det["bbox"])
            if overlap >= best_iou:
                best, best_iou = det, overlap
        if best is not None:
            unmatched.remove(best)
        pairs.append((track, best))

    for det in unmatched:
        pairs.append((_Track(bbox=det["bbox"]), det))
    return pairs


def _resolve_identity(frame, track: _Track, det: dict[str, Any]) -> None:
    """Embed and identify a face — only for tracks that aren't resolved yet."""
    embedding = faces.embed(frame, det)
    if embedding is None:
        return
    name, score = faces.identify(embedding)
    track.name = name if name else _UNKNOWN
    track.score = score
    # Nothing about an unrecognized person is written anywhere: the embedding
    # goes out of scope here and that is the whole of its lifetime.


def _process_frame(frame) -> tuple[list[str], int]:
    """Advance tracking by one frame. Returns (confirmed names, unknown count)."""
    global _tracks

    detections = faces.detect(frame)
    survivors: list[_Track] = []

    for track, det in _match_tracks(detections):
        if det is None:
            track.misses += 1
            if track.misses <= _ABSENT_FRAMES:
                survivors.append(track)
            continue

        # Identity carries by bbox continuity; a track that lost and regained
        # its detection may be a different person, so re-embed on recovery.
        if track.misses > 0:
            track.name = None
        track.bbox = det["bbox"]
        track.misses = 0
        track.hits += 1

        if track.name is None:
            _resolve_identity(frame, track, det)

        if track.hits >= _CONFIRM_FRAMES:
            track.confirmed = True
        survivors.append(track)

    _tracks = survivors

    present = sorted({t.name for t in _tracks if t.confirmed and t.name})
    unknown = sum(1 for t in _tracks if t.confirmed and t.name == _UNKNOWN)
    return present, unknown


# ── Greeting ─────────────────────────────────────────────────────────────────

def _maybe_greet(present: list[str], last_seen: dict[str, float]) -> None:
    """Greet on arrival — a person absent long enough becomes greetable again.

    `last_seen` must be the snapshot taken *before* this frame was published,
    or the gap is always zero and nobody is ever greeted twice.
    """
    fcfg = _face_cfg()
    if not fcfg.get("greet_on_first_sight", True):
        return

    regreet = float(fcfg.get("regreet_after_seconds", 1800))
    now = time.time()

    for name in present:
        greeted = _greeted_at.get(name)
        if greeted is not None:
            if regreet <= 0:
                continue                      # strict once-per-process
            away = now - last_seen.get(name, greeted)
            if away < regreet:
                continue                      # still the same visit
        _greeted_at[name] = now
        greeting = faces.greeting_for(name)      # per-person text, Phase 2
        threading.Thread(
            target=_speak, args=(greeting,), daemon=True, name="nora-vision-greet"
        ).start()
        logger.info("Greeted %s", name)


# ── Loop ─────────────────────────────────────────────────────────────────────

def _perception_loop(fps: float) -> None:
    global _running, _tracks

    logger.info("Vision perception loop started (%.1f fps)", fps)
    interval = 1.0 / max(fps, 0.5)
    holding = False

    try:
        while True:
            with _lock:
                if not _running:
                    break

            # An explicit close_camera() suspends the device; don't fight it.
            if camera.is_suspended():
                if holding:
                    camera.release("perception")
                    holding = False
                _publish([], 0)
                time.sleep(1.0)
                continue

            if not holding:
                if not camera.acquire("perception"):
                    _publish([], 0)
                    time.sleep(2.0)
                    continue
                holding = True
                _tracks = []

            frame = camera.latest_frame()
            if frame is None:
                _publish([], 0)
                time.sleep(interval)
                continue

            try:
                present, unknown = _process_frame(frame)
            except Exception as e:
                logger.debug("Perception frame failed: %s", e)
                time.sleep(interval)
                continue

            from nora import context
            prior_seen = context.get_vision()["last_seen"]
            _publish(present, unknown)
            if present:
                _maybe_greet(present, prior_seen)

            time.sleep(interval)
    finally:
        if holding:
            camera.release("perception")
        _publish([], 0)
        with _lock:
            _running = False
        logger.info("Vision perception loop stopped")


def _publish(present: list[str], unknown: int) -> None:
    from nora import context
    context.update_vision(
        present=present,
        unknown_count=unknown,
        camera_active=camera.is_active(),
    )


def start() -> None:
    """Start the perception thread if vision is enabled in config."""
    global _running, _thread

    with _lock:
        if _running:
            return

        cfg = _cfg()
        if not cfg.get("enabled", False):
            logger.debug("Vision disabled (set vision.enabled: true to enable)")
            return
        if not (cfg.get("face", {}) or {}).get("enabled", True):
            logger.debug("Face recognition disabled — perception loop not started")
            return

        _running = True
        _thread = threading.Thread(
            target=_perception_loop,
            args=(float(cfg.get("fps", 5)),),
            daemon=True,
            name="nora-vision",
        )
        _thread.start()

    # Pull the buffalo_l pack in the background so the first greeting isn't
    # stalled behind a ~300 MB model download.
    faces.warm_up()
    logger.info("Vision perception started")


def stop() -> None:
    global _running
    with _lock:
        _running = False


def is_running() -> bool:
    with _lock:
        return _running


def suspend_for_explicit_off() -> None:
    """Called by close_camera(): stop perceiving until an explicit reopen."""
    stop()


def present_names() -> list[str]:
    from nora import context
    return context.get_vision()["present"]

"""Single owner of the capture device.

Only one process can hold /dev/video0, and every consumer of frames
(the perception loop, `what_do_you_see`, Phase 4's `ask_about_view`)
needs the same stream. This module owns the one `cv2.VideoCapture`
and hands out the most recent frame.

Access is reference-counted so a voice-issued `open_camera()` and the
background perception loop can coexist. Reference counting alone is a
privacy footgun, though: a user who says "close the camera" must get
the device released, not a success message while the perception loop
keeps the LED on. So `force_off()` overrides the refcount entirely and
latches a suspend flag — acquire() fails until an explicit reopen.
Off means off.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger("nora.vision.camera")

_lock = threading.RLock()

_cap: Any = None                    # cv2.VideoCapture while the device is held
_thread: threading.Thread | None = None
_running = False
_suspended = False                  # latched by force_off(); blocks acquire()
_refs: dict[str, int] = {}          # holder name -> count
_latest_frame: Any = None           # most recent BGR ndarray
_latest_ts: float = 0.0

_WIDTH, _HEIGHT = 640, 480


def _cfg() -> dict:
    from nora.config import get_config
    return get_config().get("vision", {}) or {}


# ── Capture thread ───────────────────────────────────────────────────────────

def _capture_loop(index: int, fps: float) -> None:
    """Owns the device for its whole lifetime. Releases on the way out."""
    global _cap, _running, _latest_frame, _latest_ts

    import cv2

    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        cap.release()
        logger.debug("Camera %s unavailable (absent or busy) — capture not started", index)
        with _lock:
            _running = False
            _cap = None
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, _WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, _HEIGHT)

    with _lock:
        _cap = cap
    logger.info("Camera %s opened (%dx%d @ %.1f fps)", index, _WIDTH, _HEIGHT, fps)

    interval = 1.0 / max(fps, 0.5)
    failures = 0

    try:
        while True:
            with _lock:
                if not _running:
                    break

            ok, frame = cap.read()
            if not ok or frame is None:
                failures += 1
                if failures >= 30:
                    logger.warning("Camera %s stopped delivering frames — releasing", index)
                    break
                time.sleep(0.1)
                continue
            failures = 0

            if frame.shape[1] != _WIDTH:
                frame = cv2.resize(frame, (_WIDTH, _HEIGHT))

            with _lock:
                _latest_frame = frame
                _latest_ts = time.time()

            time.sleep(interval)
    finally:
        cap.release()
        with _lock:
            _cap = None
            _running = False
            _latest_frame = None
            _latest_ts = 0.0
        logger.info("Camera %s released", index)


def _spawn_device() -> bool:
    """Spawn the capture thread if it isn't running. Caller must hold _lock.

    Returns False only when the camera can't be attempted at all. Whether the
    device actually opened is answered later by _await_open(), which waits
    without holding the lock.
    """
    global _thread, _running

    if _running:
        return True

    try:
        import cv2  # noqa: F401
    except Exception as e:
        logger.debug("OpenCV not installed — camera unavailable: %s", e)
        return False

    cfg = _cfg()
    index = int(cfg.get("camera_index", 0))
    fps = float(cfg.get("fps", 5))

    _running = True
    _thread = threading.Thread(
        target=_capture_loop,
        args=(index, fps),
        daemon=True,
        name="nora-camera",
    )
    _thread.start()
    return True


def _await_open(timeout: float = 5.0) -> bool:
    """Block until the first frame arrives, or the capture thread gives up.

    Waiting for a frame rather than for `VideoCapture.isOpened()` keeps
    acquire() honest: a caller that gets True can immediately call
    latest_frame() and get a picture. A device that opens but never
    delivers is useless, so it counts as a failure.

    Must be called without holding _lock.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _lock:
            if _latest_frame is not None:
                return True
            if not _running:      # loop bailed out — device absent or busy
                return False
        time.sleep(0.05)
    logger.warning("Camera opened but delivered no frame within %.0fs", timeout)
    return False


def _stop_device() -> None:
    """Signal the capture thread to release the device. Caller must hold _lock."""
    global _running
    _running = False


# ── Reference-counted access ─────────────────────────────────────────────────

def acquire(holder: str = "anonymous") -> bool:
    """Take a reference on the camera, opening it if needed.

    Returns False when the camera is suspended by an explicit close, or
    when no usable device is present. Fails soft either way.
    """
    with _lock:
        if _suspended:
            logger.debug("acquire(%s) refused — camera suspended by explicit close", holder)
            return False
        if not _spawn_device():
            return False
        _refs[holder] = _refs.get(holder, 0) + 1

    if _await_open():
        return True

    # Device never came up — don't leave a phantom reference behind.
    release(holder)
    return False


def release(holder: str = "anonymous") -> None:
    """Drop a reference. The device closes when the last one goes."""
    with _lock:
        if holder in _refs:
            _refs[holder] -= 1
            if _refs[holder] <= 0:
                del _refs[holder]
        if not _refs:
            _stop_device()


def force_off() -> None:
    """Explicit off: release the device regardless of who holds a reference.

    Latches a suspend flag so the perception loop cannot silently reopen
    the device behind the user's back. Cleared only by resume().
    """
    global _suspended
    with _lock:
        _suspended = True
        _refs.clear()
        _stop_device()

    # Wait for the capture thread to actually let go of the device, so a
    # caller can truthfully report "camera off" rather than "camera closing".
    deadline = time.time() + 3.0
    while time.time() < deadline:
        with _lock:
            if _cap is None:
                return
        time.sleep(0.05)
    logger.warning("Camera did not release within 3s of force_off()")


def resume() -> None:
    """Clear the explicit-off latch. Does not itself open the device."""
    global _suspended
    with _lock:
        _suspended = False


def is_suspended() -> bool:
    with _lock:
        return _suspended


def is_active() -> bool:
    """True whenever the device is held. Never reports off while the LED is on."""
    with _lock:
        return _cap is not None


def holders() -> list[str]:
    with _lock:
        return sorted(_refs)


def latest_frame(max_age: float = 5.0):
    """Most recent BGR frame, or None if the camera is off or the frame is stale."""
    with _lock:
        if _latest_frame is None:
            return None
        if max_age and (time.time() - _latest_ts) > max_age:
            return None
        return _latest_frame.copy()


def grab(holder: str = "grab", timeout: float = 5.0):
    """One-shot capture for callers that don't hold a reference.

    Opens the camera if needed, waits for a fresh frame, then drops its
    reference. Returns None when the camera is unavailable or suspended.
    """
    frame = latest_frame(max_age=2.0)
    if frame is not None:
        return frame

    if not acquire(holder):
        return None
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            frame = latest_frame(max_age=timeout)
            if frame is not None:
                return frame
            time.sleep(0.05)
        return None
    finally:
        release(holder)


def stop() -> None:
    """Shutdown hook — release the device without latching the suspend flag."""
    with _lock:
        _refs.clear()
        _stop_device()

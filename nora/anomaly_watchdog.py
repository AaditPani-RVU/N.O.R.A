"""Anomaly Watchdog — Sprint 4 Deliverable #24 (P1).

Background thread that polls system metrics via psutil (+ nvidia-smi for GPU)
and speaks an alert when a metric crosses a user-configurable threshold.

Thresholds (from config.yaml → anomaly_watchdog):
  cpu_percent        : 90
  ram_percent        : 90
  gpu_percent        : 94          (requires pynvml or nvidia-smi)
  gpu_vram_percent   : 90
  disk_write_mbps    : 500

Alert cooldown: 5 minutes per metric to avoid spam.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

logger = logging.getLogger("nora.anomaly_watchdog")

_stop_event = threading.Event()
_thread: threading.Thread | None = None
_speak_cb: Callable[[str], None] | None = None
_drill_down_cb: Callable[[str, float], None] | None = None

# Per-metric cooldown tracking: metric_name → last_alert_ts
_last_alert: dict[str, float] = {}
_COOLDOWN_SEC = 300  # 5 minutes


def _get_thresholds() -> dict[str, float]:
    from nora.config import get_config
    cfg = get_config().get("anomaly_watchdog", {})
    return {
        "cpu_percent": float(cfg.get("cpu_percent", 90)),
        "ram_percent": float(cfg.get("ram_percent", 90)),
        "gpu_percent": float(cfg.get("gpu_percent", 94)),
        "gpu_vram_percent": float(cfg.get("gpu_vram_percent", 90)),
        "disk_write_mbps": float(cfg.get("disk_write_mbps", 500)),
    }


def _should_alert(metric: str) -> bool:
    now = time.time()
    last = _last_alert.get(metric, 0)
    if now - last >= _COOLDOWN_SEC:
        _last_alert[metric] = now
        return True
    return False


def _get_gpu_stats() -> dict[str, float]:
    """Return GPU util% and VRAM% via pynvml (preferred) or nvidia-smi fallback."""
    try:
        import pynvml
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        vram_pct = (mem.used / mem.total) * 100 if mem.total else 0
        pynvml.nvmlShutdown()
        return {"gpu_percent": float(util.gpu), "gpu_vram_percent": vram_pct}
    except Exception:
        pass
    # nvidia-smi fallback
    try:
        import subprocess
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            timeout=3,
        ).decode().strip().split(",")
        if len(out) == 3:
            gpu_pct = float(out[0].strip())
            vram_used = float(out[1].strip())
            vram_total = float(out[2].strip())
            vram_pct = (vram_used / vram_total * 100) if vram_total else 0
            return {"gpu_percent": gpu_pct, "gpu_vram_percent": vram_pct}
    except Exception:
        pass
    return {}


def _watchdog_loop(poll_sec: float) -> None:
    import psutil

    logger.info("Anomaly watchdog started (poll every %.0fs)", poll_sec)
    disk_io_prev = psutil.disk_io_counters()
    prev_time = time.monotonic()

    while not _stop_event.wait(poll_sec):
        try:
            thresholds = _get_thresholds()
            speak = _speak_cb

            # CPU
            cpu = psutil.cpu_percent(interval=None)
            if cpu >= thresholds["cpu_percent"] and _should_alert("cpu_percent"):
                msg = f"CPU usage is at {cpu:.0f}%. You may want to check what's running."
                logger.warning(msg)
                if speak:
                    speak(msg)
                if _drill_down_cb:
                    try:
                        _drill_down_cb("cpu_percent", cpu)
                    except Exception:
                        pass

            # RAM
            ram = psutil.virtual_memory().percent
            if ram >= thresholds["ram_percent"] and _should_alert("ram_percent"):
                msg = f"RAM usage is at {ram:.0f}%. Your system may be under memory pressure."
                logger.warning(msg)
                if speak:
                    speak(msg)
                if _drill_down_cb:
                    try:
                        _drill_down_cb("ram_percent", ram)
                    except Exception:
                        pass

            # GPU
            gpu_stats = _get_gpu_stats()
            gpu_pct = gpu_stats.get("gpu_percent", 0)
            vram_pct = gpu_stats.get("gpu_vram_percent", 0)

            if gpu_pct >= thresholds["gpu_percent"] and _should_alert("gpu_percent"):
                msg = f"GPU utilization is at {gpu_pct:.0f}%. Your training job may be at capacity."
                logger.warning(msg)
                if speak:
                    speak(msg)

            if vram_pct >= thresholds["gpu_vram_percent"] and _should_alert("gpu_vram_percent"):
                msg = (
                    f"GPU memory is at {vram_pct:.0f}%. "
                    "Your training job may be about to run out of VRAM."
                )
                logger.warning(msg)
                if speak:
                    speak(msg)

            # Disk write throughput
            now_time = time.monotonic()
            disk_io_now = psutil.disk_io_counters()
            if disk_io_prev and disk_io_now:
                elapsed = now_time - prev_time or 1.0
                write_mbps = (
                    (disk_io_now.write_bytes - disk_io_prev.write_bytes)
                    / elapsed / 1_048_576
                )
                if write_mbps >= thresholds["disk_write_mbps"] and _should_alert("disk_write_mbps"):
                    msg = (
                        f"Disk write speed is {write_mbps:.0f} MB/s. "
                        "Something is writing heavily to disk."
                    )
                    logger.warning(msg)
                    if speak:
                        speak(msg)
            disk_io_prev = disk_io_now
            prev_time = now_time

        except Exception as e:
            logger.debug("Watchdog poll error: %s", e)

    logger.info("Anomaly watchdog stopped.")


def register_drill_down(cb: Callable[[str, float], None]) -> None:
    """Register a callback invoked when a metric threshold fires.

    cb(metric: str, value: float) — fires after the spoken alert, before cooldown resets.
    No-op when no subscriber is registered; existing watchdog behavior is unchanged.
    """
    global _drill_down_cb
    _drill_down_cb = cb


def start(speak_callback: Callable[[str], None] | None = None, poll_sec: float = 30.0) -> None:
    """Start the background watchdog thread. No-op if disabled in config."""
    global _thread, _speak_cb

    from nora.config import get_config
    cfg = get_config().get("anomaly_watchdog", {})
    if not cfg.get("enabled", True):
        logger.info("Anomaly watchdog disabled in config.")
        return

    _speak_cb = speak_callback
    poll_sec = float(cfg.get("poll_sec", poll_sec))
    _stop_event.clear()
    _thread = threading.Thread(
        target=_watchdog_loop,
        args=(poll_sec,),
        daemon=True,
        name="nora-anomaly-watchdog",
    )
    _thread.start()


def stop() -> None:
    _stop_event.set()
    if _thread:
        _thread.join(timeout=5)

"""Proactive system resource monitoring with voice alerts.

Runs a background daemon thread that watches CPU, RAM, disk, and battery.
Fires spoken alerts when thresholds are crossed, with per-metric cooldowns
to avoid spam. Controlled via enable_alerts / disable_alerts / set_alert commands.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import psutil

logger = logging.getLogger("nora.monitor")

_DEFAULT_THRESHOLDS: dict[str, int] = {
    "cpu": 90,
    "ram": 90,
    "disk": 95,
    "battery": 15,
}

_COOLDOWN_SEC = 300


class SystemMonitor:
    def __init__(self) -> None:
        self._thresholds: dict[str, int] = dict(_DEFAULT_THRESHOLDS)
        self._last_alerts: dict[str, float] = {}
        self._enabled = True
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def set_threshold(self, metric: str, value: int) -> str:
        metric = metric.lower()
        if metric not in _DEFAULT_THRESHOLDS:
            available = ", ".join(_DEFAULT_THRESHOLDS)
            return f"Unknown metric '{metric}'. Available: {available}."
        self._thresholds[metric] = value
        return f"Alert threshold for {metric} set to {value} percent."

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    def _can_alert(self, metric: str) -> bool:
        return time.time() - self._last_alerts.get(metric, 0) > _COOLDOWN_SEC

    def _fire(self, metric: str, message: str) -> None:
        from nora import speaker
        self._last_alerts[metric] = time.time()
        logger.warning("Monitor alert [%s]: %s", metric, message)
        speaker.speak(message)

    def _check(self) -> None:
        if not self._enabled:
            return

        try:
            cpu = psutil.cpu_percent(interval=1)
            if cpu >= self._thresholds["cpu"] and self._can_alert("cpu"):
                self._fire("cpu", f"Heads up sir. CPU usage is at {cpu:.0f} percent. You may want to close some applications.")
        except Exception:
            pass

        try:
            ram = psutil.virtual_memory().percent
            if ram >= self._thresholds["ram"] and self._can_alert("ram"):
                self._fire("ram", f"Warning. RAM usage is at {ram:.0f} percent. Memory is critically low.")
        except Exception:
            pass

        try:
            disk = psutil.disk_usage("/").percent
            if disk >= self._thresholds["disk"] and self._can_alert("disk"):
                self._fire("disk", f"Disk usage is at {disk:.0f} percent. Storage is almost full.")
        except Exception:
            pass

        try:
            battery = psutil.sensors_battery()
            if battery and not battery.power_plugged:
                if battery.percent <= self._thresholds["battery"] and self._can_alert("battery"):
                    self._fire("battery", f"Battery is at {battery.percent:.0f} percent. Please plug in your charger, sir.")
        except Exception:
            pass

    def _loop(self, interval: int) -> None:
        logger.info("System monitor started (interval=%ds)", interval)
        while self._running:
            self._check()
            time.sleep(interval)
        logger.info("System monitor stopped")

    def start(self, interval: int = 30) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop,
            args=(interval,),
            daemon=True,
            name="nora-monitor",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False


_monitor = SystemMonitor()


def get_monitor() -> SystemMonitor:
    return _monitor


def start(interval: int = 30) -> None:
    _monitor.start(interval)


def stop() -> None:
    _monitor.stop()

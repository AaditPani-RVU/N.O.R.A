from __future__ import annotations

import datetime
import logging
import platform

import psutil

from nora.command_engine import register

logger = logging.getLogger("nora.commands.system_info")


@register("get_system_info")
def get_system_info() -> str:
    """Get current system information."""
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    battery = psutil.sensors_battery()

    lines = [
        f"CPU usage: {cpu}%",
        f"RAM: {mem.percent}% used ({mem.used // (1024**3)}GB / {mem.total // (1024**3)}GB)",
        f"Disk: {disk.percent}% used ({disk.used // (1024**3)}GB / {disk.total // (1024**3)}GB)",
    ]

    if battery:
        status = "charging" if battery.power_plugged else "on battery"
        lines.append(f"Battery: {battery.percent}% ({status})")

    lines.append(f"OS: {platform.system()} {platform.release()}")

    return ". ".join(lines)


@register("get_time")
def get_time() -> str:
    """Get current date and time."""
    now = datetime.datetime.now()
    return f"It is {now.strftime('%I:%M %p')} on {now.strftime('%A, %B %d, %Y')}."

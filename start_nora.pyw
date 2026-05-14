"""NORA auto-start launcher — run with pythonw.exe for no console window.

Registered by install_autostart.ps1 to launch at Windows logon.
Includes a crash-restart watchdog: if NORA exits with a non-zero code
(crash) it waits 5 seconds and relaunches. A clean exit (user said
goodbye / exit code 0) ends the loop.
"""
import subprocess
import sys
import time
from pathlib import Path

NORA_DIR = Path(__file__).resolve().parent
MAIN = NORA_DIR / "main.py"
PYTHON = sys.executable

MAX_CRASHES = 10
crash_count = 0

while crash_count < MAX_CRASHES:
    result = subprocess.run(
        [PYTHON, str(MAIN)],
        cwd=str(NORA_DIR),
    )
    if result.returncode == 0:
        # Clean exit — user said goodbye
        break
    crash_count += 1
    # Wait longer each successive crash to avoid a tight restart loop
    delay = min(5 * crash_count, 60)
    time.sleep(delay)

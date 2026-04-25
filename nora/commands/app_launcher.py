from __future__ import annotations

import logging
import os
import subprocess

from nora.command_engine import register

logger = logging.getLogger("nora.commands.app_launcher")

# Map of friendly names to executables or shell commands
APP_MAP: dict[str, str] = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "chrome": "chrome",
    "brave": "brave",
    "firefox": "firefox",
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "terminal": "wt.exe",
    "cmd": "cmd.exe",
    "powershell": "powershell.exe",
    "vscode": "code",
    "vs code": "code",
    "task manager": "taskmgr.exe",
    "paint": "mspaint.exe",
    "word": "winword",
    "excel": "excel",
    "spotify": "spotify",
}


@register("open_app")
def open_app(name: str) -> str:
    """Open an application by friendly name."""
    name_lower = name.lower().strip()
    executable = APP_MAP.get(name_lower, name_lower)

    try:
        # Try os.startfile first (works for registered file types and apps)
        os.startfile(executable)
        return f"Opened {name}."
    except OSError:
        pass

    try:
        # Fallback: try running as a command
        subprocess.Popen(executable, shell=True)
        return f"Opened {name}."
    except Exception as e:
        logger.error(f"Failed to open {name}: {e}")
        return f"Could not open {name}: {e}"


@register("close_app")
def close_app(name: str) -> str:
    """Close an application by name using taskkill."""
    name_lower = name.lower().strip()

    # Map friendly names to process names
    process_map: dict[str, str] = {
        "notepad": "notepad.exe",
        "chrome": "chrome.exe",
        "brave": "brave.exe",
        "firefox": "firefox.exe",
        "vscode": "Code.exe",
        "vs code": "Code.exe",
        "explorer": "explorer.exe",
        "spotify": "Spotify.exe",
    }

    process = process_map.get(name_lower, f"{name_lower}.exe")

    try:
        result = subprocess.run(
            ["taskkill", "/IM", process, "/F"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return f"Closed {name}."
        else:
            return f"Could not close {name}: {result.stderr.strip()}"
    except Exception as e:
        return f"Error closing {name}: {e}"

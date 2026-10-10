"""Keyboard text input fallback -- lets you type commands when mic is unavailable."""
from __future__ import annotations

import queue
import sys
import threading
import logging

logger = logging.getLogger("nora.text_input")

# Items are text, or (text, source) for text typed somewhere with a name
# ("dashboard"), so the transcript can say where it came from (Sharp F).
_queue: queue.Queue = queue.Queue()
_started = False


def _stdin_reader() -> None:
    try:
        print("[NORA] Text input active -- type a command and press Enter:", flush=True)
        for line in sys.stdin:
            text = line.strip()
            if text:
                _queue.put((text, "keyboard"))
    except Exception:
        pass


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    t = threading.Thread(target=_stdin_reader, daemon=True, name="nora-text-input")
    t.start()


def submit(text: str, source: str = "dashboard") -> None:
    """Queue typed text for the next turn."""
    _queue.put((text, source))


def pop_pending() -> tuple[str, str] | None:
    """The next typed text and where it was typed, if any."""
    try:
        item = _queue.get_nowait()
    except queue.Empty:
        return None
    return item if isinstance(item, tuple) else (item, "dashboard")


def get_pending() -> str | None:
    item = pop_pending()
    return item[0] if item else None

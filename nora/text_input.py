"""Keyboard text input fallback â€” lets you type commands when mic is unavailable."""
from __future__ import annotations

import queue
import sys
import threading
import logging

logger = logging.getLogger("nora.text_input")

_queue: queue.Queue[str] = queue.Queue()
_started = False


def _stdin_reader() -> None:
    try:
        print("[NORA] Text input active â€” type a command and press Enter:", flush=True)
        for line in sys.stdin:
            text = line.strip()
            if text:
                _queue.put(text)
    except Exception:
        pass


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    t = threading.Thread(target=_stdin_reader, daemon=True, name="nora-text-input")
    t.start()


def get_pending() -> str | None:
    try:
        return _queue.get_nowait()
    except queue.Empty:
        return None

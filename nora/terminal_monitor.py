"""Terminal Co-Pilot — monitors the active terminal for errors and speaks triage hypotheses.

Two modes:
  1. Passive clipboard watcher — polls every 2s; when clipboard looks like a stack
     trace, sets a pending alert that the user can query with "what's the error".
  2. Active check — voice command check_terminal() takes a screenshot and uses
     Claude Vision to extract any error text, then calls triage_error().

The triage call uses the Claude API directly (haiku) for sub-3s latency.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

logger = logging.getLogger("nora.terminal_monitor")

_ERROR_PATTERNS = re.compile(
    r"(Traceback \(most recent call last\)|Error:|Exception:|FAILED|"
    r"SyntaxError|RuntimeError|NameError|TypeError|ValueError|"
    r"ImportError|ModuleNotFoundError|AttributeError|KeyError|"
    r"IndexError|AssertionError|PermissionError|FileNotFoundError)",
    re.IGNORECASE,
)

_pending_error: str | None = None
_last_clipboard: str = ""
_watcher_thread: threading.Thread | None = None
_running = False


def _read_clipboard() -> str:
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        try:
            return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT) or ""
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return ""


def _looks_like_error(text: str) -> bool:
    return bool(_ERROR_PATTERNS.search(text[:2000]))


def _watcher_loop(speak_callback: Any) -> None:
    global _pending_error, _last_clipboard, _running
    while _running:
        try:
            clip = _read_clipboard().strip()
            if clip and clip != _last_clipboard and _looks_like_error(clip):
                _pending_error = clip[:3000]
                _last_clipboard = clip
                logger.info("Terminal monitor: error detected in clipboard (%d chars)", len(clip))
                if speak_callback:
                    speak_callback(
                        "I see an error in your clipboard. Say 'explain error' for a triage.",
                        mood="proactive",
                    )
        except Exception as e:
            logger.debug("Terminal monitor watcher tick error: %s", e)
        time.sleep(2)


def start(speak_callback: Any = None) -> None:
    global _running, _watcher_thread
    if _running:
        return
    _running = True
    _watcher_thread = threading.Thread(
        target=_watcher_loop, args=(speak_callback,), daemon=True, name="nora-terminal-monitor"
    )
    _watcher_thread.start()
    logger.info("Terminal monitor started")


def stop() -> None:
    global _running
    _running = False


def get_pending_error() -> str | None:
    return _pending_error


def clear_pending_error() -> None:
    global _pending_error
    _pending_error = None


def triage_error(error_text: str, code_context: str = "") -> str:
    """Send error text to Claude Haiku for triage. Returns a spoken hypothesis + fix suggestion."""
    if not error_text.strip():
        return "No error text to triage."

    prompt = (
        "You are a developer assistant. Analyse this error and respond in 2-3 spoken sentences: "
        "what likely caused it, and the most direct fix. No markdown, no bullet points.\n\n"
        f"Error:\n{error_text[:1500]}"
    )
    if code_context:
        prompt += f"\n\nCode context:\n{code_context[:500]}"

    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        logger.warning("triage_error: Claude call failed: %s", e)
        # Fallback: pattern-based triage
        if "ModuleNotFoundError" in error_text or "ImportError" in error_text:
            match = re.search(r"No module named '([^']+)'", error_text)
            pkg = match.group(1) if match else "the module"
            return f"Missing package: {pkg}. Try: pip install {pkg}."
        if "CUDA" in error_text and "out of memory" in error_text.lower():
            return "CUDA out of memory. Reduce batch size or call torch.cuda.empty_cache()."
        if "FileNotFoundError" in error_text:
            match = re.search(r"'\s*([^']+\.[\w]+)\s*'", error_text)
            path = match.group(1) if match else "the file"
            return f"File not found: {path}. Check the path exists."
        return f"Error detected: {error_text[:100]}. Check the stack trace for the root cause."

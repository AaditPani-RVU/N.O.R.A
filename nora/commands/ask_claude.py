"""Ask Claude command â€” enriches questions with clipboard + active window context."""
from __future__ import annotations

import logging
import subprocess

import win32clipboard
import win32gui

from nora.command_engine import register

logger = logging.getLogger("nora.commands.ask_claude")


def _active_window_title() -> str:
    try:
        hwnd = win32gui.GetForegroundWindow()
        return win32gui.GetWindowText(hwnd) or ""
    except Exception:
        return ""


def _clipboard_text() -> str:
    try:
        win32clipboard.OpenClipboard()
        try:
            return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT) or ""
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return ""


def _build_context_block() -> str:
    """Build a context preamble from the user's current environment."""
    parts: list[str] = []

    window = _active_window_title()
    if window:
        parts.append(f"Active window: {window}")

    clip = _clipboard_text().strip()
    if clip and len(clip) <= 2000:
        parts.append(f"Clipboard:\n{clip}")

    if not parts:
        return ""
    return "[Current context]\n" + "\n".join(parts) + "\n\n"


@register("ask_claude")
def ask_claude(question: str) -> str:
    """Send a question to Claude Code CLI, enriched with clipboard and active window context."""
    context_block = _build_context_block()

    if context_block:
        logger.info("Injecting context: window=%s, clipboard_len=%d",
                    _active_window_title(), len(_clipboard_text()))

    prompt = (
        f"{context_block}"
        f"Answer this question concisely in 2-4 spoken sentences. "
        f"No bullet points, no markdown, no formatting â€” just plain conversational text "
        f"as if you're speaking to someone. Question: {question}"
    )

    logger.info("Asking Claude: %s", question)

    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=30,
            shell=True,
        )

        if result.returncode != 0:
            logger.error("Claude CLI error: %s", result.stderr)
            return f"I had trouble reaching Claude. {result.stderr.strip()}"

        response = result.stdout.strip()
        if response:
            logger.info("Claude responded: %s...", response[:100])
            return response
        return "Claude didn't return a response."

    except subprocess.TimeoutExpired:
        return "Claude took too long to respond. Try asking something simpler."
    except FileNotFoundError:
        return "Claude CLI is not installed or not in PATH."
    except Exception as e:
        logger.error("ask_claude error: %s", e)
        return f"Something went wrong asking Claude: {e}"

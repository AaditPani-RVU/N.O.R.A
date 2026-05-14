"""Screen Intelligence — Phase 3.

Registered actions:
  read_screen         — describe what's on screen (or answer a question about it)
  find_on_screen      — locate specific text or UI element by description
  click_on            — find an element and click it
  extract_text        — dump all visible text from the screen (OCR-style)
  explain_code_on_screen — find code on screen and explain it verbally
  copy_from_screen    — extract specific text and put it on the clipboard
  watch_for           — monitor screen in background; speak when condition appears
  stop_watching       — cancel an active watch_for monitor
  debug_screen        — scan for errors, warnings, and stack traces

All vision calls use Claude Haiku (claude-haiku-4-5-20251001) for speed and cost.
"""
from __future__ import annotations

import base64
import json
import logging
import subprocess
import threading
import time
from io import BytesIO
from typing import Any

import pyautogui

from nora.command_engine import register
import nora.speaker as speaker

logger = logging.getLogger("nora.commands.screen_intelligence")

# ── Shared Anthropic client (lazy-init) ────────────────────────────────────

_client: Any = None


def _get_client() -> Any:
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


# ── Screenshot helpers ─────────────────────────────────────────────────────

def _screenshot_b64() -> tuple[str, int, int]:
    """Return (base64_png, screen_width, screen_height)."""
    img = pyautogui.screenshot()
    w, h = img.size
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode(), w, h


# ── Vision call helpers ────────────────────────────────────────────────────

_SYSTEM = (
    "You are NORA's screen analysis engine. "
    "You receive screenshots and answer questions about them precisely and concisely. "
    "Never use markdown or bullet points in spoken responses. "
    "When asked for JSON, return ONLY valid JSON with no surrounding text."
)


def _vision(image_b64: str, prompt: str, max_tokens: int = 400) -> str:
    """Send a screenshot to Claude Vision, return spoken text."""
    client = _get_client()
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=_SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": image_b64},
                },
                {"type": "text", "text": prompt},
            ],
        }],
    )
    return msg.content[0].text.strip()


def _vision_json(image_b64: str, prompt: str) -> dict:
    """Send a screenshot, parse the response as JSON. Returns {} on failure."""
    raw = _vision(image_b64, prompt, max_tokens=200)
    try:
        # Strip any accidental markdown fences
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(cleaned)
    except Exception:
        logger.warning("Vision JSON parse failed: %s", raw[:200])
        return {}


# ── Clipboard helper ───────────────────────────────────────────────────────

def _to_clipboard(text: str) -> None:
    """Copy text to the Windows clipboard."""
    try:
        import pyperclip
        pyperclip.copy(text)
    except ImportError:
        # Windows built-in fallback
        subprocess.run("clip", input=text.encode("utf-16-le"), check=True, shell=True)


# ── Background monitor state ───────────────────────────────────────────────

_watch_thread: threading.Thread | None = None
_watch_stop = threading.Event()


# ── Actions ───────────────────────────────────────────────────────────────


@register("read_screen", sig='read_screen(question: str = "")',
           description="Describe screen or answer a question about what's visible", category="screen")
def read_screen(question: str = "") -> str:
    """Describe what's on screen, or answer a specific question about it."""
    try:
        img_b64, _, _ = _screenshot_b64()
        if question:
            prompt = (
                f"{question} "
                "Answer in 2-3 spoken sentences. No markdown, no lists."
            )
        else:
            prompt = (
                "What is currently displayed on this screen? "
                "Summarize in 2-3 conversational sentences as if briefing someone out loud. "
                "No markdown."
            )
        return _vision(img_b64, prompt)
    except Exception as e:
        logger.error("read_screen failed: %s", e)
        return "I couldn't analyze the screen right now."


@register("find_on_screen", sig="find_on_screen(text: str)",
           description="Check if text/element is visible and where", category="screen")
def find_on_screen(text: str) -> str:
    """Locate specific text or a UI element on screen by description."""
    try:
        img_b64, _, _ = _screenshot_b64()
        prompt = (
            f"Is '{text}' visible anywhere on this screen? "
            "Answer in one clear sentence: if yes, describe where it is; "
            "if no, say it is not visible."
        )
        return _vision(img_b64, prompt)
    except Exception as e:
        logger.error("find_on_screen failed: %s", e)
        return "I couldn't search the screen right now."


@register("click_on", sig="click_on(target: str)",
           description="Find a UI element by description and click it", category="screen")
def click_on(target: str) -> str:
    """Find a UI element by description and click it.

    Uses Claude Vision to estimate fractional (x, y) coordinates, then
    converts to screen pixels and fires a pyautogui click.
    """
    try:
        img_b64, screen_w, screen_h = _screenshot_b64()
        prompt = (
            f"Find the element described as '{target}' on this screen. "
            "Respond with ONLY a JSON object — no other text:\n"
            '{"found": true, "x": 0.0, "y": 0.0, "label": "..."}\n'
            "where x is 0.0 (left edge) to 1.0 (right edge), "
            "y is 0.0 (top edge) to 1.0 (bottom edge), "
            "and label is a short description of what you found. "
            'If not found: {"found": false}'
        )
        data = _vision_json(img_b64, prompt)

        if not data.get("found"):
            return f"I couldn't find '{target}' on the screen."

        x = int(float(data["x"]) * screen_w)
        y = int(float(data["y"]) * screen_h)
        label = data.get("label", target)

        pyautogui.click(x, y)
        logger.info("Clicked '%s' at (%d, %d)", label, x, y)
        return f"Clicked {label}."

    except Exception as e:
        logger.error("click_on failed: %s", e)
        return f"I couldn't click '{target}'."


@register("extract_text", sig="extract_text()", description="OCR-dump all visible text to clipboard", category="screen")
def extract_text() -> str:
    """Extract all visible text from the screen (OCR-style) and copy to clipboard.

    Speaks a short summary; full text goes to clipboard.
    """
    try:
        img_b64, _, _ = _screenshot_b64()
        prompt = (
            "Extract every piece of visible text from this screen exactly as it appears. "
            "Return only the raw text, preserving line breaks. No commentary, no formatting."
        )
        text = _vision(img_b64, prompt, max_tokens=1000)
        _to_clipboard(text)
        line_count = len([l for l in text.splitlines() if l.strip()])
        preview = text[:80].replace("\n", " ") + ("…" if len(text) > 80 else "")
        return f"Extracted {line_count} lines and copied to clipboard. Starts with: {preview}"
    except Exception as e:
        logger.error("extract_text failed: %s", e)
        return "I couldn't extract text from the screen."


@register("explain_code_on_screen", sig="explain_code_on_screen()",
           description="Find code on screen and explain it verbally", category="screen")
def explain_code_on_screen() -> str:
    """Find code visible on screen and explain what it does."""
    try:
        img_b64, _, _ = _screenshot_b64()
        prompt = (
            "Look for any code visible on this screen — in an editor, terminal, or browser. "
            "If you find code, explain what it does in 3-4 spoken sentences: "
            "what the code's purpose is, any notable patterns, and potential issues if obvious. "
            "If there's no code visible, say so in one sentence. No markdown."
        )
        return _vision(img_b64, prompt, max_tokens=500)
    except Exception as e:
        logger.error("explain_code_on_screen failed: %s", e)
        return "I couldn't analyze the code on screen."


@register("copy_from_screen", sig="copy_from_screen(what: str)",
           description="Extract specific text from screen to clipboard", category="screen")
def copy_from_screen(what: str) -> str:
    """Extract a specific piece of text from the screen and copy it to clipboard.

    Example: "copy the error message", "copy the API key shown on screen"
    """
    try:
        img_b64, _, _ = _screenshot_b64()
        prompt = (
            f"Find '{what}' on this screen and return its exact text content. "
            "Return ONLY the text — no labels, no explanation, nothing else. "
            f"If '{what}' is not visible, return exactly: NOT_FOUND"
        )
        result = _vision(img_b64, prompt, max_tokens=300)

        if result.strip() == "NOT_FOUND":
            return f"I couldn't find '{what}' on the screen."

        _to_clipboard(result)
        preview = result[:60].replace("\n", " ") + ("…" if len(result) > 60 else "")
        return f"Copied to clipboard: {preview}"
    except Exception as e:
        logger.error("copy_from_screen failed: %s", e)
        return f"I couldn't copy '{what}' from the screen."


def _watch_loop(condition: str, interval_sec: float, timeout_sec: float, stop_event: threading.Event) -> None:
    """Background thread: poll screen until condition is met or timeout."""
    start = time.monotonic()
    check_prompt = (
        f"Is the following condition currently visible or true on this screen: '{condition}'? "
        'Respond with ONLY JSON: {"met": true} or {"met": false}'
    )
    while not stop_event.is_set():
        if time.monotonic() - start > timeout_sec:
            speaker.speak(f"Screen watch timed out. '{condition}' was never detected.")
            return
        try:
            img_b64, _, _ = _screenshot_b64()
            data = _vision_json(img_b64, check_prompt)
            if data.get("met"):
                speaker.speak(f"Detected on screen: {condition}.")
                return
        except Exception as e:
            logger.warning("watch_for check failed: %s", e)
        stop_event.wait(interval_sec)


@register("watch_for", sig="watch_for(condition: str, timeout_minutes: int = 10)",
           description="Monitor screen, speak when condition appears", category="screen")
def watch_for(condition: str, timeout_minutes: int = 10, check_every_seconds: int = 15) -> str:
    """Monitor the screen in the background and speak when a condition appears.

    Example: "watch for the training to finish", "watch for any error message"
    """
    global _watch_thread, _watch_stop

    new_stop = threading.Event()
    if _watch_thread and _watch_thread.is_alive():
        _watch_stop.set()
        _watch_thread.join(timeout=2)

    _watch_stop = new_stop
    _watch_thread = threading.Thread(
        target=_watch_loop,
        args=(condition, float(check_every_seconds), timeout_minutes * 60, new_stop),
        daemon=True,
        name="nora-screen-watch",
    )
    _watch_thread.start()
    return (
        f"Watching the screen for '{condition}'. "
        f"Checking every {check_every_seconds} seconds for up to {timeout_minutes} minutes."
    )


@register("stop_watching", sig="stop_watching()", description="Cancel active screen monitor", category="screen")
def stop_watching() -> str:
    """Cancel an active watch_for monitor."""
    if _watch_thread and _watch_thread.is_alive():
        _watch_stop.set()
        return "Screen monitor cancelled."
    return "No active screen monitor."


@register("debug_screen", sig="debug_screen()",
           description="Scan screen for errors, warnings, stack traces", category="screen")
def debug_screen() -> str:
    """Scan the current screen for errors, exceptions, warnings, or broken state."""
    try:
        img_b64, _, _ = _screenshot_b64()
        prompt = (
            "Look carefully at this screen for errors, warnings, stack traces, "
            "exception messages, broken UI, or anything that looks wrong. "
            "If you find issues, describe them and suggest a likely cause in 3-4 spoken sentences. "
            "If everything looks fine, say so in one sentence. No markdown."
        )
        return _vision(img_b64, prompt, max_tokens=500)
    except Exception as e:
        logger.error("debug_screen failed: %s", e)
        return "I couldn't analyze the screen for errors."

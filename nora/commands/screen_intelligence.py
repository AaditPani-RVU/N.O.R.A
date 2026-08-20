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

All vision calls use Groq's llama-3.2-90b-vision-preview — free tier, uses your existing GROQ_API_KEY.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import os
import platform
import subprocess
import threading
import time
from io import BytesIO
from typing import Any

from nora.command_engine import register
from nora.config import get_config
import nora.speaker as speaker

logger = logging.getLogger("nora.commands.screen_intelligence")

# llama-4-scout was decommissioned — it 404s on the Groq API as of 2026-08-19,
# which silently killed read_screen, find_on_screen and click_on. qwen3.6-27b
# is the only vision model left on Groq. Read from config so the next
# deprecation is a config edit rather than a code change.
_DEFAULT_VISION_MODEL = "qwen/qwen3.6-27b"
_GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# qwen3.6 is a reasoning model: it emits a <think> block before its answer and
# that block consumes the token budget. Two consequences, both measured:
#   - the reply must be stripped of <think> before it is spoken or JSON-parsed
#   - max_tokens has to clear the reasoning or `content` comes back empty
#     (200 tokens -> "", 600 -> a full answer in ~5s)
# reasoning_format:"hidden" looks like the fix and is not — the reasoning is
# still generated, so it costs 21-29s instead of 5s for the same answer.
_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_ORPHAN_THINK_RE = re.compile(r"^.*?</think>", re.S | re.I)
# A <think> with no closing tag: the reply was cut off mid-reasoning, so
# everything from the tag onward is scratchpad. Without this the whole
# scratchpad ("The user wants me to identify what is currently on the
# screen...") gets read aloud as if it were the answer.
_UNCLOSED_THINK_RE = re.compile(r"<think>.*\Z", re.S | re.I)

# Never ask for fewer than this. The reasoning block is generated before any
# content, so a budget that doesn't clear it returns an empty string —
# measured on qwen3.6-27b: 200 tokens -> "", 600 -> a full answer in ~5s.
_MIN_VISION_TOKENS = 700


class VisionTruncated(RuntimeError):
    """The model spent its whole token budget reasoning and never answered."""


def _vision_model() -> str:
    return get_config().get("screen_intelligence", {}).get(
        "vision_model", _DEFAULT_VISION_MODEL)


def _strip_reasoning(text: str) -> str:
    """Remove the model's <think> block, closed, orphaned, or unterminated."""
    text = _THINK_RE.sub(" ", text)
    if "</think>" in text.lower():
        text = _ORPHAN_THINK_RE.sub(" ", text)
    text = _UNCLOSED_THINK_RE.sub(" ", text)
    return text.strip()

# ── Shared Groq client (lazy-init, OpenAI-compatible) ─────────────────────

_client: Any = None


def _get_client() -> Any:
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(
            api_key=os.environ.get("GROQ_API_KEY"),
            base_url=_GROQ_BASE_URL,
        )
    return _client


# ── Screenshot helpers ─────────────────────────────────────────────────────

def _screenshot_b64() -> tuple[str, int, int]:
    """Return (base64_png, width, height) — works on Linux, Windows, and Mac."""
    import mss
    from PIL import Image

    with mss.mss() as sct:
        # monitors[0] is the combined bounding box of all screens
        monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        raw = sct.grab(monitor)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

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


def _vision(image_b64: str, prompt: str, max_tokens: int = _MIN_VISION_TOKENS) -> str:
    """Send a screenshot to Groq vision, return spoken text.

    A dense screen makes the model reason longer, so a budget that was ample
    yesterday can be spent entirely on the <think> block today. That comes back
    as a truncated reply with no answer in it; retry once with double the
    budget, then give up rather than speaking the scratchpad.
    """
    budget = max(int(max_tokens), _MIN_VISION_TOKENS)

    for attempt in (1, 2):
        client = _get_client()
        resp = client.chat.completions.create(
            model=_vision_model(),
            max_tokens=budget,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                        },
                        {"type": "text", "text": prompt},
                    ],
                },
            ],
        )
        choice = resp.choices[0]
        answer = _strip_reasoning(choice.message.content or "")
        if answer:
            return answer

        truncated = choice.finish_reason == "length"
        logger.warning(
            "Vision returned no answer at %d tokens (finish_reason=%s)",
            budget, choice.finish_reason)
        if attempt == 2 or not truncated:
            break
        budget *= 2

    raise VisionTruncated(
        f"vision model produced only reasoning within {budget} tokens")


def _vision_json(image_b64: str, prompt: str) -> dict:
    """Send a screenshot, parse the response as JSON. Returns {} on failure."""
    # 200 was enough for the old non-reasoning model and returns an empty
    # string from this one — the reasoning eats the whole budget.
    try:
        raw = _vision(image_b64, prompt, max_tokens=700)
    except VisionTruncated as e:
        logger.warning("Vision JSON unavailable: %s", e)
        return {}
    try:
        # Strip any accidental markdown fences
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(cleaned)
    except Exception:
        logger.warning("Vision JSON parse failed: %s", raw[:200])
        return {}


# ── Clipboard helper ───────────────────────────────────────────────────────

def _to_clipboard(text: str) -> None:
    """Copy text to clipboard — cross-platform."""
    encoded = text.encode("utf-8")
    if platform.system() == "Windows":
        subprocess.run("clip", input=text.encode("utf-16-le"), check=True, shell=True)
        return
    for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"], ["wl-copy"]):
        try:
            subprocess.run(cmd, input=encoded, check=True, timeout=3)
            return
        except Exception:
            continue
    logger.warning("No clipboard tool found (install xclip or xsel)")


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
           description="Find a UI element by description, click it, then verify the click worked",
           category="screen")
def click_on(target: str) -> str:
    """Find a UI element, click it, then screenshot-verify the click had effect.

    Flow: locate → click → 0.6s wait → screenshot → verify
    Stores before/after state in reversible log.
    """
    import time as _time
    try:
        # 1. Locate
        img_b64, screen_w, screen_h = _screenshot_b64()
        locate_prompt = (
            f"Find the element described as '{target}' on this screen. "
            "Respond with ONLY a JSON object — no other text:\n"
            '{"found": true, "x": 0.0, "y": 0.0, "label": "...", "bbox": [x1, y1, x2, y2]}\n'
            "where x/y are fractional 0-1 coordinates, bbox is optional bounding box, "
            "and label is a short description of what you found. "
            'If not found: {"found": false}'
        )
        before_data = _vision_json(img_b64, locate_prompt)

        if not before_data.get("found"):
            return f"I couldn't find '{target}' on the screen."

        x_frac = before_data.get("x")
        y_frac = before_data.get("y")
        if x_frac is None or y_frac is None:
            return f"Vision model found '{target}' but returned no coordinates."
        x_frac = float(x_frac)
        y_frac = float(y_frac)
        px_x = int(x_frac * screen_w)
        px_y = int(y_frac * screen_h)
        label = before_data.get("label", target)

        # 2. Click — prefer ydotool/xdotool on Linux (Wayland-safe), fall back to pyautogui (X11)
        if platform.system() == "Linux":
            from nora.platform.linux import ydotool_input
            if not ydotool_input.click(px_x, px_y):
                import pyautogui as _pg
                _pg.click(px_x, px_y)
        else:
            import pyautogui
            pyautogui.click(px_x, px_y)
        logger.info("Clicked '%s' at (%d, %d)", label, px_x, px_y)

        # 3. Wait for UI to respond
        _time.sleep(0.6)

        # 4. Verify — ask if something changed / the element was interacted with
        after_b64, _, _ = _screenshot_b64()
        verify_prompt = (
            f"Before a click on '{label}', the screen looked a certain way. "
            "Has something visually changed that suggests the click worked? "
            'Respond ONLY as JSON: {"changed": true/false, "observation": "one sentence"}'
        )
        verify_data = _vision_json(after_b64, verify_prompt)
        changed = verify_data.get("changed", True)
        observation = verify_data.get("observation", "")

        # Log for reversibility (can't undo a click, but record it)
        try:
            from nora.reversible import record_action
            record_action(
                action="click_on",
                params={"target": target},
                inverse_action=None,
                inverse_params={},
                description=f"Clicked '{label}' on screen",
                reversible=False,
            )
        except Exception:
            pass

        if changed:
            return f"Clicked {label}. {observation}" if observation else f"Clicked {label} — UI responded."
        return f"Clicked {label}, but the screen may not have changed. {observation}"

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


# ── Multimodal Context Fusion — Sprint 4 Task 9 ───────────────────────────

# Cache: (snippet_text, window_title, captured_at_monotonic)
_snippet_cache: tuple[str, str, float] = ("", "", 0.0)
_SNIPPET_TTL = 10.0  # seconds
_snippet_lock = threading.Lock()


def get_screen_snippet(max_chars: int = 250) -> tuple[str, str]:
    """Return a (text_snippet, window_title) pair from a quick screen read.

    Cached for _SNIPPET_TTL seconds to avoid hammering Vision on every utterance.
    Returns ("", "") on failure — callers must handle gracefully.
    """
    import time as _time
    global _snippet_cache

    with _snippet_lock:
        snippet, title, ts = _snippet_cache
        if snippet and _time.monotonic() - ts < _SNIPPET_TTL:
            return snippet, title

    try:
        # Get active window title — cross-platform
        window_title = ""
        try:
            if platform.system() == "Windows":
                import ctypes
                hwnd = ctypes.windll.user32.GetForegroundWindow()
                buf = ctypes.create_unicode_buffer(256)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
                window_title = buf.value.strip()
            else:
                try:
                    out = subprocess.run(
                        ["xdotool", "getactivewindow", "getwindowname"],
                        capture_output=True, text=True, timeout=2,
                    ).stdout.strip()
                    if out:
                        window_title = out.splitlines()[0]
                except Exception:
                    try:
                        wid = subprocess.run(
                            ["sh", "-c", "xprop -root _NET_ACTIVE_WINDOW | awk '{print $5}'"],
                            capture_output=True, text=True, timeout=2,
                        ).stdout.strip()
                        if wid and wid != "0x0":
                            out = subprocess.run(
                                ["xprop", "-id", wid, "WM_NAME"],
                                capture_output=True, text=True, timeout=2,
                            ).stdout.strip()
                            if "=" in out:
                                window_title = out.split("=", 1)[1].strip().strip('"')
                    except Exception:
                        pass
        except Exception:
            pass

        img_b64, _, _ = _screenshot_b64()
        prompt = (
            "In at most 3 short sentences, describe: (1) what application is active, "
            "(2) what the user appears to be working on, (3) any text, code, or key "
            "content that is most prominent. No markdown. Be factual and terse."
        )
        snippet_text = _vision(img_b64, prompt, max_tokens=120)
        snippet_text = snippet_text[:max_chars]

        with _snippet_lock:
            _snippet_cache = (snippet_text, window_title, _time.monotonic())

        return snippet_text, window_title
    except Exception as e:
        logger.debug("get_screen_snippet failed: %s", e)
        return "", ""


def invalidate_snippet_cache() -> None:
    """Force the next get_screen_snippet() call to re-capture."""
    global _snippet_cache
    with _snippet_lock:
        _snippet_cache = ("", "", 0.0)

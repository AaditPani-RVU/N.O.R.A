"""Screen vision commands â€” analyze, search, and debug the current screen.

Uses Claude Vision (claude-haiku-4-5-20251001) to understand what's on screen.
No OCR dependencies required; everything goes through the Anthropic SDK.
"""
from __future__ import annotations

import base64
import logging
from io import BytesIO

import pyautogui

from nora.command_engine import register

logger = logging.getLogger("nora.commands.screen_intelligence")


def _screenshot_b64() -> str:
    """Capture the current screen as a base64-encoded PNG."""
    img = pyautogui.screenshot()
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _vision(image_b64: str, prompt: str, max_tokens: int = 400) -> str:
    """Send a screenshot to Claude Vision and return the spoken response."""
    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": image_b64,
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }]
    )
    return msg.content[0].text.strip()


@register("read_screen")
def read_screen(question: str = "") -> str:
    """Analyze the current screen using Claude Vision and describe what's visible."""
    try:
        img_b64 = _screenshot_b64()
        if question:
            prompt = (
                f"{question} "
                "Answer concisely in 2-3 spoken sentences. No markdown, no lists."
            )
        else:
            prompt = (
                "What is currently displayed on this screen? "
                "Summarize it in 2-3 conversational sentences as if briefing someone out loud. "
                "No markdown, no bullet points."
            )
        return _vision(img_b64, prompt)
    except Exception as e:
        logger.error("read_screen failed: %s", e)
        return "I couldn't analyze the screen right now."


@register("find_on_screen")
def find_on_screen(text: str) -> str:
    """Find specific text or an element on screen using Claude Vision."""
    try:
        img_b64 = _screenshot_b64()
        prompt = (
            f"Is '{text}' visible anywhere on this screen? "
            "Answer in one clear sentence: if yes, describe where it is; if no, say it is not visible."
        )
        return _vision(img_b64, prompt)
    except Exception as e:
        logger.error("find_on_screen failed: %s", e)
        return "I couldn't search the screen right now."


@register("debug_screen")
def debug_screen() -> str:
    """Analyze the screen for errors, bugs, or warnings and suggest what's wrong."""
    try:
        img_b64 = _screenshot_b64()
        prompt = (
            "Look carefully at this screen for any errors, warnings, stack traces, "
            "exception messages, or anything that looks broken. "
            "If you find issues, describe them and suggest what might be causing them "
            "in 2-4 conversational sentences. "
            "If everything looks fine, say so in one sentence. No markdown."
        )
        return _vision(img_b64, prompt, max_tokens=500)
    except Exception as e:
        logger.error("debug_screen failed: %s", e)
        return "I couldn't analyze the screen for errors."

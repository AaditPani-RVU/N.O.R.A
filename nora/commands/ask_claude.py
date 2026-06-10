"""Ask Claude command — shells out to the `claude` CLI so no API key is needed."""
from __future__ import annotations

import logging
import subprocess
import shutil

from nora.command_engine import register

logger = logging.getLogger("nora.commands.ask_claude")

CLAUDE_MODELS = {
    "opus":   "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku":  "claude-haiku-4-5-20251001",
}
DEFAULT_MODEL = "sonnet"


def _active_window_title() -> str:
    for cmd in (
        ["xdotool", "getactivewindow", "getwindowname"],
        ["wmctrl", "-l"],  # fallback — returns all windows
    ):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=1).stdout.strip()
            if out:
                return out.splitlines()[0]
        except Exception:
            pass
    return ""


def _clipboard_text() -> str:
    for cmd in (
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
        ["wl-paste"],  # Wayland
    ):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=1).stdout
            if out:
                return out[:2000]
        except Exception:
            pass
    return ""


def _build_context_block() -> str:
    parts: list[str] = []
    window = _active_window_title()
    if window:
        parts.append(f"Active window: {window}")
    clip = _clipboard_text().strip()
    if clip:
        parts.append(f"Clipboard:\n{clip}")
    if not parts:
        return ""
    return "[Current context]\n" + "\n".join(parts) + "\n\n"


def _call_claude(prompt: str, model_key: str = DEFAULT_MODEL) -> str:
    """Send a prompt to Claude via the `claude` CLI and return the response text."""
    if not shutil.which("claude"):
        return "Claude CLI not found. Make sure Claude Code is installed and `claude` is in PATH."

    model_id = CLAUDE_MODELS.get(model_key, CLAUDE_MODELS[DEFAULT_MODEL])
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", model_id, "--output-format", "text"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.error("claude CLI error: %s", result.stderr[:200])
            return "Claude returned an error. Check that you're logged in with `claude`."
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "Claude took too long to respond."
    except Exception as e:
        logger.error("ask_claude subprocess error: %s", e)
        return "Couldn't reach Claude right now."


@register(
    "ask_claude",
    sig="ask_claude(question: str, model: str = 'sonnet')",
    description="Ask Claude (via claude CLI) and speak the answer. model: opus | sonnet | haiku",
    category="web",
)
def ask_claude(question: str, model: str = DEFAULT_MODEL) -> str:
    context_block = _build_context_block()
    prompt = (
        f"{context_block}"
        f"Answer in 2-4 spoken sentences. No bullet points, no markdown — "
        f"plain conversational text as if speaking out loud.\n\nQuestion: {question}"
    )
    logger.info("Asking Claude (%s): %s", model, question)
    response = _call_claude(prompt, model_key=model)
    logger.info("Claude responded: %s…", response[:80])
    return response


@register(
    "ask_claude_opus",
    sig="ask_claude_opus(question: str)",
    description="Ask Claude Opus (most capable) for a detailed answer.",
    category="web",
)
def ask_claude_opus(question: str) -> str:
    return ask_claude(question, model="opus")

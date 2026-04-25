#!/usr/bin/env python3
"""
NORA Claude Code voice notification hook.

Triggered by Claude Code's Stop event. Reads the last assistant message,
summarizes it to a NORA-style spoken line via Claude API (Haiku + prompt caching),
then speaks it through the NORA Edge TTS speaker.

Hook config (~/.claude/settings.json):
    "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "cat | python d:/JARVIS/hook_notify.py"}]}]

Disable for a session:
    export NORA_VOICE=off
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import sys

# ---------------------------------------------------------------------------
# NORA root on path so we can import nora.speaker
# ---------------------------------------------------------------------------
NORA_ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(NORA_ROOT))

logging.basicConfig(
    filename=str(NORA_ROOT / "hook_notify.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("hook_notify")

# ---------------------------------------------------------------------------
# NORA system prompt â€” stable across every call â†’ gets cached after first use
# Prompt caching saves ~90% on input tokens from the 2nd call onward.
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """\
You are J.A.R.V.I.S., providing spoken status updates to your creator.

RULES â€” follow every one without exception:
1. Respond with EXACTLY 1-2 short sentences, 10-25 words total.
2. Always begin with "Sir,"
3. Speak in first person as NORA doing the work:
   - Completions  â†’ "Sir, I have [verb] [subject]."
   - Errors       â†’ "Sir, I have encountered [problem] with [subject]."
   - Decisions    â†’ "Sir, I require your input regarding [topic]."
4. Format for text-to-speech (spell out acronyms, no symbols):
   - API â†’ A P I   |  JWT â†’ J W T   |  URL â†’ U R L   |  HTTP â†’ H T T P
   - JSON â†’ jason  |  SQL â†’ sequel  |  OAuth â†’ oh-auth
   - Avoid colons, parentheses, dashes used as punctuation
5. NEVER mention: file paths, line numbers, markdown syntax, code blocks,
   function names, or variable names.
6. Output ONLY the spoken text â€” no preamble, no quotes, no labels.\
"""


def _read_last_assistant_message(transcript_path: str) -> str | None:
    """Return the text of the last assistant turn in a Claude Code JSONL transcript."""
    try:
        last_text: str | None = None
        with open(transcript_path, encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                if entry.get("role") != "assistant":
                    continue

                content = entry.get("content", "")
                if isinstance(content, list):
                    parts = [
                        block["text"]
                        for block in content
                        if isinstance(block, dict)
                        and block.get("type") == "text"
                        and block.get("text", "").strip()
                    ]
                    if parts:
                        last_text = " ".join(parts)
                elif isinstance(content, str) and content.strip():
                    last_text = content

        return last_text
    except Exception as exc:
        log.warning("Could not read transcript %s: %s", transcript_path, exc)
        return None


def _trim(text: str, max_chars: int = 3000) -> str:
    """Keep the start and end of long messages â€” the summary lives there."""
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2]
    tail = text[-(max_chars // 4) :]
    return f"{head}\n\n[...truncated...]\n\n{tail}"


def _summarize_with_claude(text: str) -> str:
    """
    Call Claude Haiku via the Anthropic SDK with a cached system prompt.

    Token strategy:
    - System prompt (~200 tokens) is cached after the first call (cache_control ephemeral)
    - Subsequent calls pay ~0.1Ã— on those tokens instead of full price
    - max_tokens=80 keeps output tight and fast
    - Haiku 4.5: $1/1M input, $5/1M output â€” cheapest model, perfect for this task
    """
    import anthropic  # imported here so startup is instant when voice is off

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from environment

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=80,
        system=[
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # cached for 5 minutes
            }
        ],
        messages=[
            {
                "role": "user",
                "content": _trim(text),
            }
        ],
    )

    speech = response.content[0].text.strip()
    log.info(
        "Claude summary: %s | cache_read=%s input=%s output=%s",
        speech,
        response.usage.cache_read_input_tokens,
        response.usage.input_tokens,
        response.usage.output_tokens,
    )
    return speech


def _speak(text: str) -> None:
    """Speak text via the NORA Edge TTS speaker."""
    from nora.speaker import speak

    speak(text)


def main() -> None:
    # ------------------------------------------------------------------
    # 1. Read the Stop hook JSON from stdin
    # ------------------------------------------------------------------
    try:
        raw = sys.stdin.read()
        hook_data: dict = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        log.error("Failed to parse hook stdin: %s", exc)
        sys.exit(0)

    # ------------------------------------------------------------------
    # 2. Respect kill-switch
    # ------------------------------------------------------------------
    if os.environ.get("NORA_VOICE", "").lower() == "off":
        sys.exit(0)

    # ------------------------------------------------------------------
    # 3. Locate transcript
    # ------------------------------------------------------------------
    transcript_path = hook_data.get("transcript_path", "")
    if not transcript_path or not os.path.exists(transcript_path):
        log.debug("No transcript path in hook data: %s", hook_data)
        sys.exit(0)

    # ------------------------------------------------------------------
    # 4. Extract last assistant message
    # ------------------------------------------------------------------
    last_message = _read_last_assistant_message(transcript_path)
    if not last_message or len(last_message.strip()) < 15:
        log.debug("No usable assistant message found")
        sys.exit(0)

    # ------------------------------------------------------------------
    # 5. Summarize with Claude (Haiku + cached system prompt)
    # ------------------------------------------------------------------
    try:
        speech_text = _summarize_with_claude(last_message)
    except Exception as exc:
        log.error("Claude summarization failed: %s", exc)
        speech_text = "Sir, I have completed the task."

    # ------------------------------------------------------------------
    # 6. Speak via NORA
    # ------------------------------------------------------------------
    try:
        log.info("Speaking: %s", speech_text)
        _speak(speech_text)
    except Exception as exc:
        log.error("Speaker failed: %s", exc)


if __name__ == "__main__":
    main()

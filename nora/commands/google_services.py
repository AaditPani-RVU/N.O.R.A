"""Google Calendar and Gmail integration via the Claude CLI + MCP servers.

The Claude CLI must have Google Calendar and Gmail MCP servers configured
and authenticated. Run `claude` interactively and authenticate once if needed.

All functions shell out to `claude -p <prompt> --allowedTools <mcp-tools>`
exactly like ask_claude and tell_me_about do.
"""
from __future__ import annotations

import logging
import subprocess

from nora.command_engine import register

logger = logging.getLogger("nora.commands.google_services")

_CALENDAR_TOOLS = (
    "mcp__claude_ai_Google_Calendar__authenticate,"
    "mcp__claude_ai_Google_Calendar__complete_authentication,"
    "mcp__claude_ai_Google_Calendar__list_events,"
    "mcp__claude_ai_Google_Calendar__create_event,"
    "mcp__claude_ai_Google_Calendar__update_event,"
    "mcp__claude_ai_Google_Calendar__delete_event"
)

_GMAIL_TOOLS = (
    "mcp__claude_ai_Gmail__authenticate,"
    "mcp__claude_ai_Gmail__complete_authentication,"
    "mcp__claude_ai_Gmail__list_messages,"
    "mcp__claude_ai_Gmail__get_message,"
    "mcp__claude_ai_Gmail__send_email,"
    "mcp__claude_ai_Gmail__search_messages"
)

_NOT_AUTH_MSG = (
    "Make sure you've authenticated {} in Claude Code first by running it interactively."
)


def _claude(prompt: str, tools: str, timeout: int = 60) -> str | None:
    """Run Claude CLI with MCP tools and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--allowedTools", tools],
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=True,
        )
        if result.returncode != 0:
            logger.error("Claude CLI error: %s", result.stderr[:300])
            return None
        return result.stdout.strip() or None
    except subprocess.TimeoutExpired:
        logger.warning("Claude CLI timed out for prompt: %s", prompt[:80])
        return None
    except FileNotFoundError:
        logger.error("Claude CLI not found in PATH")
        return None
    except Exception as e:
        logger.error("Claude CLI failed: %s", e)
        return None


@register("check_calendar")
def check_calendar(when: str = "today") -> str:
    """Check Google Calendar for upcoming events."""
    prompt = (
        f"Check my Google Calendar for events {when}. "
        "List them concisely in plain spoken text with no markdown or bullet points. "
        "Say something like: You have 3 events today â€” standup at 9am, lunch at noon, and a review at 3pm. "
        "If there are no events, say so briefly."
    )
    response = _claude(prompt, _CALENDAR_TOOLS)
    return response or _NOT_AUTH_MSG.format("Google Calendar")


@register("add_calendar_event")
def add_calendar_event(title: str, date: str = "today", time: str = "") -> str:
    """Add an event to Google Calendar."""
    time_clause = f" at {time}" if time else ""
    prompt = (
        f"Add an event titled '{title}' to my Google Calendar on {date}{time_clause}. "
        "Confirm it was added in one spoken sentence with no markdown."
    )
    response = _claude(prompt, _CALENDAR_TOOLS)
    return response or _NOT_AUTH_MSG.format("Google Calendar")


@register("check_email")
def check_email(filter: str = "unread") -> str:
    """Check Gmail for recent messages."""
    prompt = (
        f"Check my Gmail for {filter} emails. "
        "Summarize the most important ones in 3-5 conversational sentences â€” no markdown, no formatting. "
        "Include sender names and brief subjects. If there are no emails, say so."
    )
    response = _claude(prompt, _GMAIL_TOOLS)
    return response or _NOT_AUTH_MSG.format("Gmail")


@register("send_email")
def send_email(to: str, subject: str, body: str) -> str:
    """Compose and send an email via Gmail."""
    prompt = (
        f"Send an email via Gmail to {to} with subject '{subject}' and this body: {body}. "
        "Confirm it was sent in one spoken sentence."
    )
    response = _claude(prompt, _GMAIL_TOOLS, timeout=90)
    return response or _NOT_AUTH_MSG.format("Gmail")

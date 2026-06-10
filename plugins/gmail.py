"""NORA plugin: rich Gmail voice commands.

Supplements ``nora/commands/google_services.py`` (which provides
``check_email`` and ``send_email``) with additional interactions.

Voice commands
--------------
"read my latest emails"                  → gmail_latest(n=3)
"draft a reply to the email from John"   → gmail_draft_reply(sender="John")
"search my email for invoices"           → gmail_search(query="invoices")
"what are my important emails"           → gmail_important()

Requires Gmail MCP authenticated in Claude Code.
Run ``claude`` interactively once to complete the OAuth flow if needed.
"""
from __future__ import annotations

import logging
import subprocess

from nora.command_engine import register

logger = logging.getLogger("nora.plugins.gmail")

_TOOLS = (
    "mcp__claude_ai_Gmail__search_threads,"
    "mcp__claude_ai_Gmail__get_thread,"
    "mcp__claude_ai_Gmail__create_draft,"
    "mcp__claude_ai_Gmail__list_drafts,"
    "mcp__claude_ai_Gmail__list_labels"
)
_AUTH = (
    "Authenticate Gmail in Claude Code first — "
    "run `claude` interactively and complete the OAuth flow."
)


def _mail(prompt: str, timeout: int = 90) -> str | None:
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--allowedTools", _TOOLS],
            capture_output=True, text=True, timeout=timeout, shell=True,
        )
        text = result.stdout.strip()
        return text if result.returncode == 0 and text else None
    except Exception as exc:
        logger.warning("Claude CLI Gmail error: %s", exc)
        return None


@register(
    "gmail_latest",
    sig="gmail_latest(n: int = 3)",
    description="Read the N most recent emails aloud",
    category="notification",
)
def gmail_latest(n: int = 3) -> str:
    return _mail(
        f"Read my {n} most recent Gmail emails. For each one, say the sender, "
        "the subject, and a one-sentence summary. "
        "Plain spoken prose only — no markdown, no bullets, no formatting."
    ) or _AUTH


@register(
    "gmail_draft_reply",
    sig='gmail_draft_reply(sender: str = "", topic: str = "")',
    description="Draft a reply to a recent email",
    category="notification",
)
def gmail_draft_reply(sender: str = "", topic: str = "") -> str:
    clauses: list[str] = []
    if sender:
        clauses.append(f"from {sender}")
    if topic:
        clauses.append(f"about {topic}")
    target = " ".join(clauses) or "the most recent"
    return _mail(
        f"Find the {target} email in my Gmail, read its content, and draft a "
        "polite professional reply. Save it as a Gmail draft. "
        "Confirm in one spoken sentence what you drafted and to whom. No markdown."
    ) or _AUTH


@register(
    "gmail_search",
    sig="gmail_search(query: str)",
    description="Search Gmail and summarize matching threads",
    category="notification",
)
def gmail_search(query: str) -> str:
    if not query:
        return "What would you like to search for?"
    return _mail(
        f"Search my Gmail for '{query}'. Summarize the top 3 results: "
        "for each one say the sender, subject, and a one-sentence summary. "
        "Plain spoken prose, no markdown."
    ) or _AUTH


@register(
    "gmail_important",
    sig="gmail_important()",
    description="Summarize important or starred unread emails",
    category="notification",
)
def gmail_important() -> str:
    return _mail(
        "Check my Gmail for important, starred, or high-priority unread emails. "
        "Summarize the top 3 in conversational spoken prose. No markdown or formatting."
    ) or _AUTH

from __future__ import annotations

import logging
import re
import subprocess
import urllib.parse
import webbrowser

from nora.command_engine import register

logger = logging.getLogger("nora.commands.web_search")


def _normalize_query(query: str) -> str:
    """Collapse spelled-out letter sequences like N-E-U-R-O-S-Y-M-A-I → neurosym-ai.

    When users spell a word for Whisper, each letter comes through hyphen-separated.
    We collapse them back to lowercase so searches hit the right target.
    """
    # Match 2+ single uppercase letters separated by hyphens: N-E-U-R-O-S-Y-M
    def collapse(m: re.Match) -> str:
        return m.group(0).replace("-", "").lower()

    return re.sub(r"\b[A-Z](?:-[A-Z]){2,}\b", collapse, query)


@register("web_search", sig="web_search(query: str)",
           description='ONLY for "search for" / "google". Opens browser silently.', category="web")
def web_search(query: str) -> str:
    """Open a web search in the default browser."""
    encoded = urllib.parse.quote_plus(query)
    url = f"https://www.google.com/search?q={encoded}"
    webbrowser.open(url)
    return f"Searching for: {query}"


@register("open_url", sig="open_url(url: str)", category="web")
def open_url(url: str) -> str:
    """Open a URL in the default browser."""
    webbrowser.open(url)
    return f"Opened: {url}"


@register("tell_me_about", sig="tell_me_about(query: str)",
           description="Research + SPEAK. Pack ALL qualifiers (language, platform, domain) into query string.",
           category="web")
def tell_me_about(query: str) -> str:
    """Search the web for a topic and narrate a summary.

    Uses Claude Code CLI which has built-in web search to get
    real, up-to-date information and summarize it conversationally.
    """
    query = _normalize_query(query)
    logger.info(f"Researching: {query}")

    prompt = (
        f'Search the web for "{query}" and give me a concise, accurate summary. '
        f"Use the full query to disambiguate — if it mentions a programming language, "
        f"library, author, or platform, prioritise those results over unrelated companies "
        f"or people with similar names. "
        f"Keep it conversational — 3-5 spoken sentences, no bullet points, no markdown."
    )

    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--allowedTools", "WebSearch,WebFetch"],
            capture_output=True,
            text=True,
            timeout=60,
            shell=True,
        )

        if result.returncode != 0:
            logger.error(f"Claude CLI error: {result.stderr}")
            return f"I had trouble researching {query}."

        response = result.stdout.strip()
        if response:
            logger.info(f"Research response: {response[:100]}...")
            return response

        return f"I couldn't find information about {query}."

    except subprocess.TimeoutExpired:
        return f"Research on {query} took too long. Try a more specific question."
    except FileNotFoundError:
        return "Claude CLI is not available for web research."
    except Exception as e:
        logger.error(f"tell_me_about error: {e}")
        return f"Something went wrong researching {query}."

from __future__ import annotations

import logging
import subprocess
import urllib.parse
import webbrowser

from nora.command_engine import register

logger = logging.getLogger("nora.commands.web_search")


@register("web_search")
def web_search(query: str) -> str:
    """Open a web search in the default browser."""
    encoded = urllib.parse.quote_plus(query)
    url = f"https://www.google.com/search?q={encoded}"
    webbrowser.open(url)
    return f"Searching for: {query}"


@register("open_url")
def open_url(url: str) -> str:
    """Open a URL in the default browser."""
    webbrowser.open(url)
    return f"Opened: {url}"


@register("tell_me_about")
def tell_me_about(query: str) -> str:
    """Search the web for a topic and narrate a summary.

    Uses Claude Code CLI which has built-in web search to get
    real, up-to-date information and summarize it conversationally.
    """
    logger.info(f"Researching: {query}")

    prompt = (
        f'Search the web for "{query}" and give me a concise summary. '
        f"Keep it conversational and informative â€” 3-5 spoken sentences. "
        f"No bullet points, no markdown, no formatting. Just plain spoken text "
        f"as if you're briefing someone out loud."
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

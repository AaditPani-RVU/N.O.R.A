from __future__ import annotations

import logging
import os
import re
import urllib.parse
import webbrowser

import requests

from nora.command_engine import register

logger = logging.getLogger("nora.commands.web_search")


def _normalize_query(query: str) -> str:
    def collapse(m: re.Match) -> str:
        return m.group(0).replace("-", "").lower()
    return re.sub(r"\b[A-Z](?:-[A-Z]){2,}\b", collapse, query)


# ── Search backends ────────────────────────────────────────────────────────────

def _brave_search(query: str, count: int = 5) -> list[str]:
    """Brave Search REST API — returns title+snippet strings."""
    key = os.environ.get("BRAVE_API_KEY", "")
    if not key or "your_brave" in key:
        return []
    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
            params={"q": query, "count": count},
            timeout=5,
        )
        resp.raise_for_status()
        results = resp.json().get("web", {}).get("results", [])
        snippets = []
        for r in results:
            title = r.get("title", "")
            desc  = r.get("description", "") or r.get("extra_snippets", [""])[0]
            if desc:
                snippets.append(f"{title}: {desc}")
        return snippets[:count]
    except Exception as e:
        logger.debug("Brave search error: %s", e)
        return []


def _ddg_search(query: str) -> list[str]:
    """DuckDuckGo HTML search — free, no key, scrapes lite endpoint."""
    try:
        resp = requests.get(
            "https://lite.duckduckgo.com/lite/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (compatible; NORA/1.0)"},
            timeout=6,
        )
        resp.raise_for_status()
        # Extract result snippets — DDG Lite uses <td class="result-snippet">
        snippets = re.findall(
            r'class=["\']result-snippet["\'][^>]*>(.*?)</td>',
            resp.text, re.DOTALL
        )
        # Strip HTML tags and decode entities
        clean = []
        for s in snippets[:5]:
            s = re.sub(r"<[^>]+>", "", s).strip()
            s = s.replace("&amp;", "&").replace("&quot;", '"').replace("&#x27;", "'")
            if s:
                clean.append(s)
        return clean
    except Exception as e:
        logger.debug("DDG search error: %s", e)
        return []


def _fetch_snippets(query: str) -> list[str]:
    """Try Brave first, fall back to DDG."""
    snippets = _brave_search(query)
    if not snippets:
        snippets = _ddg_search(query)
    return snippets


# ── Summariser ─────────────────────────────────────────────────────────────────

def _summarise(query: str, snippets: list[str]) -> str:
    """Feed real search snippets to Groq and get a 2-sentence spoken answer."""
    from openai import OpenAI

    context = "\n".join(f"- {s}" for s in snippets[:5])
    prompt = (
        f"Answer the question in 2 concise spoken sentences using only the provided "
        f"search results. No filler words ('so', 'well', 'basically'). "
        f"If the results don't contain a clear answer, say so briefly.\n\n"
        f"Question: {query}\n\nSearch results:\n{context}"
    )
    try:
        client = OpenAI(
            api_key=os.environ.get("GROQ_API_KEY", ""),
            base_url="https://api.groq.com/openai/v1",
        )
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=180,
            temperature=0.2,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error("summarise error: %s", e)
        return "I couldn't summarise the results right now."


# ── Commands ───────────────────────────────────────────────────────────────────

@register("web_search", sig="web_search(query: str)",
           description='ONLY for "search for" / "google". Opens browser silently.', category="web")
def web_search(query: str) -> str:
    encoded = urllib.parse.quote_plus(query)
    webbrowser.open(f"https://www.google.com/search?q={encoded}")
    return f"Searching for: {query}"


@register("open_url", sig="open_url(url: str)", category="web")
def open_url(url: str) -> str:
    webbrowser.open(url)
    return f"Opened: {url}"


@register(
    "tell_me_about",
    sig="tell_me_about(query: str)",
    description="Search the web + SPEAK result. Use for any factual/current-events question.",
    category="web",
)
def tell_me_about(query: str) -> str:
    """Fetch real search results then summarise with Groq in 2 spoken sentences."""
    query = _normalize_query(query)
    logger.info("Researching: %s", query)

    snippets = _fetch_snippets(query)

    if not snippets:
        # Last resort: pure LLM with a disclaimer about potentially stale data
        logger.warning("No search results — falling back to LLM knowledge")
        return _summarise_no_context(query)

    logger.info("Got %d snippet(s) from search", len(snippets))
    response = _summarise(query, snippets)
    logger.info("Research response: %s…", response[:80])
    return response


def _summarise_no_context(query: str) -> str:
    """LLM-only fallback when all search backends fail."""
    from openai import OpenAI
    try:
        client = OpenAI(
            api_key=os.environ.get("GROQ_API_KEY", ""),
            base_url="https://api.groq.com/openai/v1",
        )
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=150,
            temperature=0.3,
            messages=[{"role": "user", "content": (
                f'Answer in 2 spoken sentences. No filler words. '
                f'If this requires real-time data you don\'t have, say so directly. '
                f'Question: {query}'
            )}],
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error("LLM fallback error: %s", e)
        return "I couldn't find an answer right now."

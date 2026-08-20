"""Web research commands — the spoken answer to "what's going on with X".

Three tiers, tried in order (config.yaml → web_search):

  1. **live search** — the llm_router "live_search" role (groq/compound-mini)
     runs its own web searches server-side and answers from what it read. This
     is the only tier that can answer "right now" questions truthfully.
  2. **snippets** — Brave (BRAVE_API_KEY) or DuckDuckGo Lite, summarised by the
     router's "research" role. Facts come from the snippets, not the weights.
  3. **model only** — last resort, and the prompt makes it admit that.

Every tier is told today's date. Without it the model answers "current fuel
prices" from its training cutoff, in a confident present tense.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import urllib.parse
import webbrowser

import requests

from nora.command_engine import register
from nora.config import get_config
from nora.conversation import for_speech

logger = logging.getLogger("nora.commands.web_search")

# Words that mean "I want today's answer, not the average answer".
_FRESH_RE = re.compile(
    r"\b(now|today|tonight|current|currently|latest|recent|recently|breaking|"
    r"this (?:week|morning|month)|right now|so far|update[sd]?)\b",
    re.I,
)


def _cfg() -> dict:
    return get_config().get("web_search", {}) or {}


def _normalize_query(query: str) -> str:
    def collapse(m: re.Match) -> str:
        return m.group(0).replace("-", "").lower()
    return re.sub(r"\b[A-Z](?:-[A-Z]){2,}\b", collapse, query)


def _today() -> str:
    return datetime.date.today().strftime("%A, %d %B %Y")


def _is_fresh_query(query: str) -> bool:
    return bool(_FRESH_RE.search(query))


def _spoken(text: str) -> str:
    """Command results skip the conversation path, so shape them for TTS here."""
    return for_speech(text, max_sentences=int(_cfg().get("max_sentences", 3)))


# ── Search backends ────────────────────────────────────────────────────────────

def _brave_search(query: str, count: int = 5) -> list[dict]:
    """Brave Search REST API — returns {title, text, url} dicts."""
    key = os.environ.get("BRAVE_API_KEY", "")
    if not key or "your_brave" in key:
        return []
    params: dict = {"q": query, "count": count}
    if _is_fresh_query(query):
        # Brave's freshness filter. Without it "petrol price today" happily
        # returns a well-ranked page from three years ago.
        params["freshness"] = f"p{max(1, int(_cfg().get('freshness_days', 7)))}d"
    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
            params=params,
            timeout=6,
        )
        resp.raise_for_status()
        results = resp.json().get("web", {}).get("results", [])
        out = []
        for r in results:
            extra = r.get("extra_snippets") or [""]
            desc = r.get("description") or extra[0]
            if not desc:
                continue
            out.append({
                "title": r.get("title", ""),
                "text": re.sub(r"<[^>]+>", "", desc).strip(),
                "url": r.get("url", ""),
                "age": r.get("age", ""),
            })
        return out[:count]
    except Exception as e:
        logger.debug("Brave search error: %s", e)
        return []


def _ddg_search(query: str, count: int = 5) -> list[dict]:
    """DuckDuckGo HTML search — free, no key, scrapes the lite endpoint."""
    try:
        resp = requests.get(
            "https://lite.duckduckgo.com/lite/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (compatible; NORA/1.0)"},
            timeout=6,
        )
        resp.raise_for_status()
        # DDG Lite puts each snippet in <td class="result-snippet">
        snippets = re.findall(
            r'class=["\']result-snippet["\'][^>]*>(.*?)</td>',
            resp.text, re.DOTALL
        )
        out = []
        for s in snippets[:count]:
            s = re.sub(r"<[^>]+>", "", s).strip()
            s = (s.replace("&amp;", "&").replace("&quot;", '"')
                  .replace("&#x27;", "'").replace("&nbsp;", " "))
            if s:
                out.append({"title": "", "text": s, "url": "", "age": ""})
        return out
    except Exception as e:
        logger.debug("DDG search error: %s", e)
        return []


def _fetch_snippets(query: str) -> list[dict]:
    """Try Brave first, fall back to DDG."""
    count = int(_cfg().get("max_snippets", 5))
    return _brave_search(query, count) or _ddg_search(query, count)


def _format_snippets(snippets: list[dict]) -> str:
    lines = []
    for s in snippets:
        head = " — ".join(p for p in (s.get("title"), s.get("age")) if p)
        lines.append(f"- {head + ': ' if head else ''}{s['text']}")
    return "\n".join(lines)


# ── Tier 1: live search (model does its own searching) ─────────────────────────

def _sources_from_executed_tools(extras: dict) -> list[str]:
    """Pull the URLs the search tool actually read out of Groq's executed_tools."""
    urls: list[str] = []
    for tool in extras.get("executed_tools") or []:
        output = tool.get("output") if isinstance(tool, dict) else None
        if isinstance(output, (dict, list)):
            output = json.dumps(output)
        if isinstance(output, str):
            for u in re.findall(r"URL:\s*(\S+)", output):
                if u not in urls:
                    urls.append(u)
    return urls


def _live_search(query: str) -> str:
    """Ask a search-capable model. Returns "" if the role is unavailable."""
    from nora.model_router import complete, AllCandidatesFailed

    extras: dict = {}
    prompt = (
        f"Today is {_today()}. Run at least one web search before you answer — "
        f"your own knowledge is out of date and must not be used on its own. "
        f"Then answer in 2 concise spoken sentences, leading with the answer. "
        f"No filler words ('so', 'well', 'basically'), no markdown — this is "
        f"read aloud. Name a source only when it matters, and name it the way "
        f"a person would say it out loud ('according to the Guardian'). Never "
        f"write a URL, a domain, or a list of links — nobody can click a link "
        f"they are hearing. If the results disagree or are stale, say so in "
        f"the second sentence.\n\nQuestion: {query}"
    )
    try:
        text, candidate = complete(
            "live_search",
            [{"role": "user", "content": prompt}],
            max_tokens=int(_cfg().get("live_tokens", 180)),
            temperature=0.2,
            extras_out=extras,
        )
    except AllCandidatesFailed as e:
        logger.info("live search unavailable (%s) — falling back to snippets", e)
        return ""
    except Exception as e:
        logger.warning("live search error: %s", e)
        return ""

    sources = _sources_from_executed_tools(extras)
    logger.info("live search via %s, %d source(s): %s",
                candidate, len(sources), ", ".join(sources[:3]) or "none reported")
    if not sources:
        # compound answered without searching — that is just its base model
        # talking, which is exactly what tier 3 is for. Let the snippet path try.
        logger.info("live search ran no queries — treating as no live answer")
        return ""
    return _spoken(text)


# ── Tier 2/3: summarise snippets, or answer with nothing ───────────────────────

def _summarise(query: str, snippets: list[dict]) -> str:
    """Turn real search snippets into a 2-sentence spoken answer."""
    from nora.model_router import complete, AllCandidatesFailed

    prompt = (
        f"Today is {_today()}. Answer the question in 2 concise spoken "
        f"sentences using ONLY the provided search results. No filler words "
        f"('so', 'well', 'basically'), no markdown — this is read aloud. "
        f"Name a source only when it matters, and say it as a person would "
        f"('according to the Guardian') — never a URL or domain. "
        f"If the results don't contain a clear answer, say so briefly. "
        f"If they look out of date for the question, say that instead of "
        f"presenting them as current.\n\n"
        f"Question: {query}\n\nSearch results:\n{_format_snippets(snippets)}"
    )
    try:
        text, candidate = complete(
            "research",
            [{"role": "user", "content": prompt}],
            max_tokens=int(_cfg().get("summary_tokens", 400)),
            temperature=0.2,
        )
        logger.info("summarised %d snippet(s) via %s", len(snippets), candidate)
        return _spoken(text)
    except AllCandidatesFailed as e:
        logger.error("summarise failed on every candidate: %s", e)
    except Exception as e:
        logger.error("summarise error: %s", e)
    # Every model is down but the search worked — read the best snippet rather
    # than saying nothing at all.
    best = _spoken(snippets[0]["text"])
    return f"I couldn't summarise that, but the top result says: {best[:280]}"


def _summarise_no_context(query: str) -> str:
    """No search backend reachable — answer from the model, and admit it."""
    from nora.model_router import complete

    try:
        text, _ = complete(
            "research",
            [{"role": "user", "content": (
                f"Today is {_today()}, but you have no web access for this "
                f"answer. Answer in 2 spoken sentences. No filler words, no "
                f"markdown, and never a URL or a domain — this is read aloud. "
                f"If this needs real-time data you don't have, say so directly "
                f"instead of guessing.\n\nQuestion: {query}"
            )}],
            max_tokens=int(_cfg().get("summary_tokens", 400)),
            temperature=0.3,
        )
        return _spoken(text)
    except Exception as e:
        logger.error("LLM fallback error: %s", e)
        return "I couldn't reach the web or a model just now, so I can't answer that."


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
    """Live search → snippet summary → model-only, first tier that answers wins."""
    query = _normalize_query(query)
    logger.info("Researching: %s", query)

    if _cfg().get("live_search", True):
        answer = _live_search(query)
        if answer:
            logger.info("Research response (live): %s…", answer[:80])
            return answer

    snippets = _fetch_snippets(query)
    if not snippets:
        logger.warning("No search results — falling back to model knowledge")
        return _summarise_no_context(query)

    logger.info("Got %d snippet(s) from search", len(snippets))
    response = _summarise(query, snippets)
    logger.info("Research response: %s…", response[:80])
    return response

"""News briefing data — the feed layer behind the "what's going on" takeover.

No API key, on purpose. Publishers put the picture in the feed themselves
(media:thumbnail, media:content, enclosure), so curated RSS gives better
images than a paid search API and cannot expire mid-year. Topics no feed
covers fall through to Google News RSS, which answers any query but strips
thumbnails — those cards go typographic rather than showing a broken box.

The cards this returns are the *only* thing the spoken summary may describe.
One fetch, one truth: if a story is not in this list it is not on screen, and
NORA must not mention it.
"""
from __future__ import annotations

import concurrent.futures as futures
import html
import logging
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests

from nora.config import get_config

logger = logging.getLogger("nora.briefing")

_UA = "Mozilla/5.0 (compatible; NORA/1.0; +personal assistant)"

# RSS and Atom disagree about every tag name, so both spellings are tried.
_NS = {
    "media": "http://search.yahoo.com/mrss/",
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
}

_GOOGLE_NEWS = "https://news.google.com/rss/search"


def _cfg() -> dict:
    return get_config().get("briefing", {}) or {}


def _strip_html(text: str) -> str:
    """Feed summaries are HTML fragments; the orb wants a clean sentence."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(raw: str) -> float:
    """RSS uses RFC-822, Atom uses ISO-8601. Unknown dates sort as 'now' so a
    feed with bad timestamps still shows rather than silently vanishing."""
    if not raw:
        return time.time()
    try:
        return parsedate_to_datetime(raw).timestamp()
    except (TypeError, ValueError):
        pass
    try:
        cleaned = raw.strip().replace("Z", "+00:00")
        import datetime as _dt
        return _dt.datetime.fromisoformat(cleaned).timestamp()
    except ValueError:
        return time.time()


def _find_image(node: ET.Element) -> str:
    """Pull the publisher's own thumbnail. Order matters: media:thumbnail is
    usually already cropped for a card, media:content is often full-bleed."""
    for path, attr in (
        ("media:thumbnail", "url"),
        ("media:content", "url"),
        ("enclosure", "url"),
    ):
        for el in node.findall(path, _NS):
            url = (el.get(attr) or "").strip()
            # media:content carries audio and video too — only take pictures.
            mime = (el.get("type") or "").lower()
            if url and (not mime or mime.startswith("image")):
                return url

    # Some feeds embed the image in the HTML body instead of tagging it.
    for tag in ("description", "summary", "{http://www.w3.org/2005/Atom}content"):
        el = node.find(tag)
        if el is not None and el.text:
            m = re.search(r'<img[^>]+src=["\']([^"\']+)', el.text)
            if m:
                return m.group(1)
    return ""


def _text(node: ET.Element, *tags: str) -> str:
    for tag in tags:
        el = node.find(tag, _NS)
        if el is not None:
            if el.text:
                return el.text.strip()
            # Atom <link href="..."/> keeps the value in an attribute.
            href = el.get("href")
            if href:
                return href.strip()
    return ""


# Hosts whose feed URL says nothing useful about who wrote the story.
_SOURCE_OVERRIDES = {
    "hnrss.org": "Hacker News",
    "news.ycombinator.com": "Hacker News",
    "technologyreview.com": "MIT Tech Review",
    "freecodecamp.org": "freeCodeCamp",
    "thehindu.com": "The Hindu",
    "indianexpress.com": "Indian Express",
    "theguardian.com": "The Guardian",
    "bbci.co.uk": "BBC",
    "caranddriver.com": "Car and Driver",
}


def _source_name(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    host = re.sub(r"^(www|feeds|rss|export|api)\.", "", host)
    if host in _SOURCE_OVERRIDES:
        return _SOURCE_OVERRIDES[host]
    # Strip only the public suffix, not every dotted part — "hnrss.org" must
    # not become "Org".
    stem = re.sub(r"\.(com|org|net|co\.uk|co\.in|in|io|dev|news)$", "", host)
    return stem.split(".")[-1].replace("-", " ").title()


def _parse_feed(xml_bytes: bytes, feed_url: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        logger.debug("briefing: unparseable feed %s (%s)", feed_url, exc)
        return []

    nodes = root.findall(".//item") or root.findall(".//atom:entry", _NS)
    out: list[dict] = []
    for node in nodes:
        title = _strip_html(_text(node, "title", "atom:title"))
        link = _text(node, "link", "atom:link")
        if not title or not link:
            continue
        out.append({
            "title": title,
            "url": link,
            "image": _find_image(node),
            "source": _source_name(feed_url),
            "published": _parse_date(
                _text(node, "pubDate", "atom:updated", "atom:published", "dc:date")
            ),
            "summary": _strip_html(
                _text(node, "description", "atom:summary")
            )[:280],
        })
    return out


def _fetch_one(url: str, timeout: int) -> list[dict]:
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": _UA})
        resp.raise_for_status()
        return _parse_feed(resp.content, url)
    except Exception as exc:                      # network, TLS, HTTP, XML
        # A dead feed must not take the briefing down with it — the merge
        # simply has fewer sources to draw from.
        logger.info("briefing: feed failed %s (%s)", url, exc)
        return []


def _norm_title(title: str) -> str:
    """Wire copy reaches three outlets with slightly different punctuation."""
    return re.sub(r"[^a-z0-9 ]", "", title.lower())[:70]


def _interleave(batches: list[list[dict]], limit: int) -> list[dict]:
    """Round-robin across feeds so one prolific source can't sweep the grid."""
    out: list[dict] = []
    seen: set[str] = set()
    for row in range(max((len(b) for b in batches), default=0)):
        for batch in batches:
            if row >= len(batch):
                continue
            item = batch[row]
            key = _norm_title(item["title"])
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
            if len(out) >= limit:
                return out
    return out


# "What's new in the AI world" is carrier phrasing wrapped around one real
# word. Left in, "world" and "news" match whatever interest claims them and
# an AI question comes back full of politics — which is exactly what it did.
# These are stripped before matching, never matched on.
_CARRIER_RE = re.compile(
    r"\b(what'?s?|whats|new|newest|latest|going|on|in|the|a|an|of|with|"
    r"happening|happened|up|any|some|tell|me|about|show|is|are|there|"
    r"world|news|headlines?|today|now|currently|right|stuff|things?|"
    r"anything|catch|upto|update[sd]?)\b",
    re.I,
)


def _core_topic(topic: str) -> str:
    """Strip the question around the subject: 'what's new in the AI world' → 'ai'."""
    stripped = _CARRIER_RE.sub(" ", topic or "")
    return re.sub(r"[^\w\s]", " ", stripped).strip().lower()


def match_interest(topic: str) -> tuple[str, list[str]]:
    """Map a spoken topic onto a configured interest.

    Returns (label, feeds). An empty topic — or one that was nothing but
    carrier words, like a bare "what's going on in the world" — means
    "everything I care about". An unrecognised subject returns no feeds and
    the caller falls through to Google News, so any genre still answers.
    """
    interests = _cfg().get("interests", {}) or {}
    spoken = (topic or "").strip().lower()
    topic = _core_topic(spoken)

    if not topic:
        feeds: list[str] = []
        for spec in interests.values():
            feeds.extend(spec.get("feeds", []))
        return "your world", feeds

    for name, spec in interests.items():
        words = [w.lower() for w in spec.get("say", [])] + [name.lower()]
        if any(re.search(rf"\b{re.escape(w)}\b", topic) for w in words):
            return name, list(spec.get("feeds", []))
    return topic, []


def _google_news(topic: str, timeout: int) -> list[dict]:
    """Catch-all for topics no curated feed covers. Carries no thumbnails."""
    query = urllib.parse.urlencode({
        "q": f"{topic} when:1d",
        "hl": "en-IN", "gl": "IN", "ceid": "IN:en",
    })
    items = _fetch_one(f"{_GOOGLE_NEWS}?{query}", timeout)
    # Every result would otherwise be sourced to "Google". Google News appends
    # the real publisher to the headline as " - Publisher"; lift it back out so
    # the card credits GovTech rather than the aggregator.
    for item in items:
        title, sep, publisher = item["title"].rpartition(" - ")
        if sep and 0 < len(publisher) <= 40:
            item["title"], item["source"] = title.strip(), publisher.strip()
        else:
            item["source"] = "Google News"
    return items


def collect(topic: str = "") -> dict:
    """Fetch, merge and rank the cards for a briefing.

    Everything returned here is what goes on screen — and therefore the only
    material the spoken summary is allowed to draw from.
    """
    cfg = _cfg()
    limit = int(cfg.get("cards", 6))
    per_feed = int(cfg.get("per_feed", 4))
    timeout = int(cfg.get("timeout", 6))
    fresh_s = float(cfg.get("fresh_hours", 36)) * 3600

    label, feeds = match_interest(topic)
    started = time.time()

    if feeds:
        # Fanning out over ten feeds, the slowest one sets the pace — and this
        # is sitting inside a voice turn. So the briefing spends a fixed budget
        # and ships whatever arrived: a straggler costs its own card, never the
        # whole answer.
        budget = float(cfg.get("budget", 3.5))
        batches = []
        pool = futures.ThreadPoolExecutor(max_workers=len(feeds))
        pending = {pool.submit(_fetch_one, u, timeout): u for u in feeds}
        try:
            for done in futures.as_completed(pending, timeout=budget):
                batches.append(done.result())
        except futures.TimeoutError:
            late = [u for f, u in pending.items() if not f.done()]
            logger.info("briefing: %d feed(s) missed the budget: %s",
                        len(late), ", ".join(_source_name(u) for u in late))
        finally:
            # Not a `with` block: its __exit__ joins every worker, so a feed
            # already blocked on the network would push the turn past the
            # budget we just enforced. Let the stragglers die in the pool.
            pool.shutdown(wait=False, cancel_futures=True)
    else:
        batches = [_google_news(topic, timeout)]

    cutoff = time.time() - fresh_s
    trimmed: list[list[dict]] = []
    for batch in batches:
        fresh = [i for i in batch if i["published"] >= cutoff]
        # A feed that timestamps badly would vanish entirely under the cutoff;
        # keep its newest few rather than lose the source.
        fresh = fresh or batch[:per_feed]
        fresh.sort(key=lambda i: i["published"], reverse=True)
        trimmed.append(fresh[:per_feed])

    cards = _interleave(trimmed, limit)
    cards.sort(key=lambda i: i["published"], reverse=True)

    return {
        "topic": topic,
        "label": label,
        "cards": cards,
        "sourced": sum(1 for b in trimmed if b),
        "feeds": len(feeds) or 1,
        "took_ms": int((time.time() - started) * 1000),
        "generated_at": time.time(),
    }

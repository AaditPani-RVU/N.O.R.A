"""The user's own words, for every recogniser NORA listens through (NORA_ASSIST_PLAN A).

Both recognisers hear the user's vocabulary wrong in the same way: "Deptones",
"deaf tools", "pink fluid", "the state of vermouth" for the Strait of Hormuz.
The phone already biased Android's recogniser with the Spotify artists (Sharp D);
the laptop's Whisper had no hints at all. This module is the one list both get:

  - `words()` — names worth hearing right, most specific first: words the user
    taught, routine names, study subjects, watched stories, saved places, people
    NORA has called, then the artists.
  - `whisper_prompt()` — those words as Whisper's `prompt` / `initial_prompt`.
    Whisper reads it as text that came before the clip, so the spellings in it
    win when the audio is ambiguous. Kept short: the remote endpoint caps the
    prompt at 224 tokens, and a long one starts leaking into the transcript.
  - `biasing()` — the same words for the phone (EXTRA_BIASING_STRINGS).
  - `repair(text)` — rewrites a mishearing the user corrected once, everywhere
    after: "you misheard, I said Hormuz, not vermouth" → every later "vermouth"
    (as a whole word) becomes "Hormuz", on every channel, before routing.

Taught words live in the store (`vocabulary`, migration 9). Everything else is
read from where it already lives, so nothing here goes stale.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

from nora import store
from nora.config import get_config

logger = logging.getLogger("nora.vocabulary")

_ROOT = Path(__file__).resolve().parent.parent

# Whisper's prompt budget is 224 tokens; names average ~3 tokens with the
# separator, so ~60 names is safely inside it.
PROMPT_WORDS = 60
BIASING_WORDS = 150

_lock = threading.Lock()
_cache: tuple[float, list[str]] | None = None
_CACHE_SEC = 300.0
_fixes: tuple[float, list[tuple[re.Pattern, str]]] | None = None


def _cfg() -> dict:
    return get_config().get("vocabulary", {}) or {}


def _clean(word: str) -> str:
    return re.sub(r"\s+", " ", str(word or "")).strip(" .,!?\"'")


# ── sources ──────────────────────────────────────────────────────────────────

def taught() -> list[str]:
    try:
        rows = store.query("SELECT DISTINCT word FROM vocabulary ORDER BY added_at DESC")
    except Exception as e:
        logger.debug("vocabulary: store unreadable: %s", e)
        return []
    return [r["word"] for r in rows]


def _places() -> list[str]:
    try:
        rows = store.query("SELECT name FROM places")
    except Exception:
        return []
    return [r["name"] for r in rows if r["name"] not in ("home", "work")]


def _called() -> list[str]:
    """People the user asked NORA to call or message, from the audit log."""
    from nora import audit_log
    names: dict[str, int] = {}
    try:
        lines = audit_log._LOG_PATH.read_text(encoding="utf-8").splitlines()[-2000:]
    except Exception:
        return []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not str(entry.get("action", "")).startswith(("phone.call_contact", "phone.send_message",
                                                         "phone.text", "phone.whatsapp")):
            continue
        params = entry.get("params") or {}
        if "no contact" in str(entry.get("result", "")).lower():
            continue
        who = _clean(params.get("contact") or params.get("name") or params.get("to") or "")
        if who and not any(c.isdigit() for c in who):
            names[who] = names.get(who, 0) + 1
    return sorted(names, key=lambda n: -names[n])


def _from_modules() -> list[str]:
    """Names the other assistant features keep: routines, study subjects, stories."""
    out: list[str] = []
    for mod, fn in (("routines", "vocabulary"), ("study", "vocabulary"),
                    ("story_watch", "vocabulary")):
        try:
            module = __import__(f"nora.{mod}", fromlist=[fn])
            out.extend(getattr(module, fn)())
        except Exception as e:
            logger.debug("vocabulary: %s skipped (%s)", mod, e)
    return out


def _artists() -> list[str]:
    try:
        from nora import music_names
        return music_names.artists()
    except Exception:
        return []


def words() -> list[str]:
    """Every name worth hearing right, most specific first, without repeats."""
    global _cache
    with _lock:
        if _cache is not None and time.time() - _cache[0] < _CACHE_SEC:
            return list(_cache[1])
    extra = [_clean(w) for w in (_cfg().get("words") or [])]
    seen: set[str] = set()
    out: list[str] = []
    for w in [*extra, *taught(), *_from_modules(), *_places(), *_called(), *_artists()]:
        w = _clean(w)
        if len(w) < 2 or w.casefold() in seen:
            continue
        seen.add(w.casefold())
        out.append(w)
    with _lock:
        _cache = (time.time(), out)
    return list(out)


def whisper_prompt() -> str:
    """A prompt for Whisper that carries the user's spellings, or ''."""
    if not _cfg().get("whisper_prompt", True):
        return ""
    names = words()[:PROMPT_WORDS]
    return ", ".join(names) + "." if names else ""


def biasing(limit: int = BIASING_WORDS) -> list[str]:
    """For the phone's recogniser."""
    return words()[:limit]


# ── taught corrections ───────────────────────────────────────────────────────

def _invalidate() -> None:
    global _cache, _fixes
    with _lock:
        _cache = None
        _fixes = None


def learn(word: str, heard: str = "") -> str:
    """Remember `word`, and that it was heard as `heard`. Returns what to say."""
    word, heard = _clean(word), _clean(heard).lower()
    if not word:
        return "Which word should I learn?"
    if heard and heard == word.lower():
        heard = ""
    with store.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO vocabulary (word, heard, added_at) VALUES (?, ?, ?)",
                     (word, heard, time.time()))
    _invalidate()
    if heard:
        return f"Got it. When I hear \"{heard}\" I'll take it as {word}."
    return f"Got it. I'll listen for {word}."


def forget(word: str) -> str:
    word = _clean(word)
    with store.transaction() as conn:
        n = conn.execute("DELETE FROM vocabulary WHERE lower(word) = lower(?) OR heard = lower(?)",
                         (word, word)).rowcount
    _invalidate()
    return f"Forgot {word}." if n else f"I hadn't learned {word}."


def _fix_patterns() -> list[tuple[re.Pattern, str]]:
    global _fixes
    with _lock:
        if _fixes is not None and time.time() - _fixes[0] < _CACHE_SEC:
            return _fixes[1]
    try:
        rows = store.query("SELECT word, heard FROM vocabulary WHERE heard != ''")
    except Exception:
        rows = []
    # Longest first, so "deaf tools" is replaced before a fix for "deaf" could be.
    pats = [(re.compile(rf"(?<!\w){re.escape(r['heard'])}(?!\w)", re.I), r["word"])
            for r in sorted(rows, key=lambda r: -len(r["heard"]))]
    with _lock:
        _fixes = (time.time(), pats)
    return pats


def repair(text: str) -> str:
    """`text` with every taught mishearing replaced. Unchanged when none match."""
    if not text:
        return text
    out = text
    for pat, word in _fix_patterns():
        out = pat.sub(word, out)
    if out != text:
        logger.info("vocabulary: heard %r, taken as %r", text, out)
    return out


def reset_for_tests() -> None:
    _invalidate()

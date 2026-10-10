"""Names the recogniser mishears, repaired against the user's own music (Sharp D).

"Play risk by deaf tools" reached Spotify's search as "deaf tools", which found
an artist called DEAF. Spotify's own fuzzy search is no help: "darkpunk" finds
an artist called DarKPunK and "deptones" finds Nirvana. But the user nearly
always asks for something they already listen to, so the names to match
against are theirs: the artists and tracks in their playlists (and followed,
top, recent and saved, when the Spotify login granted those), a few hundred
names rather than the whole catalogue.

A name heard is compared with each of those by spelling and by sound (a crude
phonetic key: "deaf tools" → "taftals", "Deftones" → "taftanas"), the two
scores averaged. It is replaced only when the best match is clearly ahead
of the rest; a close-but-unsure repair is played and said out loud ("Taking
'deaf tools' as Deftones"), since asking first would cost a turn and a wrong
song is undone by "stop". Names nobody listens to are left alone: "stomay" for
Stromae stays as heard until Stromae is in the library.

The names are cached in nora_music_names.json and rebuilt daily by the
prefetch thread; with no cache (or Spotify not linked) nothing is repaired.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from rapidfuzz import fuzz

logger = logging.getLogger("nora.music_names")

_ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = _ROOT / "nora_music_names.json"
MAX_AGE_SEC = 24 * 3600

# Measured on the user's 325 artists: real mishearings scored 75-94 against
# the intended artist, while 30 popular names *not* in the library scored at
# most 71 against anything in it. A track title is a shorter, commoner string
# ("Risk" scores 75 against "RISE"), so it needs more.
ARTIST_MIN = 74
TRACK_MIN = 88
SURE = 88             # at or above this, repair without saying so
LEAD = 10             # the best must beat the runner-up by this much
MIN_LEN = 5           # letters; "queen" is the shortest name worth repairing

_lock = threading.Lock()
_names: dict[str, Any] = {}
_names_key: tuple[Path, float] | None = None
# The eval set repairs against a frozen copy of the names, so a playlist
# changed today can't move yesterday's score (nora.evals.NAMES_PATH).
_path_override: Path | None = None


# ── matching ──────────────────────────────────────────────────────────────────

def _flat(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


# Sounds the recogniser trades for each other, collapsed to one letter.
_SOUNDS = (("ph", "f"), ("gh", "g"), ("ck", "k"), ("ch", "k"), ("sh", "s"), ("th", "t"),
           ("dt", "t"), ("q", "k"), ("c", "k"), ("z", "s"), ("x", "ks"), ("v", "f"),
           ("p", "f"), ("b", "f"), ("d", "t"), ("g", "k"), ("w", ""))


def sound_key(text: str) -> str:
    """'deaf tools' → 'taftals', 'Deftones' → 'taftanas'."""
    t = _flat(text)
    for a, b in _SOUNDS:
        t = t.replace(a, b)
    t = t[:1] + re.sub(r"[aeiouy]+", "a", t[1:])
    return re.sub(r"(.)\1+", r"\1", t)


def similarity(heard: str, name: str) -> float:
    return (fuzz.ratio(_flat(heard), _flat(name)) + fuzz.ratio(sound_key(heard), sound_key(name))) / 2


def best_match(heard: str, names: list[str], minimum: float) -> tuple[str, float] | None:
    """(name, score) when one name is a clear match for what was heard, else None.
    An exact match (ignoring case and spacing) is returned with 100."""
    flat = _flat(heard)
    if len(flat) < MIN_LEN or not names:
        return None
    for n in names:
        if _flat(n) == flat:
            return n, 100.0
    scored = sorted(((similarity(heard, n), n) for n in names), reverse=True)[:2]
    top, name = scored[0]
    runner = scored[1][0] if len(scored) > 1 else 0.0
    if top >= minimum and top - runner >= LEAD:
        return name, top
    return None


# ── the names ─────────────────────────────────────────────────────────────────

def _load() -> dict[str, Any]:
    global _names, _names_key
    path = _path_override or CACHE_PATH
    try:
        key = (path, path.stat().st_mtime)
    except OSError:
        return {}
    with _lock:
        if key != _names_key:
            try:
                _names = json.loads(path.read_text())
            except (OSError, ValueError):
                _names = {}
            _names_key = key
        return _names


@contextmanager
def frozen(path: Path) -> Iterator[None]:
    """Repair against the names in `path` (missing: repair nothing)."""
    global _path_override
    before, _path_override = _path_override, path
    try:
        yield
    finally:
        _path_override = before


def artists() -> list[str]:
    """Artist names, most listened-to first."""
    counts = _load().get("artists") or {}
    return sorted(counts, key=lambda n: -counts[n])


def tracks() -> dict[str, list[str]]:
    """{track title: its artists}."""
    return _load().get("tracks") or {}


def refresh() -> int:
    """Rebuild the cache from Spotify. Returns how many artists it holds."""
    from nora import spotify_user
    items = spotify_user.library()
    if not items:
        return 0
    counts: dict[str, int] = {}
    titles: dict[str, list[str]] = {}
    for it in items:
        for a in it["artists"]:
            counts[a] = counts.get(a, 0) + 1
        if it["track"]:
            titles.setdefault(it["track"], it["artists"])
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"built": time.time(), "artists": counts, "tracks": titles}))
    tmp.replace(CACHE_PATH)
    logger.info("music names: %d artists, %d tracks", len(counts), len(titles))
    return len(counts)


def prefetch() -> None:
    """Daily, from the prefetch thread: rebuild when the cache is a day old."""
    from nora import spotify_user
    try:
        age = time.time() - CACHE_PATH.stat().st_mtime
    except OSError:
        age = float("inf")
    if age >= MAX_AGE_SEC and spotify_user.is_logged_in():
        refresh()


# ── repair ────────────────────────────────────────────────────────────────────

# Said around a name but not part of it: "some pink floyd", "deftones songs".
_LEAD_FILLER = re.compile(r"^(?:some|a\s+(?:bit|little)\s+of|a\s+few|any)\s+", re.I)
_TAIL_FILLER = re.compile(r"\s+(?:songs?|music|tracks?|stuff)$", re.I)


def _artist(heard: str) -> tuple[str, float] | None:
    core = _TAIL_FILLER.sub("", _LEAD_FILLER.sub("", heard.strip()))
    hit = best_match(core, artists(), ARTIST_MIN)
    return hit if hit and hit[0].casefold() != core.casefold() else None


def _track(heard: str, by: str = "") -> tuple[str, float] | None:
    titles = tracks()
    if by:
        titles = {t: a for t, a in titles.items() if any(_flat(x) == _flat(by) for x in a)} or titles
    hit = best_match(heard, list(titles), TRACK_MIN)
    return hit if hit and hit[0].casefold() != heard.casefold() else None


class Repair:
    """What was changed: [(heard, name, score)]."""

    def __init__(self) -> None:
        self.fixes: list[tuple[str, str, float]] = []

    def apply(self, heard: str, hit: tuple[str, float] | None) -> str:
        if hit is None:
            return heard
        self.fixes.append((heard, hit[0], hit[1]))
        return hit[0]

    @property
    def note(self) -> str:
        """What to say before the result: only the repairs NORA wasn't sure of."""
        unsure = [f"'{h}' as {n}" for h, n, s in self.fixes if s < SURE]
        return f"Taking {' and '.join(unsure)}." if unsure else ""


_BY = re.compile(r"^(?P<t>.+?)\s+by\s+(?P<a>.+)$", re.I)


def repair(action: str, params: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """(params with misheard names replaced, a note to say or ""). Params of
    any other action, and names already right, come back unchanged."""
    if action not in _FIELDS or not _load():
        return params, ""
    try:
        return _FIELDS[action](dict(params))
    except Exception as exc:                       # a repair must never cost the request
        logger.warning("name repair failed for %s: %s", action, exc)
        return params, ""


def _phone(p: dict[str, Any]) -> tuple[dict[str, Any], str]:
    q = str(p.get("query") or "")
    if not q or p.get("kind") == "playlist":
        return p, ""
    r = Repair()
    m = _BY.match(q)
    if m:
        artist = r.apply(m.group("a"), _artist(m.group("a")))
        title = r.apply(m.group("t"), _track(m.group("t"), artist))
        p["query"] = f"{title} by {artist}"
    elif p.get("kind") in ("any", "artist", None):
        hit = _artist(q)
        p["query"] = r.apply(q, hit) if hit or p.get("kind") == "artist" else r.apply(q, _track(q))
    else:
        p["query"] = r.apply(q, _track(q))
    return p, r.note


def _track_artist(p: dict[str, Any], track_key: str = "track") -> tuple[dict[str, Any], str]:
    r = Repair()
    if p.get("artist"):
        p["artist"] = r.apply(p["artist"], _artist(p["artist"]))
    t = str(p.get(track_key) or "")
    if t:
        m = _BY.match(t) if not p.get("artist") else None
        if m:
            p["artist"] = r.apply(m.group("a"), _artist(m.group("a")))
            p[track_key] = r.apply(m.group("t"), _track(m.group("t"), p["artist"]))
        else:
            # "play pink fluid" names an artist as often as a song.
            hit = _track(t, p.get("artist", "")) or (None if p.get("artist") else _artist(t))
            p[track_key] = r.apply(t, hit)
    return p, r.note


def _artist_only(p: dict[str, Any]) -> tuple[dict[str, Any], str]:
    r = Repair()
    if p.get("artist"):
        p["artist"] = r.apply(p["artist"], _artist(p["artist"]))
    return p, r.note


_FIELDS = {
    "play_on_phone": _phone,
    "play_music": _track_artist,
    "spotify_play_song": lambda p: _track_artist(p, "song"),
    "spotify_play_artist": _artist_only,
    "spotify_play_album": _artist_only,
}


def biasing(limit: int = 100) -> list[str]:
    """The names most worth hearing right, for the phone's recogniser."""
    return artists()[:limit]

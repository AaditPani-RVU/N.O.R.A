"""Music on the phone: "play my workout playlist", "shuffle my downloads".

Spotify on Android turned play-from-search into "open the search page": it
never started anything, and it only searched the public catalogue. So the core
works out *what* to play — the user's own playlist through `nora.spotify_user`,
else a track, artist or album through the app-token search.

Then it plays it through Spotify Connect when the phone's Spotify is running:
Spotify's media session on the phone ignored play-from-URI sent by NORA's app.
When the phone isn't a Connect device, the phone gets the `spotify:` URI and
opens it (or leaves a tap-to-open notification when NORA isn't on screen).
"""
from __future__ import annotations

import asyncio
import logging
import re

from nora import music_names, spotify_api, spotify_user
from nora.command_engine import register
from nora.schemas import StepResult

logger = logging.getLogger("nora.commands.phone_music")

_KINDS = ("any", "track", "artist", "album", "playlist")
_OFFLINE = "offline backup"


async def _phone(params: dict) -> StepResult:
    from nora.hub import server
    return await server.call_capability("phone.play_media", params)


def _own(query: str, strict: bool = False) -> tuple[dict | None, bool]:
    """(the user's playlist called `query`, whether Spotify is linked)."""
    try:
        return spotify_user.find_playlist(query, strict=strict), True
    except spotify_user.NotLoggedIn:
        return None, False
    except Exception as exc:                        # network, 5xx: fall back to search
        logger.warning("playlist lookup failed: %s", exc)
        return None, True


def _by(query: str) -> tuple[str, str] | None:
    """"risk by deftones" → ("risk", "deftones")."""
    m = re.match(r"^(?P<t>.+?)\s+by\s+(?P<a>.+)$", query, re.I)
    return (m.group("t"), m.group("a")) if m else None


def _resolve(query: str, kind: str) -> tuple[str | None, str, str]:
    """(uri, what to call it, a note for the user). Blocking: run off the loop."""
    note = ""
    by = _by(query) if kind in ("any", "track") else None
    if kind == "playlist" or (kind == "any" and by is None):
        # A loose near-miss is fine when they said "playlist"; for a bare
        # "play risk" only a clear match beats the catalogue.
        hit, linked = _own(query, strict=(kind == "any"))
        if hit is not None:
            return hit["uri"], f"your {hit['name']} playlist", ""
        if spotify_user.canonical(query) == _OFFLINE:
            return None, "Offline Backup", (
                "I can't see your Offline Backup playlist. In Spotify, add it to your "
                "library, and I'll find it." if linked else
                "Spotify isn't linked on the core, so I can't reach your Offline Backup.")
        if kind == "playlist":
            note = (f"You don't have a playlist called {query}, so here's a public one."
                    if linked else
                    "Spotify isn't linked on the core, so I can only find public playlists.")
            pub = spotify_api.find_playlist(query)
            if pub:
                return pub["uri"], f"the {pub['name']} playlist", note
            return None, query, note
    if kind == "artist":
        hit = spotify_api.find_artist(query)
        if hit:
            return hit["uri"], hit["name"], ""
    elif kind == "album":
        hit = spotify_api.find_album(query)
        if hit:
            return hit["uri"], hit["name"], ""
    else:
        if by is not None:
            hit = spotify_api.find_track(*by)
        else:
            if kind == "any":
                # "play some pink floyd": the name is an artist's, so play the artist.
                artist = spotify_api.find_artist(query)
                if artist and artist["name"].casefold() == query.casefold():
                    return artist["uri"], artist["name"], ""
            hit = spotify_api.find_track(query)
        if hit:
            return hit["uri"], f"{hit['title']} by {hit['artist']}", ""
    return None, query, note


def _via_connect(uri: str, label: str, shuffle: bool) -> tuple[bool, str] | None:
    """Play through Spotify Connect: (ok, what to say), or None when Connect
    can't be used here (not linked for playback, or the phone's Spotify isn't
    running) and the phone should try itself. Blocking."""
    try:
        if not spotify_user.can_play():
            return None
        device = spotify_user.phone_device()
        if device is None:
            return None
        now = spotify_user.play_on_device(uri, device["id"], shuffle)
    except spotify_user.NotLoggedIn:
        return None
    except spotify_user.PlaybackError as exc:
        logger.warning("Connect play of %s failed: %s", uri, exc)
        return False, str(exc)
    how = "Shuffling" if shuffle else "Playing"
    if now.casefold() == label.casefold():
        return True, f"{how} {label} on your phone."
    return True, f"{how} {label} on your phone, starting with {now}."


@register(
    "play_on_phone",
    sig="play_on_phone(query: str, kind: str = 'any', shuffle: bool = False)",
    category="device",
    description="play music on the user's phone in Spotify: their own playlists by name "
                "('my workout playlist' → query='workout', kind='playlist'), or a song, "
                "artist or album. kind: any|track|artist|album|playlist. "
                "'my downloads' / 'offline songs' is the Offline Backup playlist",
)
async def play_on_phone(query: str, kind: str = "any", shuffle: bool = False) -> StepResult:
    query = (query or "").strip()[:100]
    kind = kind if kind in _KINDS else "any"
    if not query:
        return StepResult(action="play_on_phone", success=False, message="Play what?")
    fixed, heard = music_names.repair("play_on_phone", {"query": query, "kind": kind})
    query = fixed["query"]

    loop = asyncio.get_running_loop()
    uri, label, note = await loop.run_in_executor(None, _resolve, query, kind)
    if uri is None and spotify_user.canonical(query) == _OFFLINE:
        return StepResult(action="play_on_phone", success=False, message=note)
    note = " ".join(p for p in (heard, note) if p)

    if uri:
        played = await loop.run_in_executor(None, _via_connect, uri, label, shuffle)
        if played is not None:
            ok, said = played
            return StepResult(action="play_on_phone", success=ok,
                              message=" ".join(p for p in (note, said) if p))

    params: dict = {"query": query, "kind": kind, "app": "spotify", "label": label[:100]}
    if uri:
        params["uri"] = uri
    if shuffle:
        params["shuffle"] = True
    res = await _phone(params)
    trace = (res.data or {}).get("trace") if isinstance(res.data, dict) else None
    if trace:
        # What Spotify's session did, poll by poll: the phone's side of a
        # "played the wrong song" report.
        logger.info("play_media %s -> %s: %s", uri or query, "ok" if res.success else "failed",
                    "; ".join(trace.get("steps") or []))
    message = " ".join(p for p in (note, res.message) if p)
    return StepResult(action="play_on_phone", success=res.success, withheld=res.withheld,
                      error_code=res.error_code, data=res.data, message=message)

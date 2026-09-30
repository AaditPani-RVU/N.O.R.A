"""Music on the phone: "play my workout playlist", "shuffle my downloads".

Spotify on Android turned play-from-search into "open the search page": it
never started anything, and it only searched the public catalogue. So the core
works out *what* to play — the user's own playlist through `nora.spotify_user`,
else a track, artist or album through the app-token search — and hands the
phone a `spotify:` URI, which it plays through Spotify's media session.
"""
from __future__ import annotations

import asyncio
import logging

from nora import spotify_api, spotify_user
from nora.command_engine import register
from nora.schemas import StepResult

logger = logging.getLogger("nora.commands.phone_music")

_KINDS = ("any", "track", "artist", "album", "playlist")
_OFFLINE = "offline backup"


async def _phone(params: dict) -> StepResult:
    from nora.hub import server
    return await server.call_capability("phone.play_media", params)


def _own(query: str) -> tuple[dict | None, bool]:
    """(the user's playlist called `query`, whether Spotify is linked)."""
    try:
        return spotify_user.find_playlist(query), True
    except spotify_user.NotLoggedIn:
        return None, False
    except Exception as exc:                        # network, 5xx: fall back to search
        logger.warning("playlist lookup failed: %s", exc)
        return None, True


def _resolve(query: str, kind: str) -> tuple[str | None, str, str]:
    """(uri, what to call it, a note for the user). Blocking: run off the loop."""
    note = ""
    if kind in ("playlist", "any"):
        hit, linked = _own(query)
        if hit is not None:
            return hit["uri"], f"your {hit['name']} playlist", ""
        if spotify_user.canonical(query) == _OFFLINE:
            return None, "Offline Backup", (
                "I can't see your Offline Backup playlist from here." if linked else
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
        hit = spotify_api.find_track(query)
        if hit:
            return hit["uri"], f"{hit['title']} by {hit['artist']}", ""
    return None, query, note


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

    loop = asyncio.get_running_loop()
    uri, label, note = await loop.run_in_executor(None, _resolve, query, kind)
    if uri is None and spotify_user.canonical(query) == _OFFLINE:
        return StepResult(action="play_on_phone", success=False, message=note)

    params: dict = {"query": query, "kind": kind, "app": "spotify", "label": label[:100]}
    if uri:
        params["uri"] = uri
    if shuffle:
        params["shuffle"] = True
    res = await _phone(params)
    message = " ".join(p for p in (note, res.message) if p)
    return StepResult(action="play_on_phone", success=res.success, withheld=res.withheld,
                      error_code=res.error_code, data=res.data, message=message)

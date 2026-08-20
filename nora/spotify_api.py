"""Spotify Web API client — client-credentials flow, search only.

NORA uses the Web API for exactly one thing: turning words into Spotify URIs.
"play blinding lights" becomes spotify:track:0VjIjW4GlUZAMYd2vXMi3b, which is
then handed to the desktop client over MPRIS (see nora/platform/linux/mpris.py).

That split is deliberate:
  - Search runs on the *client-credentials* grant: an app-level token from a
    Client ID + Secret. No browser redirect, no user login, no refresh tokens.
  - Playback happens locally over D-Bus, so it does NOT need Spotify Premium
    the way the Web API's /me/player endpoints do.

Configure with SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET in .env (or the
``spotify:`` block in config.yaml). Without credentials every lookup returns
None and the command layer falls back to opening Spotify's search page.

Create an app at https://developer.spotify.com/dashboard — no redirect URI is
needed for this grant.
"""
from __future__ import annotations

import base64
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger("nora.spotify_api")

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"

_HTTP_TIMEOUT = 8.0
_TOKEN_SKEW = 30.0        # refresh this many seconds before real expiry

_lock = threading.RLock()
_token: str = ""
_token_expiry: float = 0.0


class SpotifyAuthError(RuntimeError):
    """Credentials are missing or rejected by Spotify."""


# ── Configuration ─────────────────────────────────────────────────────────────

def _cfg() -> dict[str, Any]:
    try:
        from nora.config import get_config
        return get_config().get("spotify", {}) or {}
    except Exception:
        return {}


def credentials() -> tuple[str, str]:
    """Return (client_id, client_secret) from env, falling back to config.yaml."""
    cfg = _cfg()
    client_id = (
        os.environ.get("SPOTIFY_CLIENT_ID")
        or str(cfg.get("client_id") or "")
    ).strip()
    client_secret = (
        os.environ.get("SPOTIFY_CLIENT_SECRET")
        or str(cfg.get("client_secret") or "")
    ).strip()
    return client_id, client_secret


def is_configured() -> bool:
    """True when both a client id and secret are present."""
    cid, secret = credentials()
    return bool(cid and secret)


def market() -> str:
    """ISO country code used to relink tracks to a playable version."""
    return str(_cfg().get("market") or "US").upper()


# ── Token handling ────────────────────────────────────────────────────────────

def _access_token(force: bool = False) -> str:
    """Return a cached app access token, fetching a new one when stale."""
    global _token, _token_expiry
    with _lock:
        if not force and _token and time.time() < _token_expiry:
            return _token

        cid, secret = credentials()
        if not (cid and secret):
            raise SpotifyAuthError(
                "Spotify credentials are not set. Add SPOTIFY_CLIENT_ID and "
                "SPOTIFY_CLIENT_SECRET to .env."
            )

        import requests

        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        try:
            resp = requests.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=_HTTP_TIMEOUT,
            )
        except Exception as exc:
            raise SpotifyAuthError(f"Could not reach Spotify accounts: {exc}") from exc

        if resp.status_code != 200:
            raise SpotifyAuthError(
                f"Spotify rejected the credentials (HTTP {resp.status_code}). "
                "Check SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET."
            )

        payload = resp.json()
        _token = payload.get("access_token", "")
        _token_expiry = time.time() + float(payload.get("expires_in", 3600)) - _TOKEN_SKEW
        if not _token:
            raise SpotifyAuthError("Spotify returned no access token.")
        logger.info("Spotify app token acquired (valid ~%.0f min)",
                    (_token_expiry - time.time()) / 60)
        return _token


def reset_token() -> None:
    """Drop the cached token. Used by tests and after a credentials change."""
    global _token, _token_expiry
    with _lock:
        _token = ""
        _token_expiry = 0.0


# ── Raw request ───────────────────────────────────────────────────────────────

def _get(path: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """GET an API path. Returns the decoded body, or None on failure."""
    import requests

    for attempt in (1, 2):
        try:
            token = _access_token(force=(attempt == 2))
        except SpotifyAuthError as exc:
            logger.warning("Spotify auth failed: %s", exc)
            return None

        try:
            resp = requests.get(
                f"{API_BASE}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=_HTTP_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("Spotify request failed: %s", exc)
            return None

        if resp.status_code == 401 and attempt == 1:
            continue                      # token expired early — force a refresh
        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", 1))
            logger.warning("Spotify rate-limited; retry after %.0fs", wait)
            return None
        if resp.status_code != 200:
            logger.warning("Spotify API %s returned HTTP %s", path, resp.status_code)
            return None

        try:
            return resp.json()
        except Exception:
            return None
    return None


# ── Search ────────────────────────────────────────────────────────────────────

def search(query: str, kind: str = "track", limit: int = 5) -> list[dict[str, Any]]:
    """Raw search. *kind* is track | artist | album | playlist."""
    if not query.strip():
        return []
    body = _get("/search", {
        "q": query.strip(),
        "type": kind,
        "limit": max(1, min(limit, 50)),
        "market": market(),
    })
    if not body:
        return []
    items = (body.get(f"{kind}s") or {}).get("items") or []
    # Spotify occasionally returns null entries in playlist results.
    return [i for i in items if isinstance(i, dict)]


def _artist_names(item: dict[str, Any]) -> str:
    return ", ".join(
        str(a.get("name", "")) for a in (item.get("artists") or []) if isinstance(a, dict)
    )


def find_track(track: str, artist: str = "") -> dict[str, Any] | None:
    """Resolve a song name (optionally + artist) to a playable track.

    Returns {uri, title, artist, album} or None. Tries the precise
    field-qualified query first, then falls back to a loose one, because
    ``track:"x" artist:"y"`` misses when the user's phrasing is slightly off.
    """
    track = track.strip()
    if not track:
        return None

    queries: list[str] = []
    if artist.strip():
        queries.append(f'track:"{track}" artist:"{artist.strip()}"')
        queries.append(f"{track} {artist.strip()}")
    else:
        queries.append(f'track:"{track}"')
        queries.append(track)

    for query in queries:
        for item in search(query, "track", limit=5):
            uri = item.get("uri")
            if not uri:
                continue
            return {
                "uri": uri,
                "title": str(item.get("name", "")),
                "artist": _artist_names(item),
                "album": str((item.get("album") or {}).get("name", "")),
            }
    return None


def find_artist(artist: str) -> dict[str, Any] | None:
    """Resolve an artist name to {uri, name}. Playing this URI as a context
    starts that artist's popular tracks."""
    artist = artist.strip()
    if not artist:
        return None
    for item in search(f'artist:"{artist}"', "artist", limit=5) or search(artist, "artist", limit=5):
        uri = item.get("uri")
        if uri:
            return {"uri": uri, "name": str(item.get("name", ""))}
    return None


def find_album(album: str, artist: str = "") -> dict[str, Any] | None:
    """Resolve an album name to {uri, name, artist}."""
    album = album.strip()
    if not album:
        return None
    query = f'album:"{album}"'
    if artist.strip():
        query += f' artist:"{artist.strip()}"'
    for item in search(query, "album", limit=5) or search(album, "album", limit=5):
        uri = item.get("uri")
        if uri:
            return {
                "uri": uri,
                "name": str(item.get("name", "")),
                "artist": _artist_names(item),
            }
    return None


def find_playlist(name: str) -> dict[str, Any] | None:
    """Resolve a public playlist name to {uri, name, owner}.

    Only public/editorial playlists are visible to an app token — the user's
    own private playlists would require the user-authorised OAuth flow.
    """
    name = name.strip()
    if not name:
        return None
    for item in search(name, "playlist", limit=5):
        uri = item.get("uri")
        if uri:
            return {
                "uri": uri,
                "name": str(item.get("name", "")),
                "owner": str((item.get("owner") or {}).get("display_name", "")),
            }
    return None

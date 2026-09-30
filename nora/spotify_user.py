"""The user's own Spotify library: their playlists, read-only.

`nora.spotify_api` runs on an app token and sees only the public catalogue, so
"play my workout playlist" found strangers' playlists called "Workout". This
module holds a token for the user themself, minted once by a browser login,
and uses it for one thing: listing their playlists so a name said out loud
becomes the right `spotify:playlist:…` URI, and to start it on the phone
through Spotify Connect (see "Playback on the phone" below).

Setup, once:
  1. In the Spotify developer dashboard, add the redirect URI
     http://127.0.0.1:8888/callback to the app whose SPOTIFY_CLIENT_ID is in .env.
  2. `python -m nora.spotify_user login` on the core, and approve in the browser.

The login is Authorization Code with PKCE, scopes playlist-read-private,
playlist-read-collaborative, user-read-playback-state and
user-modify-playback-state. Since March 2026 Spotify only serves
development-mode apps whose owner has Premium; the user's account does.
The token lands in spotify_user_token.json (0600, gitignored) and refreshes
itself; `python -m nora.spotify_user logout` deletes it.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, parse_qs

from nora import spotify_api

logger = logging.getLogger("nora.spotify_user")

_ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATH = Path(os.environ.get("NORA_SPOTIFY_TOKEN_PATH", _ROOT / "spotify_user_token.json"))

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
SCOPES = ("playlist-read-private playlist-read-collaborative "
          "user-read-playback-state user-modify-playback-state")
_PLAYBACK_SCOPE = "user-modify-playback-state"
DEFAULT_REDIRECT = "http://127.0.0.1:8888/callback"

_HTTP_TIMEOUT = 8.0
_TOKEN_SKEW = 60.0
_PLAYLIST_TTL = 600.0     # a new playlist shows up within ten minutes

_lock = threading.RLock()
_playlists: list[dict[str, Any]] = []
_playlists_at = 0.0

# Spotify fills "Offline Backup" with the songs downloaded on the phone. Nobody
# calls it by that name out loud.
_ALIASES = {
    "downloads": "offline backup",
    "downloaded": "offline backup",
    "downloaded songs": "offline backup",
    "downloaded music": "offline backup",
    "download": "offline backup",
    "offline": "offline backup",
    "offline songs": "offline backup",
    "offline music": "offline backup",
}


class NotLoggedIn(RuntimeError):
    """No user token yet: run `python -m nora.spotify_user login`."""


def redirect_uri() -> str:
    return str(spotify_api._cfg().get("redirect_uri") or DEFAULT_REDIRECT)


# ── Token storage ─────────────────────────────────────────────────────────────

def _load() -> dict[str, Any]:
    try:
        return json.loads(TOKEN_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save(tok: dict[str, Any]) -> None:
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tok, f)


def is_logged_in() -> bool:
    return bool(_load().get("refresh_token"))


def _token_request(data: dict[str, str]) -> dict[str, Any]:
    import requests
    cid, _ = spotify_api.credentials()
    if not cid:
        raise NotLoggedIn("SPOTIFY_CLIENT_ID is not set.")
    resp = requests.post(spotify_api.TOKEN_URL, data={**data, "client_id": cid},
                         headers={"Content-Type": "application/x-www-form-urlencoded"},
                         timeout=_HTTP_TIMEOUT)
    if resp.status_code != 200:
        raise NotLoggedIn(f"Spotify refused the token request (HTTP {resp.status_code}): "
                          f"{resp.text[:200]}")
    return resp.json()


def _access_token() -> str:
    with _lock:
        tok = _load()
        if not tok.get("refresh_token"):
            raise NotLoggedIn("Spotify isn't linked yet.")
        if tok.get("access_token") and time.time() < float(tok.get("expires_at", 0)):
            return tok["access_token"]
        fresh = _token_request({"grant_type": "refresh_token",
                                "refresh_token": tok["refresh_token"]})
        tok["access_token"] = fresh["access_token"]
        tok["expires_at"] = time.time() + float(fresh.get("expires_in", 3600)) - _TOKEN_SKEW
        # Spotify may rotate the refresh token; keep whichever is newest.
        if fresh.get("refresh_token"):
            tok["refresh_token"] = fresh["refresh_token"]
        _save(tok)
        return tok["access_token"]


def _get(url: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    import requests
    try:
        resp = requests.get(url, params=params, timeout=_HTTP_TIMEOUT,
                            headers={"Authorization": f"Bearer {_access_token()}"})
    except NotLoggedIn:
        raise
    except Exception as exc:
        logger.warning("Spotify request failed: %s", exc)
        return None
    if resp.status_code != 200:
        logger.warning("Spotify %s returned HTTP %s", urlparse(url).path, resp.status_code)
        return None
    return resp.json()


# ── Playlists ─────────────────────────────────────────────────────────────────

def playlists(refresh: bool = False) -> list[dict[str, Any]]:
    """The user's playlists, their own and the ones they follow:
    [{name, uri, mine}]. Cached for ten minutes."""
    global _playlists, _playlists_at
    with _lock:
        if not refresh and _playlists and time.time() - _playlists_at < _PLAYLIST_TTL:
            return _playlists
        me = _get(f"{spotify_api.API_BASE}/me") or {}
        found: list[dict[str, Any]] = []
        url: str | None = f"{spotify_api.API_BASE}/me/playlists"
        params: dict[str, Any] | None = {"limit": 50}
        while url and len(found) < 1000:
            page = _get(url, params)
            if not page:
                break
            for item in page.get("items") or []:
                if isinstance(item, dict) and item.get("uri"):
                    found.append({
                        "name": str(item.get("name", "")),
                        "uri": item["uri"],
                        "mine": (item.get("owner") or {}).get("id") == me.get("id"),
                    })
            url, params = page.get("next"), None
        _playlists, _playlists_at = found, time.time()
        return found


def _norm(text: str) -> str:
    t = re.sub(r"[^\w\s]", " ", text.lower())
    t = re.sub(r"\b(?:my|the|playlist)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def canonical(query: str) -> str:
    q = _norm(query)
    return _ALIASES.get(q, q)


def match(query: str, candidates: list[dict[str, Any]], strict: bool = False) -> dict[str, Any] | None:
    """The playlist a spoken name means. Exact beats contained beats close;
    on a tie the user's own playlist beats one they only follow. `strict`
    drops "close": for a bare "play risk", which is usually a song."""
    q = canonical(query)
    if not q:
        return None
    words = set(q.split())
    best, best_score = None, 0.0
    for p in candidates:
        n = _norm(p["name"])
        if not n:
            continue
        if n == q:
            score = 3.0
        elif words <= set(n.split()) or q in n:
            score = 2.0 + len(q) / max(len(n), 1)       # "workout" → "Workout 2026" over "Workout Mix Vol. 7 Extended"
        elif strict:
            score = 0.0
        else:
            ratio = difflib.SequenceMatcher(None, q, n).ratio()
            score = ratio if ratio >= 0.75 else 0.0
        if score and p.get("mine"):
            score += 0.01
        if score > best_score:
            best, best_score = p, score
    return best


def find_playlist(query: str, strict: bool = False) -> dict[str, Any] | None:
    """{name, uri} of the user's playlist called something like `query`, or
    None. Raises NotLoggedIn before the first login."""
    hit = match(query, playlists(), strict)
    if hit is None and not strict and _playlists_at and time.time() - _playlists_at > 5:
        hit = match(query, playlists(refresh=True), strict)     # made one a minute ago
    return hit


# ── Playback on the phone (Spotify Connect) ───────────────────────────────────
#
# Found on the Pixel: Spotify's Android media session ignores play-from-URI
# from NORA's app (it logged no change at all, twice, over 10s), and the
# deep link needs NORA on screen. Connect goes through Spotify's servers to
# the phone's Spotify, so it works with both apps in the background. It needs
# Premium, which the user has, and Spotify running on the phone — otherwise the
# phone isn't a Connect device and play_on_phone falls back to the phone.

class PlaybackError(RuntimeError):
    """Spotify refused or failed to play; the message is for the user."""


def can_play() -> bool:
    """Linked with the playback scopes (a login from before they were asked
    for has only the playlist ones)."""
    return _PLAYBACK_SCOPE in str(_load().get("scope", "")).split()


def _send(method: str, path: str, params: dict | None = None, body: dict | None = None):
    import requests
    try:
        return requests.request(method, f"{spotify_api.API_BASE}{path}", params=params, json=body,
                                timeout=_HTTP_TIMEOUT,
                                headers={"Authorization": f"Bearer {_access_token()}"})
    except NotLoggedIn:
        raise
    except Exception as exc:
        raise PlaybackError(f"I couldn't reach Spotify: {exc}") from exc


def phone_device() -> dict[str, Any] | None:
    """The phone as a Connect device: {id, name, active}, or None when its
    Spotify isn't running. `spotify.phone_device` in config picks one by name."""
    resp = _send("GET", "/me/player/devices")
    if resp.status_code != 200:
        return None
    want = str(spotify_api._cfg().get("phone_device") or "").casefold()
    phones = [d for d in (resp.json().get("devices") or [])
              if d.get("id") and (d.get("name", "").casefold() == want if want
                                  else d.get("type") == "Smartphone")]
    if not phones:
        return None
    d = sorted(phones, key=lambda d: not d.get("is_active"))[0]
    return {"id": d["id"], "name": d.get("name", "phone"), "active": bool(d.get("is_active"))}


def _now_playing() -> dict[str, Any] | None:
    resp = _send("GET", "/me/player")
    if resp.status_code != 200 or not resp.content:
        return None
    return resp.json()


def _describe(item: dict[str, Any]) -> str:
    artists = ", ".join(a.get("name", "") for a in item.get("artists") or [] if isinstance(a, dict))
    return f"{item.get('name', '')} by {artists}" if artists else str(item.get("name", ""))


def play_on_device(uri: str, device_id: str, shuffle: bool = False,
                   settle_sec: float = 4.0) -> str:
    """Play `uri` (a track, or a playlist/album/artist context) on the device
    and confirm it took. Returns what is playing ("Risk by Deftones")."""
    body = {"uris": [uri]} if uri.startswith("spotify:track:") else {"context_uri": uri}
    resp = _send("PUT", "/me/player/play", {"device_id": device_id}, body)
    if resp.status_code == 403:
        raise PlaybackError("Spotify says playback control needs Premium on this account.")
    if resp.status_code == 404:
        raise PlaybackError("Spotify couldn't find the phone. Open Spotify on it and try again.")
    if resp.status_code not in (200, 202, 204):
        raise PlaybackError(f"Spotify refused to play it (HTTP {resp.status_code}).")
    if shuffle:
        # After play, not before: shuffle applies to the device that is playing.
        _send("PUT", "/me/player/shuffle", {"state": "true", "device_id": device_id})

    deadline = time.time() + settle_sec
    while True:
        state = _now_playing() or {}
        item = state.get("item") or {}
        ctx = (state.get("context") or {}).get("uri")
        on_it = item.get("uri") == uri or ctx == uri
        if on_it and state.get("is_playing") and (state.get("device") or {}).get("id") == device_id:
            return _describe(item)
        if time.time() >= deadline:
            what = _describe(item) if item else "nothing"
            raise PlaybackError(f"Spotify accepted it, but the phone is still on {what}.")
        time.sleep(0.5)


# ── Login (run once, by hand) ─────────────────────────────────────────────────

def login(open_browser: bool = True, timeout_sec: float = 300.0) -> str:
    """Browser login with PKCE. Returns the Spotify display name."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    cid, _ = spotify_api.credentials()
    if not cid:
        raise NotLoggedIn("Put SPOTIFY_CLIENT_ID in .env first.")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    redirect = redirect_uri()
    target = urlparse(redirect)
    url = AUTHORIZE_URL + "?" + urlencode({
        "client_id": cid, "response_type": "code", "redirect_uri": redirect,
        "code_challenge_method": "S256", "code_challenge": challenge,
        "scope": SCOPES, "state": state,
    })

    got: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != target.path:
                self.send_response(404)
                self.end_headers()
                return
            q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            got.update(q)
            ok = q.get("state") == state and "code" in q
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(("NORA is linked to Spotify. You can close this tab." if ok else
                              f"Spotify login failed: {q.get('error', 'bad state')}").encode())

        def log_message(self, *args):
            pass

    server = HTTPServer((target.hostname or "127.0.0.1", target.port or 80), Handler)
    server.timeout = 1.0
    print("Open this link to let NORA read your Spotify playlists:\n\n  " + url + "\n", flush=True)
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    deadline = time.time() + timeout_sec
    try:
        while "code" not in got and "error" not in got and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if got.get("state") != state or "code" not in got:
        raise NotLoggedIn(f"Login didn't complete: {got.get('error', 'timed out')}")

    tok = _token_request({"grant_type": "authorization_code", "code": got["code"],
                          "redirect_uri": redirect, "code_verifier": verifier})
    _save({"access_token": tok["access_token"], "refresh_token": tok["refresh_token"],
           "expires_at": time.time() + float(tok.get("expires_in", 3600)) - _TOKEN_SKEW,
           "scope": tok.get("scope", SCOPES)})
    me = _get(f"{spotify_api.API_BASE}/me") or {}
    return str(me.get("display_name") or me.get("id") or "your account")


def logout() -> bool:
    global _playlists, _playlists_at
    with _lock:
        _playlists, _playlists_at = [], 0.0
        try:
            TOKEN_PATH.unlink()
            return True
        except FileNotFoundError:
            return False


def _main(argv: list[str]) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(_ROOT / ".env")
    except ImportError:
        pass
    cmd = argv[0] if argv else "status"
    if cmd == "login":
        who = login()
        n = len(playlists(refresh=True))
        print(f"Linked to Spotify as {who}. {n} playlists visible.")
    elif cmd == "logout":
        print("Unlinked." if logout() else "Wasn't linked.")
    elif cmd == "status":
        if not is_logged_in():
            print("Not linked. Run: python -m nora.spotify_user login")
            return 1
        for p in playlists(refresh=True):
            print(("  * " if p["mine"] else "    ") + p["name"])
        if not can_play():
            print("Playback control not granted: run login again.")
        else:
            d = phone_device()
            print(f"Phone as a Connect device: {d['name'] if d else 'not visible (open Spotify on it)'}")
    else:
        print("usage: python -m nora.spotify_user [login|status|logout]")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))

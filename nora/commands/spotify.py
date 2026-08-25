"""Spotify control for NORA — MPRIS for playback, Web API for precise search.

Linux-native throughout: real D-Bus calls, no keyboard automation and no mouse
coordinates. Spotify is the only music backend.

Everything works with NO credentials. The desktop client resolves Spotify's own
search URI and plays the top hit outright:

    "play blinding lights"
        └─ mpris.OpenUri("spotify:search:blinding%20lights")   → plays it

Adding SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET to .env upgrades *accuracy*,
not capability. A field-qualified Web API lookup picks the original recording
where a loose search can land on a cover or a remix, and it can target an
artist, album or playlist specifically rather than whatever the query hits:

    "play blinding lights by the weeknd"
        ├─ spotify_api.find_track()   → spotify:track:0VjIjW4G…   (HTTPS)
        └─ mpris.OpenUri(uri)         → plays exactly that        (D-Bus)

Neither path needs Spotify Premium: the local client does the playing, so the
Web API is only ever used to look things up, never to control playback.

Transport, volume, shuffle, repeat and now-playing are pure MPRIS and never
touch the network.
"""
from __future__ import annotations

import logging
import time

from nora import context, memory, spotify_api
from nora.command_engine import register
from nora.config import get_config

logger = logging.getLogger("nora.commands.spotify")

PLAYER = "spotify"
SOURCE = "spotify"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mpris():
    from nora.platform.linux import mpris
    return mpris


def _cfg() -> dict:
    return get_config().get("spotify", {}) or {}


def _launch_command() -> str:
    return str(_cfg().get("launch_command") or "spotify")


def _autostart() -> bool:
    return bool(_cfg().get("autostart", True))


def _ensure_running() -> bool:
    """Make sure the Spotify client is on the session bus, launching if allowed."""
    m = _mpris()
    if m.is_running(PLAYER):
        return True
    if not _autostart():
        return False
    logger.info("Spotify is not running — launching it")
    return m.launch(_launch_command(), PLAYER)


def _not_running_msg() -> str:
    return "Spotify isn't running, and I couldn't start it."


def _track_key(snapshot: dict) -> str:
    """Stable identity for a track, used to detect that playback moved on."""
    return snapshot.get("uri") or snapshot.get("title") or ""


def _current_key() -> str:
    """Identity of whatever is loaded right now — capture before switching tracks."""
    return _track_key(_mpris().now_playing(PLAYER))


def _sync_context(status: str = "playing", changed_from: str | None = None,
                  timeout: float = 2.0) -> dict:
    """Refresh NORA's music state from what Spotify actually reports.

    The client keeps serving the *outgoing* track's metadata for a beat after
    OpenUri or Next, so reading once makes NORA announce the previous song.
    When *changed_from* is given, this polls until the reported track differs
    from it. Falls through on timeout, which is the honest answer when the
    track genuinely did not change (Previous often just restarts the current
    one).
    """
    m = _mpris()
    deadline = time.monotonic() + timeout
    snapshot = m.now_playing(PLAYER)

    while time.monotonic() < deadline:
        key = _track_key(snapshot)
        if key and key != changed_from:
            break
        time.sleep(0.15)
        snapshot = m.now_playing(PLAYER)

    live_status = (snapshot.get("status") or "").lower() or status
    context.update_music(
        track=snapshot.get("title", ""),
        artist=snapshot.get("artist", ""),
        source=SOURCE,
        status=live_status,
    )
    return snapshot


def _describe(snapshot: dict, fallback: str = "") -> str:
    title, artist = snapshot.get("title", ""), snapshot.get("artist", "")
    if title and artist:
        return f"{title} by {artist}"
    return title or fallback


def _play_uri(uri: str) -> bool:
    """Start playback of a Spotify URI.

    Tracks go through OpenUri; albums, artists and playlists are *contexts* and
    go through LoadContextUri so the whole thing queues rather than a single
    item. Falls back to the other method if the client rejects the first.
    """
    m = _mpris()
    is_context = any(k in uri for k in (":album:", ":artist:", ":playlist:"))
    order = ["LoadContextUri", "OpenUri"] if is_context else ["OpenUri", "LoadContextUri"]

    for method in order:
        ok, reply = m.call(method, [uri], player=PLAYER)
        if ok:
            logger.info("%s(%s) succeeded", method, uri)
            return True
        logger.debug("%s(%s) failed: %s", method, uri, reply)
    return False


def _play_search(query: str) -> str:
    """Credential-free path: hand the raw query to Spotify's own search URI.

    The Linux client resolves spotify:search:<query> and starts playing its top
    result outright — it does not merely open a search page. So this is a real
    fallback, not a degraded one, and NORA works with no credentials at all.

    The Web API path is still preferred when configured, because a
    field-qualified lookup (track:"x" artist:"y") reliably picks the original
    recording where a loose search can land on a cover or a remix.
    """
    query = query.strip()
    if not query:
        return "What would you like me to play?"

    from urllib.parse import quote
    previous = _current_key()
    if not _play_uri("spotify:search:" + quote(query, safe="")):
        return f"I couldn't play '{query}'."

    snapshot = _sync_context("playing", changed_from=previous)
    if not snapshot.get("title"):
        return f"I searched Spotify for '{query}'."

    memory.remember_music(snapshot["title"], snapshot.get("artist", ""), SOURCE)

    if not spotify_api.is_configured():
        logger.info(
            "Played '%s' via Spotify's search URI. Set SPOTIFY_CLIENT_ID / "
            "SPOTIFY_CLIENT_SECRET for exact-match lookups.", query
        )

    return f"Playing {_describe(snapshot, query)}."


def _play_resolved(uri: str, label: str, track: str, artist: str) -> str:
    """Play a resolved URI and report it, updating context + preference memory."""
    previous = _current_key()
    if not _play_uri(uri):
        return f"I found {label}, but Spotify wouldn't play it."

    snapshot = _sync_context("playing", changed_from=previous)
    memory.remember_music(
        snapshot.get("title") or track, snapshot.get("artist") or artist, SOURCE
    )

    return f"Playing {_describe(snapshot, label)}."


# ── Playback: by name ─────────────────────────────────────────────────────────

@register(
    "play_music",
    sig="play_music(track: str, artist: str)",
    description="Play a song on Spotify. Empty track = the user's last-played track.",
    category="music",
)
def play_music(track: str = "", artist: str = "") -> str:
    """Play a track on Spotify, resolving the name via the Web API.

    With no track, replays the user's remembered preference — this is what
    "play music" / "play something" maps to.
    """
    if not _ensure_running():
        return _not_running_msg()

    if not track and not artist:
        pref = memory.get_preferred_music()
        track, artist = pref.get("track", ""), pref.get("artist", "")
        if not track:
            ok, _ = _mpris().call("Play", player=PLAYER)
            if ok:
                snapshot = _sync_context("playing")
                spoken = _describe(snapshot)
                return f"Playing {spoken}." if spoken else "Playing."
            return "I don't have a track to play yet. Tell me a song and I'll remember it."

    # Already playing what was asked for — don't restart it.
    current = _mpris().now_playing(PLAYER)
    if (
        current.get("status") == "Playing"
        and track
        and track.lower() in current.get("title", "").lower()
    ):
        return f"{current['title']} is already playing."

    if not spotify_api.is_configured():
        return _play_search(f"{track} {artist}".strip())

    found = spotify_api.find_track(track, artist)
    if not found:
        logger.info("No track match for '%s' / '%s'; trying artist", track, artist)
        if artist:
            return spotify_play_artist(artist)
        return _play_search(f"{track} {artist}".strip())

    return _play_resolved(
        found["uri"], f"{found['title']} by {found['artist']}",
        found["title"], found["artist"],
    )


@register(
    "spotify_play_song",
    sig="spotify_play_song(song: str)",
    description="Play a specific song by name on Spotify.",
    category="music",
)
def spotify_play_song(song: str = "") -> str:
    if not song:
        return "Which song would you like?"
    return play_music(track=song)


@register(
    "spotify_play_artist",
    sig="spotify_play_artist(artist: str)",
    description="Play an artist's popular tracks on Spotify.",
    category="music",
)
def spotify_play_artist(artist: str = "") -> str:
    if not artist:
        return "Which artist would you like?"
    if not _ensure_running():
        return _not_running_msg()
    if not spotify_api.is_configured():
        return _play_search(artist)

    found = spotify_api.find_artist(artist)
    if not found:
        return _play_search(artist)
    return _play_resolved(found["uri"], found["name"], "", found["name"])


@register(
    "spotify_play_album",
    sig="spotify_play_album(album: str, artist: str)",
    description="Play a full album on Spotify.",
    category="music",
)
def spotify_play_album(album: str = "", artist: str = "") -> str:
    if not album:
        return "Which album would you like?"
    if not _ensure_running():
        return _not_running_msg()
    if not spotify_api.is_configured():
        return _play_search(f"{album} {artist}".strip())

    found = spotify_api.find_album(album, artist)
    if not found:
        return _play_search(f"{album} {artist}".strip())
    label = f"{found['name']} by {found['artist']}" if found["artist"] else found["name"]
    return _play_resolved(found["uri"], label, found["name"], found["artist"])


@register(
    "spotify_play_playlist",
    sig="spotify_play_playlist(name: str)",
    description="Play a public Spotify playlist by name (not your private ones).",
    category="music",
)
def spotify_play_playlist(name: str = "") -> str:
    if not name:
        return "Which playlist would you like?"
    if not _ensure_running():
        return _not_running_msg()
    if not spotify_api.is_configured():
        return _play_search(name)

    found = spotify_api.find_playlist(name)
    if not found:
        return _play_search(name)
    return _play_resolved(found["uri"], found["name"], found["name"], "")


# ── Playback: transport ───────────────────────────────────────────────────────

def _transport(method: str, status: str, ok_msg: str, idle_msg: str) -> str:
    m = _mpris()
    if not m.is_running(PLAYER):
        return idle_msg
    ok, reply = m.call(method, player=PLAYER)
    if not ok:
        logger.warning("MPRIS %s failed: %s", method, reply)
        return f"Spotify wouldn't respond to {method.lower()}."
    context.update_music(status=status)
    return ok_msg


@register("resume_music", sig="resume_music()",
          description="Resume Spotify playback.", category="music")
def resume_music() -> str:
    """Resume playback; replays the remembered track if Spotify is closed."""
    m = _mpris()
    if not m.is_running(PLAYER):
        pref = memory.get_preferred_music()
        if pref.get("track"):
            return play_music(pref["track"], pref.get("artist", ""))
        return _not_running_msg()

    ok, _ = m.call("Play", player=PLAYER)
    if not ok:
        return "Spotify wouldn't resume."
    snapshot = _sync_context("playing")
    spoken = _describe(snapshot)
    return f"Resuming {spoken}." if spoken else "Resumed."


@register("pause_music", sig="pause_music()",
          description="Pause Spotify playback.", category="music")
def pause_music() -> str:
    return _transport("Pause", "paused", "Paused.", "Nothing is playing.")


@register("toggle_music", sig="toggle_music()",
          description="Toggle play/pause on Spotify.", category="music")
def toggle_music() -> str:
    m = _mpris()
    if not m.is_running(PLAYER):
        return resume_music()
    was_playing = m.playback_status(PLAYER) == "Playing"
    ok, _ = m.call("PlayPause", player=PLAYER)
    if not ok:
        return "Spotify wouldn't respond."
    context.update_music(status="paused" if was_playing else "playing")
    return "Paused." if was_playing else "Playing."


@register("stop_music", sig="stop_music()",
          description="Stop Spotify playback.", category="music")
def stop_music() -> str:
    """Stop Spotify and any locally-played audio (the wake-word entrance clip)."""
    stopped = False

    m = _mpris()
    if m.is_running(PLAYER):
        ok, _ = m.call("Stop", player=PLAYER)
        stopped = stopped or ok

    try:
        import pygame
        if pygame.mixer.get_init() and pygame.mixer.music.get_busy():
            pygame.mixer.music.stop()
            stopped = True
    except Exception:
        pass

    context.update_music(status="stopped")
    return "Music stopped." if stopped else "Nothing was playing."


@register("next_track", sig="next_track()",
          description="Skip to the next track on Spotify.", category="music")
def next_track() -> str:
    m = _mpris()
    if not m.is_running(PLAYER):
        return "Nothing is playing."
    previous = _current_key()
    ok, reply = m.call("Next", player=PLAYER)
    if not ok:
        logger.warning("MPRIS Next failed: %s", reply)
        return "Spotify wouldn't skip."
    snapshot = _sync_context("playing", changed_from=previous)
    spoken = _describe(snapshot)
    return f"Next: {spoken}." if spoken else "Next track."


@register("previous_track", sig="previous_track()",
          description="Go back to the previous track on Spotify.", category="music")
def previous_track() -> str:
    m = _mpris()
    if not m.is_running(PLAYER):
        return "Nothing is playing."
    # One Previous often just restarts the current track, matching Spotify's
    # own behaviour, so we report whatever actually ended up playing.
    previous = _current_key()
    ok, reply = m.call("Previous", player=PLAYER)
    if not ok:
        logger.warning("MPRIS Previous failed: %s", reply)
        return "Spotify wouldn't go back."
    snapshot = _sync_context("playing", changed_from=previous)
    spoken = _describe(snapshot)
    return f"Back to {spoken}." if spoken else "Previous track."


def seek_track(position_sec: float) -> str:
    """Jump to *position_sec* in the current track.

    Not a registered action: this exists for the dashboard's scrub bar, where
    the position comes from a drag rather than from a sentence. Spoken seeking
    would need a time parser and nobody has asked for one.
    """
    m = _mpris()
    if not m.is_running(PLAYER):
        return "Nothing is playing."
    if not m.seek_to(max(0.0, float(position_sec)), player=PLAYER):
        return "Spotify wouldn't seek."
    return f"Seeked to {int(position_sec) // 60}:{int(position_sec) % 60:02d}."


# ── State + settings ──────────────────────────────────────────────────────────

@register("now_playing", sig="now_playing()",
          description='What is currently playing — use for "what song is this".',
          category="music")
def now_playing() -> str:
    m = _mpris()
    if not m.is_running(PLAYER):
        return "Spotify isn't running."

    snapshot = m.now_playing(PLAYER)
    title = snapshot.get("title", "")
    if not title:
        return "Nothing is playing right now."

    context.update_music(
        track=title, artist=snapshot.get("artist", ""),
        source=SOURCE, status=(snapshot.get("status") or "").lower(),
    )

    artist, album = snapshot.get("artist", ""), snapshot.get("album", "")
    reply = title if not artist else f"{title} by {artist}"
    if album and album.lower() != title.lower():
        reply += f", from {album}"
    if snapshot.get("status") == "Paused":
        reply += " — paused"
    return reply + "."


@register("spotify_set_volume", sig="spotify_set_volume(level: int)",
          description="Set Spotify's own volume, 0-100, without touching system volume.",
          category="music")
def spotify_set_volume(level: int = 50) -> str:
    m = _mpris()
    if not m.is_running(PLAYER):
        return "Spotify isn't running."
    try:
        pct = max(0, min(int(level), 100))
    except (TypeError, ValueError):
        return "Give me a volume between 0 and 100."
    if m.set_prop("Volume", pct / 100.0, "d", player=PLAYER):
        return f"Spotify volume at {pct} percent."
    return "I couldn't change Spotify's volume."


@register("spotify_shuffle", sig="spotify_shuffle(enabled: bool)",
          description="Turn Spotify shuffle on or off.", category="music")
def spotify_shuffle(enabled: bool = True) -> str:
    m = _mpris()
    if not m.is_running(PLAYER):
        return "Spotify isn't running."
    if m.set_prop("Shuffle", bool(enabled), "b", player=PLAYER):
        return f"Shuffle {'on' if enabled else 'off'}."
    return "I couldn't change shuffle."


@register("spotify_repeat", sig="spotify_repeat(mode: str)",
          description='Set repeat mode: "off", "track", or "all".', category="music")
def spotify_repeat(mode: str = "off") -> str:
    m = _mpris()
    if not m.is_running(PLAYER):
        return "Spotify isn't running."
    loop = {
        "off": "None", "none": "None", "no": "None",
        "track": "Track", "song": "Track", "one": "Track", "this": "Track",
        "all": "Playlist", "playlist": "Playlist", "album": "Playlist",
    }.get(str(mode).lower().strip())
    if loop is None:
        return 'Repeat can be "off", "track", or "all".'
    if m.set_prop("LoopStatus", loop, "s", player=PLAYER):
        return {"None": "Repeat off.", "Track": "Repeating this track.",
                "Playlist": "Repeating everything."}[loop]
    return "I couldn't change repeat mode."


@register("open_spotify", sig="open_spotify()",
          description="Launch Spotify and bring its window to the front.",
          category="music")
def open_spotify() -> str:
    m = _mpris()
    if not _ensure_running():
        return _not_running_msg()
    m.call("Raise", player=PLAYER, interface=m.ROOT_IFACE)
    return "Spotify is up."

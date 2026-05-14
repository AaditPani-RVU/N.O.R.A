from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from nora import context, memory
from nora.command_engine import register
from nora.config import get_config

logger = logging.getLogger("nora.commands.music")

# â"€â"€ Config â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

TRACK_NAME   = "Should I Stay or Should I Go"
ARTIST_NAME  = "The Clash"

# Drop any .mp3/.wav/.ogg file into this folder and NORA will play it.
# Accepted filenames (case-insensitive, any order):
#   should i stay or should i go.mp3
#   the clash - should i stay.mp3
#   should_i_stay.mp3   --¦ etc.
SOUNDS_DIR   = Path(__file__).parent.parent.parent / "sounds"

# Keywords that must all appear in the filename for it to match
_MATCH_WORDS = {"should", "stay", "go"}

# â"€â"€ Helpers â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

def _wake_limit_ms() -> int:
    """Return the configured wake-triggered playback cap in milliseconds."""
    cfg = get_config().get("music", {})
    return int(cfg.get("wake_playback_limit_seconds", 10)) * 1000


def _find_local_track() -> Path | None:
    """Search SOUNDS_DIR for a file matching MATCH_WORDS in its stem."""
    if not SOUNDS_DIR.exists():
        return None
    for ext in ("*.mp3", "*.wav", "*.ogg", "*.flac", "*.m4a"):
        for f in SOUNDS_DIR.glob(ext):
            stem = f.stem.lower().replace("-", " ").replace("_", " ")
            if all(w in stem for w in _MATCH_WORDS):
                return f
    # Loose fallback: any audio file in the sounds dir
    for ext in ("*.mp3", "*.wav", "*.ogg"):
        files = list(SOUNDS_DIR.glob(ext))
        if files:
            return files[0]
    return None


def _play_local_full(path: Path) -> None:
    """Play an audio file via pygame.mixer (full playback, non-blocking)."""
    import pygame
    pygame.mixer.init()
    pygame.mixer.music.load(str(path))
    pygame.mixer.music.play()
    logger.info("Playing local file (full): %s", path.name)


def _play_local_limited(path: Path, duration_ms: int) -> None:
    """Play up to *duration_ms* milliseconds of *path*, then fade out.

    Preferred path uses pydub + simpleaudio so the clip is pre-trimmed
    before playback (no extra teardown needed).  Falls back to a timed
    pygame stop when pydub/simpleaudio are unavailable.
    """
    seconds = duration_ms / 1000
    logger.info(
        "Playing local file (limited to %.0fs): %s", seconds, path.name
    )

    # â"€â"€ pydub + simpleaudio (spec-preferred) â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    try:
        from pydub import AudioSegment          # type: ignore
        import simpleaudio as sa                 # type: ignore

        audio   = AudioSegment.from_file(str(path))
        clipped = audio[:duration_ms]

        play_obj = sa.play_buffer(
            clipped.raw_data,
            num_channels=clipped.channels,
            bytes_per_sample=clipped.sample_width,
            sample_rate=clipped.frame_rate,
        )
        # Fail-safe stop after the clip length + a small buffer
        def _stop(obj: sa.PlayObject, delay: float) -> None:
            time.sleep(delay + 0.5)
            obj.stop()

        threading.Thread(
            target=_stop, args=(play_obj, seconds), daemon=True
        ).start()
        return
    except ImportError:
        logger.debug("pydub/simpleaudio not installed; using pygame timed stop")
    except Exception as exc:
        logger.warning("pydub playback failed (%s); using pygame timed stop", exc)

    # â"€â"€ pygame fallback â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    import pygame
    pygame.mixer.init()
    pygame.mixer.music.load(str(path))
    pygame.mixer.music.play()

    def _pygame_stop(delay: float) -> None:
        time.sleep(delay)
        try:
            pygame.mixer.music.fadeout(500)
        except Exception:
            pass

    threading.Thread(
        target=_pygame_stop, args=(seconds,), daemon=True
    ).start()


def _play_local(path: Path, wake_triggered: bool = False) -> None:
    """Play a local audio file.  Clips to the configured limit when wake-triggered."""
    if wake_triggered:
        _play_local_limited(path, _wake_limit_ms())
    else:
        _play_local_full(path)


# â"€â"€ Iron Man entrance â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

def iron_man_entrance() -> None:
    """Play the wake-up track when the wake phrase is spoken.

    Runs in a background thread so the greeting speech is never delayed.
    Playback is automatically capped at ``wake_playback_limit_seconds``
    (default 10 s) because this is always a wake-triggered event.

    Strategy (in order):
      1. Local MP3 in  d:/JARVIS/sounds/  -- instant, no browser
      2. iTunes COM   -- if classic iTunes is installed
      3. YouTube      -- always available, opens in default browser
    """
    thread = threading.Thread(
        target=_entrance_worker, daemon=True, name="iron-man-entrance"
    )
    thread.start()


def _entrance_worker() -> None:
    # â"€â"€ Strategy 1: local file (fastest, best) â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    local = _find_local_track()
    if local is not None:
        try:
            # Entrance is always wake-triggered â†' enforce playback limit
            _play_local(local, wake_triggered=True)
            context.update_music(
                track=local.stem, artist=ARTIST_NAME,
                source="local", status="playing",
            )
            return
        except Exception as exc:
            logger.warning("Local playback failed (%s), trying next strategy", exc)
    else:
        logger.info(
            "No local track found in %s -- "
            "drop an MP3 there to enable offline playback. Trying YouTube--¦",
            SOUNDS_DIR,
        )

    # â"€â"€ Strategy 2: iTunes COM â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    try:
        _play_via_itunes_com()
        logger.info("Iron Man entrance: playing via iTunes COM")
        return
    except Exception as exc:
        logger.warning("iTunes COM unavailable (%s), falling back to YouTube", exc)

    # â"€â"€ Strategy 3: YouTube in browser â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    try:
        _play_via_youtube()
        logger.info("Iron Man entrance: playing via YouTube")
    except Exception as exc:
        logger.error("All music strategies failed: %s", exc)


# â"€â"€ Strategy implementations â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

def _play_via_itunes_com() -> None:
    """Try Apple Music (new app) then classic iTunes COM."""
    import win32com.client

    # Windows Apple Music registers under 'AppleMusic.Application' on newer
    # installs, classic iTunes uses 'iTunes.Application'.  Try both.
    com_names = ["AppleMusic.Application", "iTunes.Application"]
    itunes = None
    for name in com_names:
        try:
            itunes = win32com.client.Dispatch(name)
            logger.debug("Connected to COM: %s", name)
            break
        except Exception as exc:
            logger.debug("COM '%s' unavailable: %s", name, exc)

    if itunes is None:
        raise RuntimeError("Neither AppleMusic nor iTunes COM is available")

    results = itunes.LibraryPlaylist.Search(TRACK_NAME, 5)
    if results is None or results.Count == 0:
        raise RuntimeError(f"'{TRACK_NAME}' not found in library")
    chosen = results.Item(1)
    for i in range(1, results.Count + 1):
        t = results.Item(i)
        if ARTIST_NAME.lower() in t.Artist.lower():
            chosen = t
            break
    logger.info("Playing via COM: %s -- %s", chosen.Name, chosen.Artist)
    chosen.Play()


def _play_via_youtube() -> None:
    import webbrowser
    import pyautogui

    query  = f"{TRACK_NAME} {ARTIST_NAME}".replace(" ", "+")
    yt_url = f"https://www.youtube.com/results?search_query={query}"
    logger.info("Opening YouTube: %s", yt_url)
    webbrowser.open(yt_url)

    time.sleep(4.0)  # let the page fully render

    sw, sh = pyautogui.size()
    click_x = int(sw * 0.20)
    click_y = int(sh * 0.30)
    logger.info("Clicking first YouTube result at (%d, %d)", click_x, click_y)
    pyautogui.click(click_x, click_y)


# â"€â"€ Registered voice commands â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

@register("play_music", sig="play_music(track: str, artist: str)",
           description="Priority-chain playback (local -> Apple Music -> YouTube)", category="music")
def play_music(track: str = "", artist: str = "") -> str:
    """Play a track with the priority chain:

      1. Local MP3 in sounds/          (fastest, offline)
      2. Apple Music / iTunes COM      (deterministic)
      3. Apple Music URI (itms://)     (opens app)
      4. YouTube search                (always-available fallback)

    - If current music matches the request and is already playing: no-op.
    - Wake-triggered playback is capped at config.music.wake_playback_limit_seconds.
    - Updates context.music so the UI reflects real state.
    - Remembers the last-played track as the user's music preference.
    """
    wake = context.wake_triggered

    # "play something" / empty â†' use preferred or Iron-Man default
    if not track:
        pref = memory.get_preferred_music()
        if pref.get("track"):
            track, artist = pref["track"], pref.get("artist", "")
        else:
            track, artist = TRACK_NAME, ARTIST_NAME

    # Don't restart the same track if it's already playing
    current = context.get_music()
    if (
        current["status"] == "playing"
        and track.lower() in (current["track"] or "").lower()
    ):
        return f"{current['track']} is already playing."

    # â"€â"€ 1. Local file â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    if track.lower() in TRACK_NAME.lower() or track.lower() == "default":
        local = _find_local_track()
        if local:
            try:
                _play_local(local, wake_triggered=wake)
                context.update_music(
                    track=local.stem, artist=ARTIST_NAME,
                    source="local", status="playing",
                )
                memory.remember_music(local.stem, ARTIST_NAME, "local")
                label = f"{local.stem} (10 s preview)" if wake else local.stem
                return f"Playing {label}."
            except Exception as exc:
                logger.warning("Local playback failed: %s", exc)

    # â"€â"€ 2. Apple Music / iTunes COM â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    try:
        import win32com.client

        itunes = None
        for com_name in ("AppleMusic.Application", "iTunes.Application"):
            try:
                itunes = win32com.client.Dispatch(com_name)
                logger.debug("Connected to COM: %s", com_name)
                break
            except Exception as exc:
                logger.debug("COM '%s' unavailable: %s", com_name, exc)

        if itunes is not None:
            results = itunes.LibraryPlaylist.Search(track, 5)
            if results is None or results.Count == 0:
                logger.warning("'%s' not found in Apple Music library -- trying URI fallback", track)
            else:
                chosen = results.Item(1)
                if artist:
                    for i in range(1, results.Count + 1):
                        t = results.Item(i)
                        if artist.lower() in t.Artist.lower():
                            chosen = t
                            break
                chosen.Play()
                context.update_music(
                    track=chosen.Name, artist=chosen.Artist,
                    source="apple_music_com", status="playing",
                )
                memory.remember_music(chosen.Name, chosen.Artist, "apple_music_com")
                if wake:
                    limit_s = _wake_limit_ms() / 1000
                    def _pause_itunes(delay: float) -> None:
                        time.sleep(delay)
                        try:
                            itunes.Pause()
                            context.update_music(status="paused")
                        except Exception:
                            pass
                    threading.Thread(
                        target=_pause_itunes, args=(limit_s,), daemon=True
                    ).start()
                    return f"Playing {chosen.Name} by {chosen.Artist} (10 s preview)."
                return f"Playing {chosen.Name} by {chosen.Artist}."
    except Exception as exc:
        logger.warning("Apple Music / iTunes COM error: %s -- trying URI fallback", exc)

    # â"€â"€ 3. Apple Music URI fallback â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    try:
        import subprocess
        query = f"{track} {artist}".strip().replace(" ", "+")
        uri = f"itms://music.apple.com/search?term={query}"
        logger.info("Launching Apple Music via URI: %s", uri)
        subprocess.Popen(["explorer", uri], shell=False)
        context.update_music(
            track=track, artist=artist,
            source="apple_music_web", status="playing",
        )
        memory.remember_music(track, artist, "apple_music_web")
        return f"Opening Apple Music for '{track}'."
    except Exception as exc:
        logger.warning("URI fallback failed: %s -- trying YouTube", exc)

    # â"€â"€ 4. YouTube fallback â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    try:
        import webbrowser
        query = f"{track} {artist}".strip().replace(" ", "+")
        url = f"https://www.youtube.com/results?search_query={query}"
        webbrowser.open(url)
        context.update_music(
            track=track, artist=artist,
            source="youtube", status="playing",
        )
        memory.remember_music(track, artist, "youtube")
        return f"Searching YouTube for {track}."
    except Exception as exc:
        logger.error("YouTube fallback also failed: %s", exc)

    return f"I couldn't play '{track}'."


@register("resume_music", sig="resume_music()", description="Resume last-played / paused track", category="music")
def resume_music() -> str:
    """Resume the last-played track (after a stop or pause)."""
    # Paused pygame â†' unpause
    try:
        import pygame
        if pygame.mixer.get_init() and not pygame.mixer.music.get_busy():
            pygame.mixer.music.unpause()
            if pygame.mixer.music.get_busy():
                context.update_music(status="playing")
                return "Resumed."
    except Exception:
        pass

    # Otherwise re-play last track / preferred
    current = context.get_music()
    track = current.get("track") or context.music.last_track
    artist = current.get("artist") or context.music.last_artist
    if not track:
        pref = memory.get_preferred_music()
        track = pref.get("track", "")
        artist = pref.get("artist", "")
    if not track:
        return "Nothing to resume."
    return play_music(track=track, artist=artist)


@register("stop_music", sig="stop_music()", category="music")
def stop_music() -> str:
    """Stop all music playback (local, iTunes, and pygame TTS-adjacent)."""
    stopped = False
    try:
        import pygame
        if pygame.mixer.get_init() and pygame.mixer.music.get_busy():
            pygame.mixer.music.stop()
            stopped = True
    except Exception:
        pass

    try:
        import win32com.client
        for com_name in ("AppleMusic.Application", "iTunes.Application"):
            try:
                win32com.client.Dispatch(com_name).Stop()
                stopped = True
                break
            except Exception:
                continue
    except Exception:
        pass

    context.update_music(status="stopped")
    return "Music stopped." if stopped else "Nothing was playing."


@register("pause_music", sig="pause_music()", category="music")
def pause_music() -> str:
    """Pause or resume music playback."""
    # Pygame pause/unpause
    try:
        import pygame
        if pygame.mixer.get_init():
            if pygame.mixer.music.get_busy():
                pygame.mixer.music.pause()
                context.update_music(status="paused")
                return "Paused."
            else:
                pygame.mixer.music.unpause()
                if pygame.mixer.music.get_busy():
                    context.update_music(status="playing")
                    return "Resumed."
    except Exception:
        pass

    # iTunes fallback
    try:
        import win32com.client
        for com_name in ("AppleMusic.Application", "iTunes.Application"):
            try:
                itunes = win32com.client.Dispatch(com_name)
                itunes.PlayPause()
                # We don't know the new state reliably; assume toggled
                current = context.get_music()
                new_status = "paused" if current["status"] == "playing" else "playing"
                context.update_music(status=new_status)
                return "Done."
            except Exception:
                continue
    except Exception as exc:
        logger.error("pause_music error: %s", exc)

    return "I couldn't pause the music."

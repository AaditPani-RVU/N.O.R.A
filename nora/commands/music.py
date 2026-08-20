"""Wake-word entrance clip — local audio only.

This module owns exactly one thing: the short flourish that plays when the
wake phrase is spoken. It reads a file from ``sounds/`` and plays it through
pygame, so it costs no network round-trip on the wake path and never touches
whatever is queued up in Spotify.

All actual music control lives in nora/commands/spotify.py.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from nora import context
from nora.config import get_config

logger = logging.getLogger("nora.commands.music")

# ── Config ────────────────────────────────────────────────────────────────────

TRACK_NAME = "Should I Stay or Should I Go"
ARTIST_NAME = "The Clash"

# Drop any .mp3/.wav/.ogg file into this folder and NORA will play it.
# Accepted filenames (case-insensitive, any order):
#   should i stay or should i go.mp3
#   the clash - should i stay.mp3
#   should_i_stay.mp3   … etc.
SOUNDS_DIR = Path(__file__).parent.parent.parent / "sounds"

# Keywords that must all appear in the filename for it to match
_MATCH_WORDS = {"should", "stay", "go"}


def _wake_limit_ms() -> int:
    """Return the configured wake-triggered playback cap in milliseconds."""
    cfg = get_config().get("music", {})
    return int(cfg.get("wake_playback_limit_seconds", 10)) * 1000


def _find_local_track() -> Path | None:
    """Search SOUNDS_DIR for a file matching _MATCH_WORDS in its stem."""
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


# ── Playback ──────────────────────────────────────────────────────────────────

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
    logger.info("Playing local file (limited to %.0fs): %s", seconds, path.name)

    # ── pydub + simpleaudio (spec-preferred) ──────────────────────────────
    try:
        from pydub import AudioSegment          # type: ignore
        import simpleaudio as sa                 # type: ignore

        audio = AudioSegment.from_file(str(path))
        clipped = audio[:duration_ms]

        play_obj = sa.play_buffer(
            clipped.raw_data,
            num_channels=clipped.channels,
            bytes_per_sample=clipped.sample_width,
            sample_rate=clipped.frame_rate,
        )

        # Fail-safe stop after the clip length + a small buffer
        def _stop(obj, delay: float) -> None:
            time.sleep(delay + 0.5)
            obj.stop()

        threading.Thread(target=_stop, args=(play_obj, seconds), daemon=True).start()
        return
    except ImportError:
        logger.debug("pydub/simpleaudio not installed; using pygame timed stop")
    except Exception as exc:
        logger.warning("pydub playback failed (%s); using pygame timed stop", exc)

    # ── pygame fallback ───────────────────────────────────────────────────
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

    threading.Thread(target=_pygame_stop, args=(seconds,), daemon=True).start()


def _play_local(path: Path, wake_triggered: bool = False) -> None:
    """Play a local audio file. Clips to the configured limit when wake-triggered."""
    if wake_triggered:
        _play_local_limited(path, _wake_limit_ms())
    else:
        _play_local_full(path)


# ── Iron Man entrance ─────────────────────────────────────────────────────────

def iron_man_entrance() -> None:
    """Play the wake-up clip when the wake phrase is spoken.

    Runs in a background thread so the greeting speech is never delayed, and
    is always capped at ``music.wake_playback_limit_seconds`` because this is
    by definition a wake-triggered event. Silently does nothing when no audio
    file is present in ``sounds/`` — the entrance is a flourish, not a feature
    worth failing a wake over.
    """
    threading.Thread(
        target=_entrance_worker, daemon=True, name="iron-man-entrance"
    ).start()


def _entrance_worker() -> None:
    local = _find_local_track()
    if local is None:
        logger.info(
            "No entrance clip in %s — drop an MP3 there to enable it.", SOUNDS_DIR
        )
        return

    try:
        _play_local(local, wake_triggered=True)
        context.update_music(
            track=local.stem, artist=ARTIST_NAME, source="local", status="playing",
        )
    except Exception as exc:
        logger.warning("Entrance playback failed: %s", exc)

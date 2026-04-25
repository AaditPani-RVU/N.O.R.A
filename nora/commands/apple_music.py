"""Apple Music control for NORA â€” keyboard-driven, no mouse coordinates.

Strategy (in order of reliability):
  1. Media keys (play/pause/next/prev) â€” global, no window focus needed
  2. Window focus via pygetwindow for search
  3. Ctrl+F â†’ type â†’ Enter to search in the desktop app
  4. Fallback: open web player URL if app window not found
"""
from __future__ import annotations

import logging
import subprocess
import time
import webbrowser

from nora.command_engine import register

logger = logging.getLogger("nora.commands.apple_music")

_APP_TITLES = ("Apple Music", "iTunes")
_WEB_URL    = "https://music.apple.com/"


# â”€â”€ Window helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _find_window():
    """Return the first Apple Music / iTunes window, or None."""
    try:
        import pygetwindow as gw
        for title in _APP_TITLES:
            wins = gw.getWindowsWithTitle(title)
            if wins:
                return wins[0]
    except Exception as exc:
        logger.debug("pygetwindow error: %s", exc)
    return None


def _focus_or_open() -> bool:
    """Bring Apple Music to front. Opens web player if no window found."""
    win = _find_window()
    if win:
        try:
            if win.isMinimized:
                win.restore()
                time.sleep(0.2)
            win.activate()
            time.sleep(0.4)
            logger.info("Apple Music window focused: %s", win.title)
            return True
        except Exception as exc:
            logger.warning("Could not activate window: %s", exc)

    # App not open â€” launch web player and re-check
    logger.info("Apple Music window not found â€” opening web player")
    webbrowser.open(_WEB_URL)
    time.sleep(3.5)

    win = _find_window()
    if win:
        try:
            win.activate()
            time.sleep(0.4)
            return True
        except Exception:
            pass
    return False


# â”€â”€ Media key helpers (global â€” no window focus required) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _media_key(key: str) -> bool:
    """Send a media key via pyautogui. Returns True on success."""
    try:
        import pyautogui
        pyautogui.press(key)
        logger.debug("Media key sent: %s", key)
        return True
    except Exception as exc:
        logger.warning("Media key %s failed: %s", key, exc)
        return False


# â”€â”€ Search flow â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _search_and_play(query: str) -> None:
    """Focus Apple Music, search, select first autocomplete result, play.

    Key insight: use Down+Enter BEFORE pressing Enter on the search bar.
    This picks from the autocomplete dropdown while focus is still in the
    search input â€” so Down moves through suggestions, not the page.
    """
    import pyautogui
    pyautogui.FAILSAFE = False

    _focus_or_open()

    # Focus search bar (Ctrl+F works in desktop app and web player)
    pyautogui.hotkey("ctrl", "f")
    time.sleep(0.4)

    # Clear and type â€” do NOT press Enter yet
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.write(query, interval=0.05)
    logger.info("Typed search query: %s", query)

    # Wait for autocomplete dropdown to fully render
    time.sleep(2.0)

    # One Down press selects the first suggestion in the dropdown
    pyautogui.press("down")
    time.sleep(0.3)

    # Enter: if it's a song entry it plays directly;
    # if it's a text suggestion it navigates to the search results page
    pyautogui.press("enter")
    time.sleep(3.0)  # wait for results page to load if needed

    # On the results page the first song is usually auto-focused â€”
    # pressing Enter again plays it. Harmless if already playing.
    pyautogui.press("enter")
    time.sleep(0.3)

    # Fire media play key as final fallback to ensure playback starts
    _media_key("playpause")


# â”€â”€ Registered commands â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@register("open_apple_music")
def open_apple_music() -> str:
    _focus_or_open()
    return "Opening Apple Music."


@register("apple_music_play_song")
def apple_music_play_song(song: str) -> str:
    if not song:
        return "Please tell me which song to play."
    logger.info("Playing song: %s", song)
    _search_and_play(song)
    return f"Playing {song} on Apple Music."


@register("apple_music_play_artist")
def apple_music_play_artist(artist: str) -> str:
    if not artist:
        return "Please tell me which artist to play."
    logger.info("Playing artist: %s", artist)
    _search_and_play(artist)
    return f"Playing music by {artist} on Apple Music."


@register("apple_music_pause")
def apple_music_pause() -> str:
    """Toggle play/pause via global media key â€” no window focus needed."""
    _media_key("playpause")
    return "Toggled playback."


@register("apple_music_next_track")
def apple_music_next_track() -> str:
    _media_key("nexttrack")
    return "Next track."


@register("apple_music_previous_track")
def apple_music_previous_track() -> str:
    _media_key("prevtrack")
    return "Previous track."

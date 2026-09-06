"""Master output volume and mute for Linux, over whichever mixer exists.

Both `nora.commands.system_control` and the dashboard's slider import this and
have done since the Linux port; the module itself was never written, so every
call raised ImportError. `system_control` caught it and told the user to install
wireplumber — advice that could not help, because wireplumber was already there
— and `ui_server` caught it and logged a traceback at DEBUG on every poll of the
music card. That second one is why nora.log reached 177 MB: 40,000 tracebacks
for one missing file.

Three backends, tried in that order, because the answer differs per machine and
none of them is safe to assume:

  wpctl   PipeWire's own control. Correct on any modern desktop, and the only
          one that follows the default sink when it moves.
  pactl   PulseAudio, and PipeWire's Pulse shim when it is installed.
  amixer  ALSA. The floor — present on nearly everything, and the least aware
          of what the desktop is actually routing to.

Everything is a subprocess call rather than a binding, so there is no import to
fail and nothing to install; the cost is a fork per read, which is fine at the
once-a-second the dashboard asks for and is why `get_state` is the only one on
a hot path.

Percentages are the currency here. wpctl speaks 0.0-1.0, amixer speaks its own
mapped scale, and both are converted at the edge so callers only ever see 0-100.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess

logger = logging.getLogger("nora.platform.linux.volume")

_SINK_WP = "@DEFAULT_AUDIO_SINK@"
_SINK_PA = "@DEFAULT_SINK@"
_TIMEOUT = 2.0  # a mixer that has not answered in 2s is wedged, not slow

_backend_cache: str | None | bool = False  # False = not yet detected


def _run(args: list[str]) -> str | None:
    """Run a mixer command, returning stdout or None if it failed in any way.

    Every caller here treats "did not work" identically, so the distinction
    between a missing binary, a non-zero exit and a timeout is not worth
    propagating — but it is worth logging once at debug, because the alternative
    is a silent slider.
    """
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("%s failed: %s", args[0], exc)
        return None
    if proc.returncode != 0:
        logger.debug("%s exited %d: %s", args[0], proc.returncode, proc.stderr.strip())
        return None
    return proc.stdout


def _detect() -> str | None:
    """Pick a backend once and remember it.

    Presence on PATH is not enough on its own — a machine can ship `pactl` with
    no PulseAudio running behind it — so each candidate has to answer a read
    before it is accepted.
    """
    global _backend_cache
    if _backend_cache is not False:
        return _backend_cache  # type: ignore[return-value]

    for name, probe in (
        ("wpctl", ["wpctl", "get-volume", _SINK_WP]),
        ("pactl", ["pactl", "get-sink-volume", _SINK_PA]),
        ("amixer", ["amixer", "-M", "get", "Master"]),
    ):
        if shutil.which(name) and _run(probe) is not None:
            logger.info("Linux volume backend: %s", name)
            _backend_cache = name
            return name

    logger.warning(
        "No usable mixer found (tried wpctl, pactl, amixer) — volume control is off.",
    )
    _backend_cache = None
    return None


def available() -> bool:
    """True when some mixer answered a read. `system_control` gates on this."""
    return _detect() is not None


def get_state() -> tuple[int, bool] | None:
    """Current (volume 0-100, muted) for the default sink, or None if unreadable.

    Returning None rather than a plausible default is deliberate: the dashboard
    omits the slider when it cannot read the machine, which is honest, where a
    slider parked at 0 or 50 is a lie the user will try to drag.
    """
    backend = _detect()
    if backend == "wpctl":
        # "Volume: 0.60" or "Volume: 0.60 [MUTED]"
        out = _run(["wpctl", "get-volume", _SINK_WP])
        if not out:
            return None
        m = re.search(r"Volume:\s*([0-9.]+)", out)
        if not m:
            return None
        return round(float(m.group(1)) * 100), "[MUTED]" in out

    if backend == "pactl":
        out = _run(["pactl", "get-sink-volume", _SINK_PA])
        if not out:
            return None
        m = re.search(r"(\d+)%", out)
        if not m:
            return None
        mute_out = _run(["pactl", "get-sink-mute", _SINK_PA]) or ""
        return int(m.group(1)), "yes" in mute_out.lower()

    if backend == "amixer":
        # "Front Left: Playback 39320 [60%] [on]" — the [on]/[off] is the mute
        # flag inverted, and channels can disagree, so take the first.
        out = _run(["amixer", "-M", "get", "Master"])
        if not out:
            return None
        m = re.search(r"\[(\d+)%\]", out)
        if not m:
            return None
        return int(m.group(1)), "[off]" in out

    return None


def set_volume(pct: int) -> bool:
    """Set the default sink to an absolute 0-100. True when it took.

    Clamped here as well as in the caller because wpctl will happily amplify
    past 100% — which on most hardware means distortion, not loudness.
    """
    pct = max(0, min(100, int(pct)))
    backend = _detect()
    if backend == "wpctl":
        return _run(["wpctl", "set-volume", _SINK_WP, f"{pct / 100:.2f}"]) is not None
    if backend == "pactl":
        return _run(["pactl", "set-sink-volume", _SINK_PA, f"{pct}%"]) is not None
    if backend == "amixer":
        return _run(["amixer", "-M", "-q", "set", "Master", f"{pct}%"]) is not None
    return False


def adjust_volume(step: int) -> int | None:
    """Move the volume by a relative amount, returning the new level.

    Read-modify-write rather than the backends' own relative syntax (`5%+`), so
    that the clamp is ours and the return value is the level that was actually
    set — "turn it up" from 98 has to answer 100, not 103.
    """
    state = get_state()
    if state is None:
        return None
    target = max(0, min(100, state[0] + int(step)))
    return target if set_volume(target) else None


def set_muted(muted: bool) -> bool:
    """Set the mute flag, leaving the level underneath it alone.

    Kept distinct from `set_volume(0)` because unmuting has to restore what the
    user had, and a zeroed level has forgotten it.
    """
    backend = _detect()
    flag = "1" if muted else "0"
    if backend == "wpctl":
        return _run(["wpctl", "set-mute", _SINK_WP, flag]) is not None
    if backend == "pactl":
        return _run(["pactl", "set-sink-mute", _SINK_PA, flag]) is not None
    if backend == "amixer":
        return _run(["amixer", "-q", "set", "Master", "mute" if muted else "unmute"]) is not None
    return False

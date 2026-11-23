"""Events from devices, and the rules they set off (distributed plan Phase 8).

Devices report what happens to them as `event` frames: a call screened, the
charger plugged in, a geofence crossed. The hub hands every one to `emit`,
which queues it for a single worker thread, so a rule that locks the screen
or asks the phone something never holds up the socket.

Rules act on their own and say what they did; anything that can be put back
is recorded in `nora.reversible`, so "undo" works on it like on any command.
They replace `nora.proactive`'s "Based on your routine, should I …?", which
asked from action names and asked the same thing every Friday.

**Geofences.** The saved places (`nora.places`) are handed to the phone as
geofences when it connects and whenever a place changes; the phone watches
them itself and reports crossings, so its position never streams to the
core. Each place's last known state (inside / outside) is kept, so a
repeated or out-of-order crossing is ignored, and a crossing that arrives
late (the phone was offline) updates the state without acting on it: an
hour-old "left home" must not lock the laptop now.

**Home.** Leaving: lock the laptop, pause Spotify on it, and send the phone
a short checklist (battery low, something due today). Arriving: say welcome
on the laptop with what was missed, resume Spotify, and send the same to the
phone. Each part is switchable under `triggers.home` in config.yaml.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

from nora import store
from nora.config import get_config

logger = logging.getLogger("nora.triggers")

GEOFENCE_CAPABILITY = "phone.set_geofences"
# Capabilities the core drives itself and keeps out of the model's tool list.
# phone.take_photo is reached through look_with_phone, which also asks the
# vision model about the picture (nora.commands.camera).
INTERNAL_CAPABILITIES = frozenset({GEOFENCE_CAPABILITY, "phone.wake_alarm", "phone.take_photo"})

ENTER, EXIT = "enter", "exit"
INSIDE, OUTSIDE = "inside", "outside"


@dataclass(frozen=True)
class Event:
    device: str
    name: str
    data: dict
    at: float          # when it happened, by the device's clock (epoch seconds)


@dataclass(frozen=True)
class Crossing:
    """A geofence transition that changed what the core knew."""
    place: str
    state: str                 # INSIDE | OUTSIDE
    since: float | None        # when the previous state began; None if unknown
    act: bool                  # fresh and not flapping: rules may act on it


def _cfg() -> dict:
    return get_config().get("triggers", {}) or {}


def _home_cfg() -> dict:
    return _cfg().get("home", {}) or {}


# ── the queue ────────────────────────────────────────────────────────────────

_queue: "queue.Queue[Event | None]" = queue.Queue(maxsize=500)
_thread: threading.Thread | None = None
_speak: Callable[[str], None] | None = None


def emit(device: str, name: str, data: dict, occurred_at: float | None = None) -> None:
    """Called by the hub for every device event. Never blocks."""
    if not _cfg().get("enabled", True):
        return
    now = time.time()
    at = float(occurred_at) if isinstance(occurred_at, (int, float)) and 0 < occurred_at <= now + 300 else now
    try:
        _queue.put_nowait(Event(device, name, dict(data or {}), min(at, now)))
    except queue.Full:
        logger.warning("trigger queue full; dropped %s from %s", name, device)


def handle(ev: Event) -> None:
    """Run every rule for one event. Synchronous; the worker's body, and the tests' entry."""
    if ev.name == "battery.sample":
        from nora import battery_stats
        battery_stats.record(ev.device, ev.data, ev.at)
        return
    if ev.name == "geofence.transition":
        crossing = record_crossing(ev)
        if crossing is None:
            return
        home = str(_home_cfg().get("place", "home"))
        if crossing.place == home:
            if not crossing.act:
                logger.info("home %s noted, not acted on (late or flapping)", crossing.state)
            elif crossing.state == OUTSIDE:
                left_home(ev)
            else:
                arrived_home(ev, crossing)


def _worker() -> None:
    while True:
        ev = _queue.get()
        if ev is None:
            return
        try:
            handle(ev)
        except Exception:
            logger.exception("trigger rules failed on %s", ev.name)


def start(speak: Callable[[str], None] | None = None) -> None:
    global _thread, _speak
    _speak = speak
    if _thread is not None or not _cfg().get("enabled", True):
        return
    _thread = threading.Thread(target=_worker, daemon=True, name="nora-triggers")
    _thread.start()


def stop() -> None:
    global _thread
    if _thread is not None:
        _queue.put(None)
        _thread = None


# ── where the user is ────────────────────────────────────────────────────────

def record_crossing(ev: Event) -> Crossing | None:
    """Update the place's state from a geofence event. None when nothing changed."""
    from nora import places
    place = places.canonical(str(ev.data.get("place", "")))
    transition = str(ev.data.get("transition", ""))
    if not place or transition not in (ENTER, EXIT):
        return None
    state = INSIDE if transition == ENTER else OUTSIDE
    rows = store.query("SELECT state, changed_at FROM place_presence WHERE place = ?", (place,))
    prev = rows[0] if rows else None
    if prev is not None and (prev["state"] == state or ev.at < prev["changed_at"]):
        return None            # a repeat, or older than what is already known
    with store.transaction() as conn:
        conn.execute("INSERT INTO place_presence (place, state, changed_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(place) DO UPDATE SET state = excluded.state, changed_at = excluded.changed_at",
                     (place, state, ev.at))
    now = time.time()
    fresh = now - ev.at <= float(_cfg().get("stale_after_sec", 600))
    # GPS drift at a fence's edge can read as leaving and coming back inside a
    # minute; a crossing that soon after the last one changes the state only.
    settled = prev is None or ev.at - prev["changed_at"] >= float(_cfg().get("flap_sec", 180))
    logger.info("%s: %s (%s)", place, state, "acting" if fresh and settled else "noted")
    return Crossing(place, state, prev["changed_at"] if prev is not None else None, fresh and settled)


def assume(place: str, state: str | None) -> None:
    """Set where the user is without a crossing, setting off no rules.

    The fences carry no initial trigger, so the phone never says which side
    of a new or moved fence it is on. A place saved from the phone's own
    location is one the user is standing in; a place moved by address, or
    forgotten, is somewhere the old state no longer describes. Left alone,
    a stale "outside" swallows the first real exit as a repeat.
    """
    from nora import places
    key = places.canonical(place)
    with store.transaction() as conn:
        if state is None:
            conn.execute("DELETE FROM place_presence WHERE place = ?", (key,))
        else:
            conn.execute("INSERT INTO place_presence (place, state, changed_at) VALUES (?, ?, ?) "
                         "ON CONFLICT(place) DO UPDATE SET state = excluded.state, "
                         "changed_at = excluded.changed_at", (key, state, time.time()))
    logger.info("%s: %s (assumed)", key, state or "unknown")


def where(place: str) -> str | None:
    """INSIDE, OUTSIDE, or None when no crossing has been seen yet."""
    from nora import places
    rows = store.query("SELECT state FROM place_presence WHERE place = ?", (places.canonical(place),))
    return rows[0]["state"] if rows else None


# ── geofences on the phone ───────────────────────────────────────────────────

def fences() -> list[dict]:
    from nora import places
    radius = float(_cfg().get("geofence_radius_m", 150))
    return [{"id": p.name, "lat": p.lat, "lon": p.lon, "radius_m": radius}
            for p in places.listing() if p.lat is not None and p.lon is not None]


_told_permission = False


async def sync_device(hub, device_id: str) -> None:
    """Give one device the current fences. Runs on the hub's loop."""
    global _told_permission
    if not hub.offers(device_id, GEOFENCE_CAPABILITY):
        return
    res = await hub.invoke(device_id, GEOFENCE_CAPABILITY, {"fences": fences()})
    if res.success:
        logger.info("%s watching %s place(s)", device_id, res.data.get("fenced", "?"))
        return
    logger.warning("geofences on %s: %s", device_id, res.message)
    if res.error_code == "PERMISSION_DENIED" and not _told_permission:
        # Once per run: the phone's own message says where the setting is.
        _told_permission = True
        from nora import delivery
        delivery.deliver(res.message, device=device_id, kind="trigger")


def sync_all() -> None:
    """After a place is saved or forgotten. Any thread; fire and forget."""
    from nora.hub import server
    hub = server.get()
    if hub is None or hub.loop is None:
        return
    for device_id in hub.sessions():
        asyncio.run_coroutine_threadsafe(sync_device(hub, device_id), hub.loop)


# ── home ─────────────────────────────────────────────────────────────────────

def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _battery(device: str) -> dict:
    from nora.hub import server
    hub = server.get()
    if hub is None or hub.loop is None:
        return {}
    try:
        fut = asyncio.run_coroutine_threadsafe(hub.invoke(device, "device.status", {}), hub.loop)
        res = fut.result(timeout=10)
    except Exception as e:
        logger.debug("device.status for the leave checklist failed: %s", e)
        return {}
    return res.data if res.success else {}


def _due_line(day: date) -> str:
    from nora import task_ledger
    due = task_ledger.due_by(day)
    if not due:
        return ""
    late = [t["title"] for t in due if t["due_on"] < day.isoformat()]
    today = [t["title"] for t in due if t["due_on"] == day.isoformat()]
    out = []
    if today:
        out.append(f"Due today: {_join(today[:3])}.")
    if late:
        out.append(f"Overdue: {_join(late[:3])}.")
    return " ".join(out)


def _spotify_playing() -> bool:
    try:
        from nora.platform.linux import mpris
        return mpris.playback_status("spotify") == "Playing"
    except Exception:
        return False


def left_home(ev: Event) -> None:
    cfg = _home_cfg()
    done: list[str] = []
    if cfg.get("leave_lock_laptop", True):
        from nora.commands import system_control
        if system_control.lock_screen() == "Screen locked.":
            done.append("locked the laptop")
    if cfg.get("leave_pause_media", True) and _spotify_playing():
        from nora import reversible
        from nora.commands import spotify
        if spotify.pause_music() == "Paused.":
            done.append("paused Spotify")
            reversible.record_action("pause_music", {}, "resume_music", {},
                                     "Paused Spotify on the laptop when you left home")
    lines = ["You've left home."]
    if done:
        said = _join(done)
        lines.append(said[0].upper() + said[1:] + ".")
    if cfg.get("leave_checklist", True):
        status = _battery(ev.device)
        level = status.get("battery_percent")
        low = int(cfg.get("low_battery_pct", 30))
        if isinstance(level, (int, float)) and level < low and not status.get("charging"):
            lines.append(f"Your phone is on {int(level)}%.")
        due = _due_line(date.today())
        if due:
            lines.append(due)
    text = " ".join(lines)
    logger.info("left home: %s", text)
    from nora import delivery
    delivery.deliver(text, device=ev.device, kind="trigger")


def _away(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 60:
        return f"{m} minute{'s' if m != 1 else ''}"
    h, m = divmod(m, 60)
    return f"{h} hour{'s' if h != 1 else ''}" + (f" {m} minute{'s' if m != 1 else ''}" if m else "")


def arrived_home(ev: Event, crossing: Crossing) -> None:
    cfg = _home_cfg()
    from nora import calls
    quiet = calls.quiet_now()
    lines = ["Welcome home."]
    if crossing.since is not None:
        lines[0] = f"Welcome home. You were out {_away(ev.at - crossing.since)}."
    if cfg.get("arrive_brief", True):
        hours = min(24.0, max(1.0, (ev.at - crossing.since) / 3600)) if crossing.since else 12.0
        try:
            from nora import missed_calls
            missed = missed_calls.briefing_line(hours)
        except Exception as e:
            logger.debug("missed calls for the welcome failed: %s", e)
            missed = ""
        if missed:
            lines.append(missed)
        due = _due_line(date.today())
        if not due:
            from nora import task_ledger
            tomorrow = [t["title"] for t in task_ledger.due_that_day(date.today() + timedelta(days=1))]
            due = f"Due tomorrow: {_join(tomorrow[:3])}." if tomorrow else ""
        if due:
            lines.append(due)
    from nora import delivery, focus
    # Said first, then the music: speech over a song that just started is lost.
    # Arriving is exactly when focus reads "away", so its gate would hold this
    # back; only quiet hours and a call in progress keep the laptop silent.
    if _speak is not None and not quiet and focus.current() != focus.FocusState.CALL:
        try:
            _speak(" ".join(lines))
        except Exception as e:
            logger.debug("welcome speech failed: %s", e)
    if cfg.get("arrive_resume_music", True) and not quiet and not _spotify_playing():
        from nora import reversible
        from nora.commands import spotify
        if spotify.resume_music().startswith(("Resum", "Playing")):
            reversible.record_action("resume_music", {}, "pause_music", {},
                                     "Resumed Spotify on the laptop when you got home")
            lines.append("I've put your music back on the laptop. Say undo to stop it.")
    text = " ".join(lines)
    logger.info("arrived home: %s", text)
    delivery.deliver(text, device=ev.device, kind="trigger")


def reset_for_tests() -> None:
    global _told_permission
    _told_permission = False
    while not _queue.empty():
        _queue.get_nowait()

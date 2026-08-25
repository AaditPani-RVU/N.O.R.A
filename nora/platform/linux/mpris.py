"""MPRIS2 client for Linux media players — synchronous, dependency-tolerant.

Talks to any org.mpris.MediaPlayer2.* service on the session bus. NORA uses it
for Spotify, but nothing here is Spotify-specific.

Two transports, tried in order:
  1. python-dbus (``import dbus``) — typed, fast, no subprocess.
  2. ``gdbus`` subprocess — glib ships on every desktop, so this is the
     always-there fallback. Its GVariant text output is parsed by _gv_parse.

Everything is synchronous: NORA's command handlers run in a thread pool
(see command_engine.execute), so blocking here is correct and keeps the
call sites simple.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from typing import Any

logger = logging.getLogger("nora.platform.mpris")

BUS_PREFIX = "org.mpris.MediaPlayer2."
OBJECT_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
ROOT_IFACE = "org.mpris.MediaPlayer2"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

_GDBUS_TIMEOUT = 5.0


class MprisError(RuntimeError):
    """Raised when no transport can reach the player."""


class ObjectPath(str):
    """A string that D-Bus must see as an object path, not a plain string.

    SetPosition takes the track's object path as its first argument, and both
    transports need to be told: python-dbus wants a dbus.ObjectPath instance,
    gdbus wants the literal prefixed with ``objectpath``. Passing a bare str
    gets it rejected as a signature mismatch by one and silently mistyped by
    the other, so the intent is carried on the value itself.
    """


# ── Transport 1: python-dbus ──────────────────────────────────────────────────

def _session_bus() -> Any:
    """Return a cached python-dbus SessionBus, or None if unavailable.

    A failed connection is cached as False so we attempt it only once per
    process; callers always get None in that case, never the sentinel.
    """
    global _bus
    if _bus is False:
        return None
    if _bus is not None:
        return _bus
    try:
        import dbus  # type: ignore
        _bus = dbus.SessionBus()
    except Exception as exc:
        logger.debug("python-dbus unavailable (%s); falling back to gdbus", exc)
        _bus = False
        return None
    return _bus


_bus: Any = None


def _dbus_iface(service: str, interface: str) -> Any:
    import dbus  # type: ignore
    bus = _session_bus()
    if bus is None:
        return None
    proxy = bus.get_object(service, OBJECT_PATH)
    return dbus.Interface(proxy, interface)


# ── Transport 2: gdbus subprocess ─────────────────────────────────────────────

def _gdbus(args: list[str]) -> tuple[bool, str]:
    """Run a gdbus command. Returns (ok, stdout-or-error)."""
    if not shutil.which("gdbus"):
        return False, "gdbus not installed"
    try:
        proc = subprocess.run(
            ["gdbus", *args],
            capture_output=True, text=True, timeout=_GDBUS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, "gdbus timed out"
    except Exception as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()
    return True, proc.stdout.strip()


# ── GVariant text parser ──────────────────────────────────────────────────────
#
# gdbus prints GVariant literals, e.g.
#   (<'Playing'>,)
#   (<{'xesam:title': <'When the Sun Hits'>, 'mpris:length': <uint64 285906000>}>,)
# Only the subset MPRIS actually emits is supported: tuples, variants, dicts,
# arrays, strings, object paths, booleans and numbers.

_TYPE_PREFIX = re.compile(
    r"^(uint64|uint32|uint16|int64|int32|int16|byte|handle|double|objectpath|signature)\s+"
)
_NUMBER = re.compile(r"^-?\d+(\.\d+)?([eE][+-]?\d+)?")


class _GvCursor:
    def __init__(self, text: str) -> None:
        self.s = text
        self.i = 0

    def skip(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \t\n\r,":
            self.i += 1

    def peek(self) -> str:
        return self.s[self.i] if self.i < len(self.s) else ""

    def parse(self) -> Any:
        self.skip()
        if self.i >= len(self.s):
            return None

        # Strip a leading type annotation ("uint64 123", "objectpath '/x'").
        m = _TYPE_PREFIX.match(self.s[self.i:])
        if m:
            self.i += m.end()
            self.skip()

        ch = self.peek()

        if ch == "<":                      # variant
            self.i += 1
            val = self.parse()
            self.skip()
            if self.peek() == ">":
                self.i += 1
            return val

        if ch == "(":                      # tuple
            self.i += 1
            items = []
            while True:
                self.skip()
                if self.peek() in (")", ""):
                    self.i += 1
                    break
                items.append(self.parse())
            return items

        if ch == "[":                      # array
            self.i += 1
            items = []
            while True:
                self.skip()
                if self.peek() in ("]", ""):
                    self.i += 1
                    break
                items.append(self.parse())
            return items

        if ch == "{":                      # dict
            self.i += 1
            out: dict[Any, Any] = {}
            while True:
                self.skip()
                if self.peek() in ("}", ""):
                    self.i += 1
                    break
                key = self.parse()
                self.skip()
                if self.peek() == ":":
                    self.i += 1
                out[key] = self.parse()
            return out

        if ch in ("'", '"'):               # string
            quote = ch
            self.i += 1
            buf = []
            while self.i < len(self.s):
                c = self.s[self.i]
                if c == "\\" and self.i + 1 < len(self.s):
                    buf.append(self.s[self.i + 1])
                    self.i += 2
                    continue
                if c == quote:
                    self.i += 1
                    break
                buf.append(c)
                self.i += 1
            return "".join(buf)

        if self.s.startswith("true", self.i):
            self.i += 4
            return True
        if self.s.startswith("false", self.i):
            self.i += 5
            return False
        if self.s.startswith("nothing", self.i):
            self.i += 7
            return None

        m = _NUMBER.match(self.s[self.i:])
        if m:
            self.i += m.end()
            raw = m.group(0)
            return float(raw) if ("." in raw or "e" in raw.lower()) else int(raw)

        # Unrecognised token — consume it so we never spin.
        start = self.i
        while self.i < len(self.s) and self.s[self.i] not in " \t\n\r,)]}>":
            self.i += 1
        return self.s[start:self.i] or None


def _gv_parse(text: str) -> Any:
    """Parse gdbus GVariant output, unwrapping the outer 1-tuple if present."""
    try:
        val = _GvCursor(text).parse()
    except Exception as exc:
        logger.debug("GVariant parse failed on %r: %s", text[:120], exc)
        return None
    if isinstance(val, list) and len(val) == 1:
        return val[0]
    return val


def _py(value: Any) -> Any:
    """Convert python-dbus typed values into plain Python."""
    try:
        import dbus  # type: ignore
    except Exception:
        return value
    if isinstance(value, dbus.String) or isinstance(value, dbus.ObjectPath):
        return str(value)
    if isinstance(value, dbus.Boolean):
        return bool(value)
    if isinstance(value, (dbus.Int16, dbus.Int32, dbus.Int64,
                          dbus.UInt16, dbus.UInt32, dbus.UInt64, dbus.Byte)):
        return int(value)
    if isinstance(value, dbus.Double):
        return float(value)
    if isinstance(value, dbus.Array):
        return [_py(v) for v in value]
    if isinstance(value, dbus.Dictionary):
        return {str(k): _py(v) for k, v in value.items()}
    return value


# ── Player discovery ──────────────────────────────────────────────────────────

def list_players() -> list[str]:
    """Return every MPRIS bus name currently on the session bus."""
    bus = _session_bus()
    if bus is not None:
        try:
            return sorted(
                str(n) for n in bus.list_names() if str(n).startswith(BUS_PREFIX)
            )
        except Exception as exc:
            logger.debug("python-dbus list_names failed: %s", exc)

    ok, out = _gdbus([
        "call", "--session",
        "--dest", "org.freedesktop.DBus",
        "--object-path", "/org/freedesktop/DBus",
        "--method", "org.freedesktop.DBus.ListNames",
    ])
    if not ok:
        return []
    names = _gv_parse(out)
    if not isinstance(names, list):
        return []
    return sorted(n for n in names if isinstance(n, str) and n.startswith(BUS_PREFIX))


def service_for(player: str = "spotify") -> str:
    """Full bus name for a short player id ('spotify' -> org.mpris...spotify)."""
    return player if player.startswith(BUS_PREFIX) else BUS_PREFIX + player


def is_running(player: str = "spotify") -> bool:
    """True when the player currently owns its MPRIS bus name."""
    target = service_for(player)
    names = list_players()
    # Spotify sometimes registers an instance suffix (…spotify.instance1234).
    return any(n == target or n.startswith(target + ".") for n in names)


def resolve(player: str = "spotify") -> str | None:
    """Return the live bus name for a player, honouring instance suffixes."""
    target = service_for(player)
    for name in list_players():
        if name == target or name.startswith(target + "."):
            return name
    return None


# ── Core operations ───────────────────────────────────────────────────────────

def call(method: str, args: list[Any] | None = None,
         player: str = "spotify", interface: str = PLAYER_IFACE) -> tuple[bool, Any]:
    """Invoke an MPRIS method. Returns (ok, reply-or-error-message)."""
    args = args or []
    service = resolve(player)
    if service is None:
        return False, f"{player} is not running."

    bus = _session_bus()
    if bus is not None:
        try:
            iface = _dbus_iface(service, interface)
            if iface is not None:
                return True, _py(getattr(iface, method)(*_typed_args(args)))
        except Exception as exc:
            logger.debug("python-dbus %s failed (%s); trying gdbus", method, exc)

    cmd = [
        "call", "--session",
        "--dest", service,
        "--object-path", OBJECT_PATH,
        "--method", f"{interface}.{method}",
    ]
    cmd += [_gv_literal(a) for a in args]
    ok, out = _gdbus(cmd)
    return (True, _gv_parse(out)) if ok else (False, out)


def _typed_args(args: list[Any]) -> list[Any]:
    """Re-type the arguments python-dbus cannot infer. Currently object paths."""
    if not any(isinstance(a, ObjectPath) for a in args):
        return args
    try:
        import dbus  # type: ignore
    except Exception:
        return args
    return [dbus.ObjectPath(a) if isinstance(a, ObjectPath) else a for a in args]


def _gv_literal(value: Any) -> str:
    """Render a Python value as a gdbus command-line argument."""
    if isinstance(value, ObjectPath):
        return f"objectpath '{value}'"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return f"'{str(value)}'"


def get_prop(name: str, player: str = "spotify",
             interface: str = PLAYER_IFACE) -> Any:
    """Read an MPRIS property. Returns None when unavailable."""
    service = resolve(player)
    if service is None:
        return None

    bus = _session_bus()
    if bus is not None:
        try:
            props = _dbus_iface(service, PROPS_IFACE)
            if props is not None:
                return _py(props.Get(interface, name))
        except Exception as exc:
            logger.debug("python-dbus Get(%s) failed (%s); trying gdbus", name, exc)

    ok, out = _gdbus([
        "call", "--session",
        "--dest", service,
        "--object-path", OBJECT_PATH,
        "--method", f"{PROPS_IFACE}.Get",
        interface, name,
    ])
    return _gv_parse(out) if ok else None


def set_prop(name: str, value: Any, signature: str, player: str = "spotify",
             interface: str = PLAYER_IFACE) -> bool:
    """Write an MPRIS property. *signature* is the D-Bus type code ('d', 'b', 's')."""
    service = resolve(player)
    if service is None:
        return False

    bus = _session_bus()
    if bus is not None:
        try:
            import dbus  # type: ignore
            props = _dbus_iface(service, PROPS_IFACE)
            if props is not None:
                typed = {
                    "d": lambda v: dbus.Double(float(v)),
                    "b": lambda v: dbus.Boolean(bool(v)),
                    "s": lambda v: dbus.String(str(v)),
                    "x": lambda v: dbus.Int64(int(v)),
                }.get(signature, lambda v: v)(value)
                props.Set(interface, name, typed)
                return True
        except Exception as exc:
            logger.debug("python-dbus Set(%s) failed (%s); trying gdbus", name, exc)

    if signature == "d":
        literal = f"<{float(value)}>"
    elif signature == "b":
        literal = f"<{'true' if value else 'false'}>"
    elif signature == "x":
        literal = f"<int64 {int(value)}>"
    else:
        literal = f"<'{value}'>"

    ok, _ = _gdbus([
        "call", "--session",
        "--dest", service,
        "--object-path", OBJECT_PATH,
        "--method", f"{PROPS_IFACE}.Set",
        interface, name, literal,
    ])
    return ok


# ── Convenience readers ───────────────────────────────────────────────────────

def playback_status(player: str = "spotify") -> str:
    """'Playing' | 'Paused' | 'Stopped' | '' when the player is not running."""
    val = get_prop("PlaybackStatus", player)
    return str(val) if isinstance(val, str) else ""


def metadata(player: str = "spotify") -> dict[str, Any]:
    """Raw MPRIS metadata dict (xesam:title, xesam:artist, …)."""
    val = get_prop("Metadata", player)
    return val if isinstance(val, dict) else {}


def now_playing(player: str = "spotify") -> dict[str, Any]:
    """Normalised now-playing snapshot.

    Keys: title, artist, album, uri, url, art_url, length_sec, status.
    Every key is always present; missing values are empty.
    """
    meta = metadata(player)
    artists = meta.get("xesam:artist") or []
    if isinstance(artists, str):
        artists = [artists]

    length = meta.get("mpris:length") or 0
    try:
        length_sec = int(length) / 1_000_000  # MPRIS length is microseconds
    except (TypeError, ValueError):
        length_sec = 0.0

    trackid = str(meta.get("mpris:trackid") or "")
    uri = ""
    if "/track/" in trackid:                    # /com/spotify/track/<id>
        uri = "spotify:track:" + trackid.rsplit("/", 1)[-1]

    return {
        "title": str(meta.get("xesam:title") or ""),
        "artist": ", ".join(str(a) for a in artists),
        "album": str(meta.get("xesam:album") or ""),
        "uri": uri,
        "url": str(meta.get("xesam:url") or ""),
        "art_url": str(meta.get("mpris:artUrl") or ""),
        "length_sec": length_sec,
        "status": playback_status(player),
    }


def position_sec(player: str = "spotify") -> float:
    """Playback position in seconds, or 0.0 when the player will not say."""
    pos = get_prop("Position", player)          # MPRIS reports microseconds
    try:
        return float(pos) / 1_000_000 if pos is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def seek_to(position_sec_target: float, player: str = "spotify") -> bool:
    """Jump to an absolute position in the current track.

    Two ways in, because players disagree about which one works. SetPosition
    is the correct call and takes the track's object path, which guards it
    against landing on the *next* track if the song changed mid-request.
    Spotify's Linux client has shipped builds where it is a no-op, so the
    position is read back and a relative Seek covers the gap when it is.

    Returns True once the player is actually near the requested spot.
    """
    meta = metadata(player)
    if not meta:
        return False

    try:
        length_sec = float(meta.get("mpris:length") or 0) / 1_000_000
    except (TypeError, ValueError):
        length_sec = 0.0
    target = max(0.0, float(position_sec_target))
    # Stop a drag to the far end from tripping the track change: land just
    # inside the track rather than exactly on its last microsecond.
    if length_sec > 0:
        target = min(target, max(0.0, length_sec - 1.0))

    def landed() -> bool:
        return abs(position_sec(player) - target) <= 2.0

    trackid = str(meta.get("mpris:trackid") or "")
    if trackid.startswith("/"):
        ok, err = call("SetPosition", [ObjectPath(trackid), int(target * 1_000_000)],
                       player=player)
        if ok and landed():
            return True
        if not ok:
            logger.debug("SetPosition failed on %s (%s); trying relative Seek",
                         player, err)

    offset = target - position_sec(player)
    ok, err = call("Seek", [int(offset * 1_000_000)], player=player)
    if not ok:
        logger.warning("seek on %s failed: %s", player, err)
        return False
    return landed()


# ── Launching ─────────────────────────────────────────────────────────────────

def launch(command: str = "spotify", player: str = "spotify",
           timeout: float = 15.0) -> bool:
    """Start the player and block until it claims its MPRIS name.

    Returns True once the bus name appears, False on timeout or if the
    binary is missing.
    """
    if is_running(player):
        return True
    if not shutil.which(command):
        logger.warning("%s binary not found on PATH", command)
        return False

    try:
        subprocess.Popen(
            [command],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        logger.error("Failed to launch %s: %s", command, exc)
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running(player):
            logger.info("%s came up on the session bus", player)
            return True
        time.sleep(0.4)

    logger.warning("%s did not claim its MPRIS name within %.0fs", player, timeout)
    return False

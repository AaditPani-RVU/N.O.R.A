from __future__ import annotations

"""Lightweight HTTP server that serves the NORA dashboard UI.

HTTP endpoints
--------------
GET  /           → index.html
GET  /geo.js     → globe coastline/border geometry for the location takeover
GET  /state      → {speaking, text, status, ptt_mode, user_text, user_seq, reply_seq}
GET  /metrics    → system vitals
GET  /music      → {track, artist, source, status}
GET  /processes  → live process table for the process constellation
GET  /memory_graph → memory embeddings projected to a sphere
GET  /takeovers.js → process + memory constellation renderers
POST /proc_kill  → terminate a pid picked out of the constellation
POST /ptt        → push-to-talk button control
POST /music_ctl  → dispatch playback controls from the UI (play/pause/next/prev/seek/volume)

Authenticated WebSocket API (Sprint 5)
---------------------------------------
ws://<host>:<port>  (default port 8765)

Auth — if NORA_API_TOKEN is set in .env, clients must authenticate within 5 s:
  Client → {"type": "auth", "token": "<token>"}      (first message)
  or supply ?token=<token> in the connect URL.
  Server → {"type": "auth_ok"} or {"type": "error", "message": "Unauthorized"}

Client → NORA messages:
  {"type": "command", "text": "open chrome"}     typed voice command
  {"type": "ptt_start"}                          begin PTT
  {"type": "ptt_end"}                            end PTT
  {"type": "ping"}                               keepalive

NORA → Client push:
  {"type": "state",        "data": {...}}         on every state change
  {"type": "stage",        "stage": "listening"}  pipeline stage update
  {"type": "user",         "text": "open chrome"}  utterance NORA just heard
  {"type": "notification", "message": "..."}      proactive alerts
  {"type": "pong"}                                reply to ping
"""

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from nora import context

try:
    import psutil as _psutil
except ImportError:
    _psutil = None

try:
    import websockets  # type: ignore[import]
    _HAS_WS = True
except ImportError:
    _HAS_WS = False

logger = logging.getLogger("nora.ui_server")

# ``user_seq``/``reply_seq`` are monotonic counters, not decoration: the UI
# transcript needs to tell "NORA repeated herself" from "nothing new arrived",
# and comparing text alone cannot. HTTP pollers diff the counters too.
_state: dict = {
    "speaking": False,
    "text": "",
    "status": "STANDBY",
    "stage": "idle",
    "user_text": "",
    "user_seq": 0,
    "reply_seq": 0,
    "location": None,
    "location_seq": 0,
}
_lock = threading.Lock()

# Push-to-talk state -- set by the UI button or WebSocket client, read by listener.py
_ptt_event = threading.Event()

# WebSocket state
_ws_clients: set = set()
_ws_lock = threading.Lock()
_ws_loop: asyncio.AbstractEventLoop | None = None


def is_ptt_pressed() -> bool:
    """Return True while the UI push-to-talk button is held."""
    return _ptt_event.is_set()

_STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def notify(speaking: bool, text: str = "", status: str = "", stage: str = "") -> None:
    """Update UI state. Thread-safe -- call from anywhere."""
    with _lock:
        _state["speaking"] = speaking
        if text:
            _state["text"] = text
            _state["reply_seq"] = int(_state.get("reply_seq", 0)) + 1
        if status:
            _state["status"] = status
        if stage:
            _state["stage"] = stage
    # Push state snapshot to connected WebSocket clients
    ws_push({"type": "state", "data": {**_state, "ptt_mode": "on" if context.get_ptt_enabled() else "off"}})


def notify_stage(stage: str) -> None:
    """Update just the pipeline stage indicator. Thread-safe."""
    with _lock:
        _state["stage"] = stage
    ws_push({"type": "stage", "stage": stage})


def notify_user(text: str) -> None:
    """Mirror an utterance NORA just heard (or was typed) to the dashboard.

    Called before the input guard runs, so a blocked command still shows up in
    the transcript next to the refusal instead of vanishing silently.
    """
    text = (text or "").strip()
    if not text:
        return
    with _lock:
        _state["user_text"] = text
        seq = _state["user_seq"] = int(_state.get("user_seq", 0)) + 1
    ws_push({"type": "user", "text": text, "seq": seq})


def notify_location(name: str, country: str, lat: float, lon: float) -> None:
    """Push a geocoded place for the dashboard's orb-to-globe morph.

    Rides the same _state snapshot as notify()/the /state poll (rather than
    a bespoke WS-only message type) so it reaches the dashboard whether the
    client has a live WebSocket or has fallen back to HTTP polling — both
    read applyState()/_serve_state() off the same dict.
    """
    with _lock:
        _state["location"] = {"name": name, "country": country, "lat": lat, "lon": lon}
        _state["location_seq"] = int(_state.get("location_seq", 0)) + 1
        snapshot = {**_state, "ptt_mode": "on" if context.get_ptt_enabled() else "off"}
    ws_push({"type": "state", "data": snapshot})


def notify_takeover(kind: str, **extra: Any) -> None:
    """Ask the dashboard to morph the orb into one of the takeovers.

    *kind* is "processes", "memory", or "off" to dismiss whatever is up.
    Rides the shared _state snapshot with its own monotonic seq for exactly
    the reason notify_location() does: it then reaches the page over a live
    WebSocket and over the HTTP polling fallback without a second code path.
    """
    with _lock:
        _state["takeover"] = {"kind": kind, **extra}
        _state["takeover_seq"] = int(_state.get("takeover_seq", 0)) + 1
        snapshot = {**_state, "ptt_mode": "on" if context.get_ptt_enabled() else "off"}
    ws_push({"type": "state", "data": snapshot})


def notify_ptt_mode(enabled: bool) -> None:
    """Mirror PTT-mode changes to the dashboard immediately."""
    with _lock:
        _state["ptt_mode_changed_at"] = enabled


def start(port: int = 8766) -> str:
    """Start the HTTP server in a daemon thread. Returns the URL.

    The URL carries NORA_API_TOKEN when one is set. The routes that
    drive the desktop demand it, and the tab we open here is the one
    the operator actually uses — without the token it would be the only
    client that cannot use them. It is also a distinct origin from the
    LAN address a phone uses, so it gets no help from that tab having
    authenticated: localStorage does not cross origins.
    """
    import os
    import urllib.parse

    server = HTTPServer(("0.0.0.0", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="nora-ui")
    thread.start()
    url = f"http://localhost:{port}"
    token = os.environ.get("NORA_API_TOKEN", "")
    logger.info("NORA UI server started at %s", url)
    if token:
        url += "/?token=" + urllib.parse.quote(token, safe="")
    return url


# ---------------------------------------------------------------------------
# Authenticated WebSocket API (Sprint 5)
# ---------------------------------------------------------------------------

def ws_push(msg: dict) -> None:
    """Broadcast a JSON message to all connected WebSocket clients (fire-and-forget)."""
    if not _ws_clients or not _ws_loop or _ws_loop.is_closed():
        return
    data = json.dumps(msg)
    with _ws_lock:
        clients = set(_ws_clients)
    if not clients:
        return

    async def _broadcast() -> None:
        for ws in clients:
            try:
                await ws.send(data)
            except Exception:
                pass

    try:
        asyncio.run_coroutine_threadsafe(_broadcast(), _ws_loop)
    except Exception:
        pass


def has_ws_clients() -> bool:
    """True when at least one client is connected. Lets callers skip expensive
    payload work (base64-encoding TTS audio) when nobody is on the other end."""
    with _ws_lock:
        return bool(_ws_clients)


def ws_notify(message: str) -> None:
    """Push a proactive notification string to all connected WebSocket clients."""
    ws_push({"type": "notification", "message": message})


async def _ws_connection_handler(websocket: Any, token: str) -> None:
    """Handle a single authenticated WebSocket connection."""
    import urllib.parse

    # ── Auth ──────────────────────────────────────────────────────────────
    if token:
        # 1. Try query-string token: ws://host:port?token=xxx
        # websockets >= 14 moved the request line to `.request.path`; the old
        # `.path` attribute is gone. Reading only the old one silently yielded
        # "" here, so every query-string auth fell through to the 5 s
        # first-message wait and then failed as Unauthorized.
        raw_path = ""
        for getter in (lambda: websocket.request.path, lambda: websocket.path):
            try:
                raw_path = getter() or ""
                if raw_path:
                    break
            except Exception:
                continue
        try:
            params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(raw_path).query))
            provided = params.get("token", "")
        except Exception:
            provided = ""

        # 2. Fall back to first-message auth packet
        if not provided:
            try:
                first = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                first_data = json.loads(first)
                if first_data.get("type") == "auth":
                    provided = first_data.get("token", "")
            except Exception:
                pass

        if provided != token:
            try:
                await websocket.send(json.dumps({"type": "error", "message": "Unauthorized"}))
            except Exception:
                pass
            return

    await websocket.send(json.dumps({"type": "auth_ok"}))

    # ── Register ──────────────────────────────────────────────────────────
    with _ws_lock:
        _ws_clients.add(websocket)

    # Send current state snapshot immediately
    with _lock:
        snapshot = dict(_state)
    snapshot["ptt_mode"] = "on" if context.get_ptt_enabled() else "off"
    try:
        await websocket.send(json.dumps({"type": "state", "data": snapshot}))
    except Exception:
        pass

    # ── Message loop ──────────────────────────────────────────────────────
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg_type = msg.get("type", "")
            if msg_type == "command":
                text = (msg.get("text") or "").strip()
                if text:
                    from nora import text_input
                    text_input._queue.put(text)
            elif msg_type == "ptt_start":
                _ptt_event.set()
            elif msg_type == "ptt_end":
                _ptt_event.clear()
            elif msg_type == "ping":
                try:
                    await websocket.send(json.dumps({"type": "pong"}))
                except Exception:
                    pass
    finally:
        with _ws_lock:
            _ws_clients.discard(websocket)


def start_ws(port: int = 8765, token: str = "") -> str | None:
    """Start the authenticated WebSocket server in a daemon thread.

    Returns the server URL (ws://0.0.0.0:<port>) or None if websockets is
    not installed.  Install with: ``pip install websockets``
    """
    if not _HAS_WS:
        logger.warning(
            "websockets library not installed — WebSocket API disabled. "
            "Run: pip install websockets"
        )
        return None

    global _ws_loop

    async def _serve() -> None:
        async def _handler(ws: Any, *_: Any) -> None:
            await _ws_connection_handler(ws, token)

        async with websockets.serve(_handler, "0.0.0.0", port):  # type: ignore[attr-defined]
            await asyncio.Future()  # run forever

    def _run() -> None:
        global _ws_loop
        loop = asyncio.new_event_loop()
        _ws_loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_serve())
        finally:
            loop.close()

    thread = threading.Thread(target=_run, daemon=True, name="nora-ws")
    thread.start()
    url = f"ws://0.0.0.0:{port}"
    logger.info("NORA WebSocket API started at %s", url)
    return url


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

def _position_sec(player: str) -> float:
    """Playback position in seconds, or 0.0 when the player won't say."""
    try:
        from nora.platform.linux import mpris
        return mpris.position_sec(player)
    except Exception:
        return 0.0


def _system_audio_state() -> dict:
    """Default-sink volume and mute flag for the dashboard slider.

    Reported alongside the track so the slider shows what the machine is
    actually doing, including changes made outside NORA.
    """
    try:
        from nora.platform.linux import volume as _vol
        state = _vol.get_state()
        if state is not None:
            return {"volume": state[0], "muted": state[1]}
    except Exception:
        logger.debug("system volume read failed", exc_info=True)
    return {}


def _live_music_state() -> dict:
    """Track state plus the system audio state the dashboard slider needs."""
    state = _player_music_state()
    state.update(_system_audio_state())
    return state


def _player_music_state() -> dict:
    """Music state for the UI, read from Spotify rather than from memory.

    context.music only moves when NORA itself issues a command, so it goes
    stale the moment you skip a track in Spotify's own window. Spotify is the
    source of truth now and MPRIS is cheap to poll, so read it directly and
    keep context in step. Falls back to the cached state if the bus is
    unreachable — a stale widget beats a broken endpoint.
    """
    try:
        from nora.platform.linux import mpris
        if mpris.is_running("spotify"):
            snap = mpris.now_playing("spotify")
            if snap.get("title"):
                status = (snap.get("status") or "").lower()
                context.update_music(
                    track=snap["title"], artist=snap.get("artist", ""),
                    source="spotify", status=status,
                )
                return {
                    "track": snap["title"],
                    "artist": snap.get("artist", ""),
                    "source": "spotify",
                    "status": status,
                    "album": snap.get("album", ""),
                    "art_url": snap.get("art_url", ""),
                    "length_sec": snap.get("length_sec", 0.0),
                    "position_sec": _position_sec("spotify"),
                }
    except Exception as exc:
        logger.debug("Live music read failed (%s); using cached state", exc)
    return context.get_music()


class _Handler(BaseHTTPRequestHandler):
    @property
    def route(self) -> str:
        """Path with the query string stripped.

        `self.path` is the raw request target, query string included, so routing
        on it directly means `/?token=abc` matches no branch and 404s. That is
        exactly the URL a remote client uses to pass NORA_API_TOKEN, so the
        dashboard was unreachable from anywhere that needed to authenticate.
        """
        import urllib.parse
        return urllib.parse.urlparse(self.path).path or "/"

    def do_GET(self) -> None:
        route = self.route
        if route == "/state":
            self._serve_state()
        elif route == "/metrics":
            self._serve_metrics()
        elif route == "/music":
            self._serve_music()
        elif route == "/history":
            self._serve_history()
        elif route == "/analytics":
            self._serve_analytics()
        elif route == "/processes":
            self._serve_processes()
        elif route == "/memory_graph":
            self._serve_memory_graph()
        elif route == "/desktop":
            self._serve_desktop()
        elif route in ("/", "/index.html"):
            self._serve_file(_STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif route == "/takeovers.js":
            # The process and memory constellations.
            # Split out of index.html for the same reason geo.js is: that
            # file is long enough already. Still same-origin.
            self._serve_file(_STATIC_DIR / "takeovers.js",
                             "application/javascript; charset=utf-8")
        elif route == "/remote.js":
            # The desktop remote's renderer. Split out for the same reason
            # takeovers.js and geo.js are — index.html is long enough.
            self._serve_file(_STATIC_DIR / "remote.js",
                             "application/javascript; charset=utf-8")
        elif route == "/geo.js":
            # Globe coastline/border geometry. Split out of index.html only
            # for that file's sake -- still same-origin, so the dashboard
            # keeps working with no external host reachable.
            self._serve_file(_STATIC_DIR / "geo.js", "application/javascript; charset=utf-8")
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self) -> None:  # CORS preflight
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        route = self.route
        if route == "/ptt":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
            if body.get("action") == "press":
                _ptt_event.set()
            else:
                _ptt_event.clear()
            self._json_ok(b"{}")
        elif route == "/music_ctl":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
            self._dispatch_music(body)
            self._json_ok(b"{}")
        elif route == "/interrupt":
            # Same stop_all() the "stop" voice phrase reaches, so the dashboard
            # button and the spoken interrupt cannot drift apart.
            try:
                from nora.commands.interrupt import stop_all
                stop_all()
            except Exception:
                logger.exception("ui: /interrupt failed")
            self._json_ok(b"{}")
        elif route == "/proc_kill":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
            from nora import visuals
            result = visuals.kill_process(body.get("pid", 0), bool(body.get("force")))
            logger.info("ui: /proc_kill %s -> %s", body.get("pid"), result)
            self._json_ok(json.dumps(result).encode())
        elif route == "/desktop_act":
            # Drives the user's actual desktop, so unlike /proc_kill this one
            # refuses to run without the token when one is configured.
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
            if not self._token_ok():
                self._json_denied()
                return
            from nora import desktop
            result = desktop.act(
                str(body.get("snap") or ""),
                body.get("id"),
                str(body.get("action") or "click"),
                str(body.get("text") or ""),
            )
            logger.info("ui: /desktop_act %s#%s %s -> %s",
                        body.get("action"), body.get("id"),
                        result.get("label", ""), result.get("ok"))
            self._json_ok(json.dumps(result).encode())
        elif route == "/type_command":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
            text = (body.get("text") or "").strip()
            if text:
                print(f"[NORA UI] /type_command received: {text!r}", flush=True)
                from nora import text_input
                text_input._queue.put(text)
                print(f"[NORA UI] Queued. Queue size now: {text_input._queue.qsize()}", flush=True)
            self._json_ok(b"{}")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def _token_ok(self) -> bool:
        """True when the caller carries NORA_API_TOKEN, or none is configured.

        Same rule the WebSocket applies: unset means localhost-only trust,
        set means every reachable client has to prove it. Accepts the token
        on the query string (the form the dashboard is already opened with)
        or as a bearer header for anything scripted.
        """
        import urllib.parse
        from nora import security

        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        provided = (params.get("token") or [""])[0]
        if not provided:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                provided = auth[7:]
        return security.check_api_token(provided)

    def _json_denied(self) -> None:
        payload = json.dumps({"ok": False, "error": "unauthorised"}).encode()
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def _json_ok(self, payload: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def _dispatch_music(self, body: dict) -> None:
        """Run a playback control on a background thread so HTTP returns fast."""
        act = (body.get("action") or "").lower()

        def _run() -> None:
            try:
                from nora.commands import spotify as sp
                if act == "play":
                    sp.resume_music()
                elif act == "pause":
                    sp.pause_music()
                elif act == "toggle":
                    sp.toggle_music()
                elif act == "stop":
                    sp.stop_music()
                elif act == "next":
                    sp.next_track()
                elif act == "prev":
                    sp.previous_track()
                elif act == "seek":
                    # Absolute seconds from the dashboard's scrub bar. The UI
                    # already clamps to the track length; the player clamps
                    # again, so a stale length can't overshoot into the next
                    # track.
                    sp.seek_track(float(body.get("position", 0)))
                elif act == "volume":
                    # Drives the *system* sink, not Spotify's own volume. MPRIS
                    # volume is a fraction of the system level, so a slider
                    # wired to it can never get louder than whatever the desktop
                    # mixer already allows -- pinning at 100% of, say, 60% and
                    # looking broken, which is exactly how it behaved.
                    from nora.commands.system_control import set_volume
                    set_volume(int(body.get("level", 50)))
                elif act == "mute":
                    from nora.commands.system_control import mute_audio
                    mute_audio(bool(body.get("muted", True)))
                elif act == "spotify_volume":
                    # Still reachable for anyone who wants app-level balance.
                    sp.spotify_set_volume(int(body.get("level", 50)))
            except Exception as exc:
                logger.warning("music_ctl %s failed: %s", act, exc)

        threading.Thread(target=_run, daemon=True, name=f"music-ctl-{act}").start()

    def _serve_state(self) -> None:
        with _lock:
            payload = dict(_state)
        # Live PTT mode (read from context, not stored in _state)
        payload["ptt_mode"] = "on" if context.get_ptt_enabled() else "off"
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_music(self) -> None:
        body = json.dumps(_live_music_state()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_metrics(self) -> None:
        if _psutil is not None:
            cpu  = _psutil.cpu_percent(interval=None)
            ram  = _psutil.virtual_memory()
            disk = _psutil.disk_usage("/")
            payload = {
                "cpu_percent":   round(cpu, 1),
                "ram_used_gb":   round(ram.used / 1e9, 1),
                "ram_total_gb":  round(ram.total / 1e9, 1),
                "ram_percent":   round(ram.percent, 1),
                "disk_used_gb":  round(disk.used / 1e9, 0),
                "disk_total_gb": round(disk.total / 1e9, 0),
                "disk_percent":  round(disk.percent, 1),
            }
        else:
            payload = {
                "cpu_percent": 0, "ram_used_gb": 0, "ram_total_gb": 0,
                "ram_percent": 0, "disk_used_gb": 0, "disk_total_gb": 0,
                "disk_percent": 0,
            }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_processes(self) -> None:
        """Live process table for the process-constellation takeover."""
        from nora import visuals
        self._json_ok(json.dumps(visuals.process_snapshot()).encode())

    def _serve_desktop(self) -> None:
        """The focused window as a map of pressable targets.

        Token-gated even though it is a read: the map names every button in
        whatever window happens to be focused, which on a machine reachable
        from the network is a live description of what the user is doing.
        """
        from nora import desktop
        if not self._token_ok():
            self._json_denied()
            return
        import urllib.parse
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        follow = (params.get("follow") or ["0"])[0] == "1"
        self._json_ok(json.dumps(desktop.window_map(follow=follow)).encode())

    def _serve_memory_graph(self) -> None:
        """Memory embeddings projected to a sphere, for the constellation.

        Non-blocking by design: the cold build is a couple of seconds of
        SVD and this server handles one request at a time, so a miss
        returns {"building": true} and a daemon thread does the work.
        """
        from nora import visuals
        self._json_ok(json.dumps(visuals.memory_graph_async()).encode())

    def _serve_history(self) -> None:
        try:
            from nora import cognitive_memory
            episodes = cognitive_memory.get_recent_episodes(n=30)
        except Exception:
            episodes = []
        body = json.dumps(episodes).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_analytics(self) -> None:
        try:
            from nora import cognitive_memory
            analytics = cognitive_memory.get_analytics()
        except Exception:
            analytics = {}
        body = json.dumps(analytics).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_response(404)
            self.end_headers()
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:  # silence request logs
        pass

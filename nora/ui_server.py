from __future__ import annotations

"""Lightweight HTTP server that serves the NORA dashboard UI.

HTTP endpoints
--------------
GET  /           → index.html
GET  /state      → {speaking, text, status, ptt_mode}
GET  /metrics    → system vitals
GET  /music      → {track, artist, source, status}
POST /ptt        → push-to-talk button control
POST /music_ctl  → dispatch playback controls from the UI (play/pause/next/prev/volume)

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

_state: dict = {"speaking": False, "text": "", "status": "STANDBY", "stage": "idle"}
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


def notify_ptt_mode(enabled: bool) -> None:
    """Mirror PTT-mode changes to the dashboard immediately."""
    with _lock:
        _state["ptt_mode_changed_at"] = enabled


def start(port: int = 8766) -> str:
    """Start the HTTP server in a daemon thread. Returns the URL."""
    server = HTTPServer(("0.0.0.0", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="nora-ui")
    thread.start()
    url = f"http://localhost:{port}"
    logger.info("NORA UI server started at %s", url)
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


def ws_notify(message: str) -> None:
    """Push a proactive notification string to all connected WebSocket clients."""
    ws_push({"type": "notification", "message": message})


async def _ws_connection_handler(websocket: Any, token: str) -> None:
    """Handle a single authenticated WebSocket connection."""
    import urllib.parse

    # ── Auth ──────────────────────────────────────────────────────────────
    if token:
        # 1. Try query-string token: ws://host:port?token=xxx
        try:
            raw_path = websocket.path  # type: ignore[attr-defined]
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

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/state":
            self._serve_state()
        elif self.path == "/metrics":
            self._serve_metrics()
        elif self.path == "/music":
            self._serve_music()
        elif self.path == "/history":
            self._serve_history()
        elif self.path == "/analytics":
            self._serve_analytics()
        elif self.path in ("/", "/index.html"):
            self._serve_file(_STATIC_DIR / "index.html", "text/html; charset=utf-8")
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
        if self.path == "/ptt":
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
        elif self.path == "/music_ctl":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
            self._dispatch_music(body)
            self._json_ok(b"{}")
        elif self.path == "/type_command":
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
                from nora.commands import music as music_cmd
                if act == "play":
                    music_cmd.resume_music()
                elif act == "pause":
                    music_cmd.pause_music()
                elif act == "stop":
                    music_cmd.stop_music()
                elif act == "next":
                    from nora.commands.apple_music import apple_music_next_track
                    apple_music_next_track()
                elif act == "prev":
                    from nora.commands.apple_music import apple_music_previous_track
                    apple_music_previous_track()
                elif act == "volume":
                    from nora.commands.system_control import set_volume
                    level = int(body.get("level", 50))
                    set_volume(level)
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
        body = json.dumps(context.get_music()).encode()
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

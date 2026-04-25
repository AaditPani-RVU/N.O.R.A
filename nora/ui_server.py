from __future__ import annotations

"""Lightweight HTTP server that serves the NORA dashboard UI.

Endpoints
---------
GET  /           â†’ index.html
GET  /state      â†’ {speaking, text, status, ptt_mode}
GET  /metrics    â†’ system vitals
GET  /music      â†’ {track, artist, source, status}
POST /ptt        â†’ push-to-talk button control
POST /music_ctl  â†’ dispatch playback controls from the UI (play/pause/next/prev/volume)
"""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from nora import context

try:
    import psutil as _psutil
except ImportError:  # graceful fallback if psutil missing
    _psutil = None

logger = logging.getLogger("nora.ui_server")

_state: dict = {"speaking": False, "text": "", "status": "STANDBY"}
_lock = threading.Lock()

# Push-to-talk state â€” set by the UI button, read by listener.py
_ptt_event = threading.Event()


def is_ptt_pressed() -> bool:
    """Return True while the UI push-to-talk button is held."""
    return _ptt_event.is_set()

_STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def notify(speaking: bool, text: str = "", status: str = "") -> None:
    """Update UI state. Thread-safe â€” call from anywhere."""
    with _lock:
        _state["speaking"] = speaking
        if text:
            _state["text"] = text
        if status:
            _state["status"] = status


def notify_ptt_mode(enabled: bool) -> None:
    """Mirror PTT-mode changes to the dashboard immediately."""
    with _lock:
        _state["ptt_mode_changed_at"] = enabled


def start(port: int = 8766) -> str:
    """Start the HTTP server in a daemon thread. Returns the URL."""
    server = HTTPServer(("localhost", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="nora-ui")
    thread.start()
    url = f"http://localhost:{port}"
    logger.info("NORA UI server started at %s", url)
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
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:  # silence request logs
        pass

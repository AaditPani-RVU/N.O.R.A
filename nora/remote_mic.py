"""
Remote microphone bridge — receives raw PCM audio from a remote client
(e.g. a MacBook), transcribes it locally, and injects the text into NORA's
existing command pipeline via the text_input queue.

Endpoints
---------
POST /audio   raw float32 mono 16 kHz PCM body → transcribe + queue
GET  /ping    {"ok": true, "stage": "<pipeline stage>"}

Start with remote_mic.start() in pipeline.py when remote_mic.enabled: true.
Client: run nora_remote.py on the remote machine.
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

logger = logging.getLogger("nora.remote_mic")

_RMS_FLOOR = 0.003  # skip silent/nearly-silent buffers


class _Handler(BaseHTTPRequestHandler):

    def do_GET(self) -> None:
        if self.path == "/ping":
            try:
                from nora import ui_server
                with ui_server._lock:
                    stage = ui_server._state.get("stage", "idle")
            except Exception:
                stage = "idle"
            self._json({"ok": True, "stage": stage})
        else:
            self._send(404)

    def do_POST(self) -> None:
        if self.path == "/audio":
            self._handle_audio()
        else:
            self._send(404)

    def _handle_audio(self) -> None:
        import numpy as np
        from nora import text_input, transcriber

        length = int(self.headers.get("Content-Length", 0))
        if not length:
            self._json({"ok": False, "error": "empty body"}, 400)
            return

        raw = self.rfile.read(length)

        try:
            audio = np.frombuffer(raw, dtype=np.float32).copy()
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
            return

        rms = float(np.sqrt(np.mean(audio ** 2)))
        if rms < _RMS_FLOOR:
            logger.debug("Remote audio too quiet (rms=%.4f) — skipped", rms)
            self._json({"ok": True, "transcription": None})
            return

        try:
            text = transcriber.transcribe(audio)
        except Exception as exc:
            logger.error("Remote transcription failed: %s", exc)
            self._json({"ok": False, "error": str(exc)}, 500)
            return

        text = (text or "").strip()
        if len(text) >= 2:
            logger.info("Remote mic: %r", text)
            print(f"[NORA] Remote mic: {text}", flush=True)
            text_input._queue.put(text)
            self._json({"ok": True, "transcription": text})
        else:
            self._json({"ok": True, "transcription": None})

    # ── helpers ──────────────────────────────────────────────────────────────

    def _json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_) -> None:
        pass  # suppress per-request logs


def start(host: str = "0.0.0.0", port: int = 8767) -> str:
    """Start the remote mic HTTP server in a daemon thread. Returns the URL."""
    server = HTTPServer((host, port), _Handler)
    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name="nora-remote-mic",
    )
    thread.start()
    url = f"http://{host}:{port}"
    logger.info("Remote mic server started at %s", url)
    return url

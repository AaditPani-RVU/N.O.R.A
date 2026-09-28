"""
Remote microphone bridge — receives raw PCM audio from a remote client
(e.g. a MacBook), transcribes it locally, and injects the text into NORA's
existing command pipeline via the text_input queue.

Endpoints
---------
POST /audio   raw float32 mono 16 kHz PCM body → transcribe + queue
GET  /ping    {"ok": true, "stage": "<pipeline stage>"}

Both need `Authorization: Bearer <NORA_API_TOKEN>` and a peer inside
`security.remote_networks`. This server takes audio and turns it into commands,
so an unauthenticated POST was a way to speak to NORA from anywhere on the LAN.

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

# 60 s of float32 mono at 16 kHz. A longer body is refused before it is read,
# so a client can't make NORA allocate whatever Content-Length it claims.
_MAX_BODY_BYTES = 60 * 16000 * 4


class _Handler(BaseHTTPRequestHandler):

    def _authorised(self) -> bool:
        from nora import security

        peer = self.client_address[0] if self.client_address else None
        if not security.peer_allowed(peer):
            logger.warning("Remote mic: refused %s (outside remote_networks)", peer)
            self._send(403)
            return False
        auth = self.headers.get("Authorization", "")
        provided = auth[7:] if auth.startswith("Bearer ") else ""
        if not security.check_api_token(
                provided, peer=peer, proxied=security.is_proxied(self.headers)):
            logger.warning("Remote mic: refused unauthenticated request from %s", peer)
            self._json({"ok": False, "error": "unauthorised"}, 401)
            return False
        return True

    def do_GET(self) -> None:
        if not self._authorised():
            return
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
        if not self._authorised():
            return
        if self.path == "/audio":
            self._handle_audio()
        else:
            self._send(404)

    def _handle_audio(self) -> None:
        import numpy as np
        from nora import text_input, transcriber

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = -1
        if length <= 0:
            self._json({"ok": False, "error": "empty body"}, 400)
            return
        if length > _MAX_BODY_BYTES:
            self._json({"ok": False, "error": "body too large"}, 413)
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
    import os
    if not os.environ.get("NORA_API_TOKEN"):
        logger.warning("Remote mic: NORA_API_TOKEN is unset, so only requests "
                       "from this machine will be accepted")
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

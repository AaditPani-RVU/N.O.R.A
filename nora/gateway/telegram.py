"""Telegram gateway — NORA answering from your pocket.

Long-polling, not webhooks. A webhook would mean a public HTTPS endpoint
pointing at a process that can control the user's desktop, which is a large
thing to expose for a convenience feature; `getUpdates` needs no inbound port
and no certificate, and Telegram holds the connection open so it costs nothing
while idle.

Two guards, both non-negotiable given what is on the other end of this socket:

*An allowlist.* `telegram.allowed_chat_ids` in config. An empty allowlist
refuses everyone rather than defaulting open — a bot token is a bearer
credential, and the failure mode of getting that backwards is a stranger with a
shell on the user's machine.

*No remote confirmation.* `gateway.core.handle_text` refuses anything the
security policy wants confirmed rather than accepting a typed "yes". Whoever
holds the phone is not necessarily whoever owns the desktop.

Voice memos are the reason this is the gateway worth having on a voice-first
assistant: they go through the same Whisper model as the microphone, so talking
to NORA from the bus is the same interaction as talking to it from the desk.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

logger = logging.getLogger("nora.gateway.telegram")

_API = "https://api.telegram.org/bot{token}/{method}"
_FILE_API = "https://api.telegram.org/file/bot{token}/{path}"

# Telegram holds the request open this long when there is nothing new, so the
# loop spends almost all of its time blocked rather than spinning.
_POLL_TIMEOUT = 30

_thread: threading.Thread | None = None
_stop = threading.Event()
# Chat ids already told they aren't authorised, so each is told exactly once.
_rejected: set[int] = set()


def _cfg() -> dict:
    from nora.config import get_config
    return get_config().get("telegram", {}) or {}


def _token() -> str:
    import os
    cfg = _cfg()
    return (os.environ.get(cfg.get("token_env", "TELEGRAM_BOT_TOKEN"), "")
            or cfg.get("token", "")).strip()


def _allowed(chat_id: int) -> bool:
    """Whether this chat may talk to NORA. Closed by default."""
    allowed = _cfg().get("allowed_chat_ids") or []
    return int(chat_id) in {int(c) for c in allowed}


def _call(method: str, token: str, http_timeout: int = 40, **params: Any) -> dict | None:
    """POST to the Bot API. `http_timeout` is the socket deadline; everything
    else in **params goes to Telegram verbatim — including its own `timeout`,
    which is the long-poll duration and a different thing entirely.
    """
    import requests

    try:
        resp = requests.post(
            _API.format(token=token, method=method), json=params, timeout=http_timeout
        )
        payload = resp.json()
    except Exception as e:
        logger.debug("telegram %s failed: %s", method, e)
        return None
    if not payload.get("ok"):
        logger.warning("telegram %s error: %s", method, payload.get("description"))
        return None
    return payload.get("result")


def send(chat_id: int, text: str) -> None:
    token = _token()
    if not token or not text:
        return
    # Telegram hard-caps a message at 4096 characters.
    _call("sendMessage", token, chat_id=chat_id, text=text[:4000])


def _download(file_id: str, token: str) -> bytes | None:
    import requests

    info = _call("getFile", token, file_id=file_id)
    if not info or "file_path" not in info:
        return None
    try:
        resp = requests.get(
            _FILE_API.format(token=token, path=info["file_path"]), timeout=60
        )
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        logger.warning("could not download telegram file: %s", e)
        return None


def _transcribe_voice(file_id: str, token: str) -> str:
    """Voice memo → text, through the same Whisper model the microphone uses."""
    from nora import transcriber
    from nora.gateway.core import decode_audio

    data = _download(file_id, token)
    if not data:
        return ""
    audio = decode_audio(data)
    if audio is None:
        return ""
    try:
        return transcriber.transcribe(audio).strip()
    except Exception as e:
        logger.warning("voice memo transcription failed: %s", e)
        return ""


def _handle(update: dict, token: str) -> None:
    message = update.get("message") or update.get("edited_message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id is None:
        return

    if not _allowed(chat_id):
        # Told once, then ignored. The owner setting this up needs to see the
        # chat id to put in the allowlist; anyone else gets a single reply
        # rather than an unbounded number, so the bot can't be used to make
        # NORA send messages on demand.
        logger.warning("Rejected telegram message from chat %s", chat_id)
        if chat_id not in _rejected:
            _rejected.add(chat_id)
            send(chat_id, f"Not authorised. Add {chat_id} to telegram.allowed_chat_ids.")
        return

    text = (message.get("text") or "").strip()
    if not text and message.get("voice"):
        text = _transcribe_voice(message["voice"]["file_id"], token)
        if not text:
            send(chat_id, "I couldn't make out that voice note.")
            return
        # Echo the transcript so a Whisper mishearing is visible rather than
        # silently acted on.
        send(chat_id, f"heard: {text}")
    if not text:
        return

    from nora.gateway.core import handle_text

    try:
        reply = asyncio.run(handle_text(text, source="telegram"))
    except Exception as e:
        logger.error("telegram turn failed: %s", e)
        reply = "Something went wrong handling that."
    send(chat_id, reply or "Done.")


def _loop(token: str) -> None:
    offset = 0
    backoff = 1.0
    while not _stop.is_set():
        # `timeout` here is Telegram's long-poll hold; the socket deadline is
        # deliberately longer, or requests would abort every idle poll.
        result = _call("getUpdates", token, http_timeout=_POLL_TIMEOUT + 10,
                       offset=offset, timeout=_POLL_TIMEOUT)
        if result is None:
            # Network flap or a bad token. Back off rather than hammering.
            _stop.wait(backoff)
            backoff = min(backoff * 2, 60.0)
            continue
        backoff = 1.0
        for update in result:
            offset = max(offset, update.get("update_id", 0) + 1)
            try:
                _handle(update, token)
            except Exception as e:
                logger.error("telegram update failed: %s", e)


def start() -> bool:
    """Start polling if configured. Returns whether it started."""
    global _thread
    cfg = _cfg()
    if not cfg.get("enabled", False):
        return False

    token = _token()
    if not token:
        logger.warning("telegram enabled but no bot token — set TELEGRAM_BOT_TOKEN")
        return False
    if not (cfg.get("allowed_chat_ids") or []):
        logger.warning("telegram enabled but allowed_chat_ids is empty — refusing "
                       "to start an open bot")
        return False
    if _thread is not None and _thread.is_alive():
        return True

    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(token,), daemon=True,
                               name="nora-telegram")
    _thread.start()
    logger.info("Telegram gateway polling")
    return True


def stop() -> None:
    global _thread
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=2.0)
    _thread = None

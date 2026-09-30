"""Delivery router — which device hears something NORA says unprompted.

A turn's reply goes back down its own channel (`nora.channel`). This is for
everything said *outside* a turn: a background job's answer, a scheduled
reminder. Those used to call one speak callback, which meant the laptop
speaker, which meant a question typed on the phone was answered to an empty
room.

Rule: something is delivered to the device that asked for it. If that device
is not connected right now, it is held — `jobs` keeps the row undelivered —
and flushed when the device next connects, rather than spoken at a house
nobody is in. Work that came from the core's own microphone (`local`) keeps
the old path: the focus-gated local speaker.

Devices register a sink while connected (`nora.hub` does this on handshake),
so this module holds no sockets and knows nothing about the protocol.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from nora.channel import LOCAL_DEVICE

logger = logging.getLogger("nora.delivery")

# device_id -> send(text, kind). Returns True when the device accepted it.
Sink = Callable[[str, str], bool]

_lock = threading.Lock()
_sinks: dict[str, Sink] = {}
_last_active: dict[str, float] = {}


def register(device_id: str, sink: Sink) -> None:
    with _lock:
        _sinks[device_id] = sink
        _last_active.setdefault(device_id, time.time())


def unregister(device_id: str, sink: Sink | None = None) -> None:
    """Drop a device's sink. With `sink`, only if it is still the registered
    one — a reconnect may already have replaced it."""
    with _lock:
        if sink is None or _sinks.get(device_id) is sink:
            _sinks.pop(device_id, None)


def connected() -> list[str]:
    with _lock:
        return sorted(_sinks)


def note_active(device_id: str) -> None:
    """The user just did something on this device."""
    with _lock:
        _last_active[device_id] = time.time()


def deliver(text: str, *, device: str, kind: str = "job") -> bool:
    """Send `text` to `device`. False if it is not connected or refused it.

    Never called for `local` — the local speaker is the caller's own fallback,
    because it has to go through `focus` gating the caller already owns.
    """
    if device == LOCAL_DEVICE:
        return False
    with _lock:
        sink = _sinks.get(device)
    if sink is None:
        return False
    try:
        return bool(sink(text, kind))
    except Exception as e:
        logger.warning("Delivery to %s failed: %s", device, e)
        return False


def broadcast(text: str, *, kind: str = "reminder") -> list[str]:
    """Send `text` to every connected device. Returns the ones that took it.

    For what must reach the user wherever they are — a reminder set on the
    core's own microphone — and not for answers, which go to who asked.
    Off with `hub.reminders_to_devices: false`.
    """
    from nora.config import get_config
    if not (get_config().get("hub", {}) or {}).get("reminders_to_devices", True):
        return []
    return [d for d in connected() if deliver(text, device=d, kind=kind)]


def reset_for_tests() -> None:
    with _lock:
        _sinks.clear()
        _last_active.clear()

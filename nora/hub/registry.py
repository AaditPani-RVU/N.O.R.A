"""Paired devices and pairing codes — rows in the core store.

Kept in the store rather than in the hub's memory so the CLI
(`python -m nora.hub`) and the running core see the same state: a device
approved or revoked from a terminal takes effect in the live hub without a
restart, because the hub reads these rows at handshake and before each
invocation.

The pairing code itself is never stored, only its SHA-256. It is single-use,
expires in five minutes, and five wrong guesses void every outstanding code —
40 bits is plenty against that budget.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass

from nora import store
from nora.hub import protocol

CODE_TTL_SEC = 300
MAX_FAILED_PAIRS = 5
_FAIL_WINDOW_SEC = 300

_fail_lock = threading.Lock()
_failures: list[float] = []


@dataclass
class Device:
    id: str
    name: str
    platform: str
    public_key: bytes
    paired_at: float
    approved_at: float | None
    revoked_at: float | None
    last_seen: float | None

    @property
    def usable(self) -> bool:
        return self.approved_at is not None and self.revoked_at is None

    @property
    def status(self) -> str:
        if self.revoked_at is not None:
            return "revoked"
        return "approved" if self.approved_at is not None else "pending"


def _hash(code: str) -> str:
    return hashlib.sha256(protocol.normalise_code(code).encode()).hexdigest()


def create_code(ttl: float = CODE_TTL_SEC) -> tuple[str, float]:
    """A fresh pairing code and its expiry time. Shown to the user, never stored."""
    code = protocol.pairing_code()
    now = time.time()
    with store.transaction() as conn:
        conn.execute("DELETE FROM pairing_codes WHERE expires_at < ?", (now,))
        conn.execute("INSERT INTO pairing_codes (code_hash, created_at, expires_at)"
                     " VALUES (?, ?, ?)", (_hash(code), now, now + ttl))
    return code, now + ttl


def _note_failure() -> None:
    now = time.time()
    with _fail_lock:
        _failures[:] = [t for t in _failures if now - t < _FAIL_WINDOW_SEC]
        _failures.append(now)
        tripped = len(_failures) >= MAX_FAILED_PAIRS
        if tripped:
            _failures.clear()
    if tripped:
        with store.transaction() as conn:
            conn.execute("UPDATE pairing_codes SET used_at = ? WHERE used_at IS NULL", (now,))


def redeem(code: str, *, name: str, platform: str, public_key_b64: str) -> Device | None:
    """Spend a pairing code on a new device. None if the code is not good.

    The device starts *pending*: holding the code proves the user showed it to
    this device, and approval (`approve`) is the user saying so on the core.
    """
    protocol.load_public_key(public_key_b64)          # raises on a bad key
    now = time.time()
    with store.transaction() as conn:
        cur = conn.execute(
            "UPDATE pairing_codes SET used_at = ? WHERE code_hash = ?"
            " AND used_at IS NULL AND expires_at >= ?",
            (now, _hash(code), now))
        if cur.rowcount != 1:
            ok = False
        else:
            ok = True
            device = Device(
                id="d_" + secrets.token_hex(6), name=name.strip()[:60] or "device",
                platform=platform.strip()[:20] or "unknown",
                public_key=base64.b64decode(public_key_b64),
                paired_at=now, approved_at=None, revoked_at=None, last_seen=None)
            conn.execute(
                "INSERT INTO devices (id, name, platform, public_key, paired_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (device.id, device.name, device.platform, device.public_key, now))
    if not ok:
        _note_failure()
        return None
    return device


def _row(r) -> Device:
    return Device(id=r["id"], name=r["name"], platform=r["platform"],
                  public_key=bytes(r["public_key"]), paired_at=r["paired_at"],
                  approved_at=r["approved_at"], revoked_at=r["revoked_at"],
                  last_seen=r["last_seen"])


def get(device_id: str) -> Device | None:
    rows = store.query("SELECT * FROM devices WHERE id = ?", (device_id,))
    return _row(rows[0]) if rows else None


def listing() -> list[Device]:
    return [_row(r) for r in store.query("SELECT * FROM devices ORDER BY paired_at")]


def approve(device_id: str) -> bool:
    with store.transaction() as conn:
        return conn.execute(
            "UPDATE devices SET approved_at = ? WHERE id = ? AND revoked_at IS NULL"
            " AND approved_at IS NULL", (time.time(), device_id)).rowcount == 1


def revoke(device_id: str) -> bool:
    """Revoke a device. Its key stays on the row for the audit trail but can
    never authenticate again; the live hub drops it on its next check."""
    with store.transaction() as conn:
        return conn.execute(
            "UPDATE devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (time.time(), device_id)).rowcount == 1


def touch(device_id: str) -> None:
    with store.transaction() as conn:
        conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (time.time(), device_id))


def reset_for_tests() -> None:
    with _fail_lock:
        _failures.clear()

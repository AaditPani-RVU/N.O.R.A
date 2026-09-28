"""Protocol v1 between the core and a device — envelopes, ids, keys, schemas.

Everything here is pure: no sockets, no store. `server` and `fake_device` both
build on it, which is what keeps the fake device honest — it speaks the same
wire format through the same code a real client would have to match.

See NORA_DISTRIBUTED_PLAN.md §5 for the message catalogue.
"""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

VERSION = 1
PATH = "/v1/device"

# Largest frame accepted either way. Photos and other bulk go through blob
# upload (Phase 7), never the control socket.
MAX_FRAME = 256 * 1024

# What a signature covers besides the nonce, so a signature made for some
# other purpose with the same key can never be replayed as a login.
AUTH_CONTEXT = b"nora-v1"

# Messages a device may send once authenticated.
DEVICE_TYPES = frozenset({
    "manifest", "result", "confirm_response", "event", "utterance",
    "ping", "pong", "kill",
})

# Error codes (plan §5). DEVICE_OFFLINE is core-side only.
INVALID_PARAMS = "INVALID_PARAMS"
PERMISSION_DENIED = "PERMISSION_DENIED"
CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
POLICY_BLOCKED = "POLICY_BLOCKED"
USER_DECLINED = "USER_DECLINED"
CONFIRMATION_EXPIRED = "CONFIRMATION_EXPIRED"
DEVICE_BUSY = "DEVICE_BUSY"
BACKGROUND_RESTRICTED = "BACKGROUND_RESTRICTED"
EXPIRED = "EXPIRED"
TIMEOUT = "TIMEOUT"
DEVICE_OFFLINE = "DEVICE_OFFLINE"
EXECUTION_FAILED = "EXECUTION_FAILED"

# Tiers (plan §6): 0 read · 1 act, logged · 2 on-device confirm ·
# 3 confirm + biometric · 4 never implemented.
TIER_CONFIRM = 2
TIER_FORBIDDEN = 4

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class ProtocolError(ValueError):
    """A frame that is not valid protocol v1. The connection is closed."""


def new_id() -> str:
    """A ULID: 48-bit millisecond time + 80 random bits, Crockford base32.
    Sortable by creation time, which makes the invocation log read in order."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(out))


def pairing_code() -> str:
    """Eight Crockford characters (40 bits), grouped for reading aloud."""
    raw = "".join(_CROCKFORD[b & 31] for b in os.urandom(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalise_code(code: str) -> str:
    """Accept what a person types: any case, with or without the dash, and the
    look-alikes Crockford base32 folds (O→0, I/L→1)."""
    code = code.strip().upper().replace("-", "").replace(" ", "")
    return code.translate(str.maketrans("OIL", "011"))


def envelope(type_: str, body: dict | None = None, *, corr: str | None = None,
             seq: int = 0, id_: str | None = None) -> dict:
    return {"v": VERSION, "id": id_ or new_id(), "type": type_,
            "ts": int(time.time() * 1000), "corr": corr, "seq": seq,
            "body": body or {}}


def encode(msg: dict) -> str:
    return json.dumps(msg, separators=(",", ":"))


def decode(frame: str | bytes) -> dict:
    """Parse and structurally check one frame. Raises ProtocolError."""
    if isinstance(frame, bytes):
        raise ProtocolError("binary frames are not used in protocol v1 control")
    if len(frame) > MAX_FRAME:
        raise ProtocolError("frame too large")
    try:
        msg = json.loads(frame)
    except ValueError as e:
        raise ProtocolError(f"not JSON: {e}") from None
    if not isinstance(msg, dict):
        raise ProtocolError("frame is not an object")
    if msg.get("v") != VERSION:
        raise ProtocolError(f"unsupported protocol version {msg.get('v')!r}")
    if not isinstance(msg.get("id"), str) or not msg["id"] or len(msg["id"]) > 64:
        raise ProtocolError("missing id")
    if not isinstance(msg.get("type"), str):
        raise ProtocolError("missing type")
    if not isinstance(msg.get("body", {}), dict):
        raise ProtocolError("body is not an object")
    corr = msg.get("corr")
    if corr is not None and not isinstance(corr, str):
        raise ProtocolError("corr is not a string")
    msg.setdefault("body", {})
    return msg


# ── Keys and signatures ──────────────────────────────────────────────────────

def auth_payload(nonce: bytes, device_id: str) -> bytes:
    return nonce + device_id.encode("utf-8") + AUTH_CONTEXT


def load_public_key(der_b64: str) -> ec.EllipticCurvePublicKey:
    """A device public key: SubjectPublicKeyInfo DER, base64 — what Android
    Keystore's `getPublic().getEncoded()` produces. P-256 only."""
    try:
        key = serialization.load_der_public_key(base64.b64decode(der_b64, validate=True))
    except Exception as e:
        raise ProtocolError(f"unreadable public key: {e}") from None
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise ProtocolError("public key must be EC P-256")
    return key


def public_key_der(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.DER,
                            serialization.PublicFormat.SubjectPublicKeyInfo)


def verify(public_der: bytes, signature_b64: str, payload: bytes) -> bool:
    """ECDSA-SHA256 over `payload`, DER signature, base64."""
    try:
        key = serialization.load_der_public_key(public_der)
        key.verify(base64.b64decode(signature_b64, validate=True), payload,
                   ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def sign(private_key: ec.EllipticCurvePrivateKey, payload: bytes) -> str:
    """Device side, for the fake device. Android does this in Keystore."""
    return base64.b64encode(private_key.sign(payload, ec.ECDSA(hashes.SHA256()))).decode()


# ── Capability manifests ─────────────────────────────────────────────────────

_NAME_OK = set("abcdefghijklmnopqrstuvwxyz0123456789._")


def check_manifest_entry(entry: Any) -> dict:
    """Validate one capability from a device manifest. Raises ProtocolError.

    The schema is tightened on the way in: an object schema that does not say
    `additionalProperties` gets `false`, because the plan's rule is that
    unknown fields are rejected, and a device forgetting to say so should not
    be what opens that up.
    """
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    if not isinstance(entry, dict):
        raise ProtocolError("capability is not an object")
    name = entry.get("name")
    if (not isinstance(name, str) or not 3 <= len(name) <= 64 or "." not in name
            or not set(name) <= _NAME_OK or name.startswith(".") or name.endswith(".")):
        raise ProtocolError(f"bad capability name {name!r}")
    tier = entry.get("tier")
    if not isinstance(tier, int) or isinstance(tier, bool) or not 0 <= tier <= TIER_FORBIDDEN:
        raise ProtocolError(f"{name}: tier must be 0-4")
    schema = entry.get("params_schema", {"type": "object", "properties": {}})
    if not isinstance(schema, dict):
        raise ProtocolError(f"{name}: params_schema is not an object")
    schema = dict(schema)
    if schema.get("type") == "object":
        schema.setdefault("additionalProperties", False)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as e:
        raise ProtocolError(f"{name}: invalid params_schema: {e.message}") from None
    description = entry.get("description", "")
    if not isinstance(description, str):
        raise ProtocolError(f"{name}: description is not a string")
    return {
        "name": name,
        "version": int(entry.get("version", 1)),
        "description": description[:200],
        "params_schema": schema,
        "tier": tier,
        "requires_live_user": bool(entry.get("requires_live_user", False)),
        "available": bool(entry.get("available", True)),
    }


def validate_params(schema: dict, params: dict) -> str | None:
    """None if `params` fit `schema`, else the first problem, for the LLM to read."""
    from jsonschema import Draft202012Validator

    errors = sorted(Draft202012Validator(schema).iter_errors(params), key=lambda e: e.path)
    if not errors:
        return None
    e = errors[0]
    where = ".".join(str(p) for p in e.path)
    return f"{where}: {e.message}" if where else e.message


def signature_hint(entry: dict) -> str:
    """`name(param, param=...)` for the action block of the system prompt."""
    schema = entry["params_schema"]
    required = set(schema.get("required", []))
    parts = []
    for prop in schema.get("properties", {}):
        parts.append(prop if prop in required else f"{prop}=...")
    return f"{entry['name']}({', '.join(parts)})"

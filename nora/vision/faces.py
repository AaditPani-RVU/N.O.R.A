"""Persistent face memory.

Backed by insightface `buffalo_l` (ONNX runtime) for detection and 512-d
ArcFace embeddings. Detection and embedding are exposed separately so the
perception loop can detect cheaply every frame and embed only when it
actually needs to answer "who is this".

Store lives at ~/.nora/faces.json, matching the ~/.nora/dbus_blessed.json
convention already used for learned D-Bus shortcuts.

    {"people": {"aadit": {"embeddings": [[512 floats], ...],
                          "enrolled_at": 1699999999.0,
                          "trusted": true,
                          "greeting": "Welcome back.",
                          "persona": {"tone": "casual"}}}}

Phase 2 adds the profile fields alongside the embeddings: `greeting` (what
NORA says when this person arrives) and `persona` (style hints handed to
the prompt while they are the only one in frame). `trusted` marks the
owner — see is_trusted() for why that flag may only ever restrict.

Privacy: nothing reaches this file except through enroll(), which is only
ever called by an explicit `learn_face(name)`. Unrecognized faces are
matched and discarded — no embedding, no thumbnail, no log entry.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("nora.vision.faces")

_PATH = Path.home() / ".nora" / "faces.json"
_lock = threading.RLock()

_app: Any = None                 # insightface FaceAnalysis
_app_failed = False
_app_lock = threading.RLock()


def _cfg() -> dict:
    from nora.config import get_config
    return get_config().get("vision", {}) or {}


def _face_cfg() -> dict:
    return _cfg().get("face", {}) or {}


# ── Model ────────────────────────────────────────────────────────────────────

def _providers() -> list[str]:
    """ONNX providers for the configured device, falling back when CUDA is absent."""
    device = str(_cfg().get("detector_device", "cpu")).lower()
    if device != "cuda":
        return ["CPUExecutionProvider"]
    try:
        import onnxruntime
        available = onnxruntime.get_available_providers()
    except Exception:
        return ["CPUExecutionProvider"]
    if "CUDAExecutionProvider" in available:
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    logger.info("detector_device is 'cuda' but onnxruntime has no CUDA provider — using CPU")
    return ["CPUExecutionProvider"]


def get_app():
    """Lazily build the FaceAnalysis app. Returns None if unavailable.

    First call downloads the buffalo_l pack (~300 MB), which is why
    warm_up() exists — the first greeting shouldn't wait on a download.
    """
    global _app, _app_failed

    with _app_lock:
        if _app is not None or _app_failed:
            return _app
        try:
            from insightface.app import FaceAnalysis

            providers = _providers()
            app = FaceAnalysis(
                name="buffalo_l",
                allowed_modules=["detection", "recognition"],
                providers=providers,
            )
            ctx_id = 0 if providers[0].startswith("CUDA") else -1
            app.prepare(ctx_id=ctx_id, det_size=(640, 640))
            _app = app
            logger.info("Face model ready (buffalo_l, %s)", providers[0])
        except Exception as e:
            _app_failed = True
            logger.warning("Face recognition unavailable: %s", e)
        return _app


def warm_up() -> None:
    """Load the face model in the background, like cognitive_memory.warm_up()."""
    threading.Thread(target=get_app, daemon=True, name="nora-face-warmup").start()


def is_available() -> bool:
    return get_app() is not None


# ── Detection / embedding (deliberately separate) ────────────────────────────

def detect(frame) -> list[dict[str, Any]]:
    """Detect faces in a BGR frame. Cheap enough to run every frame.

    Returns dicts of {"bbox": (x1, y1, x2, y2), "kps": ndarray|None, "score": float}.
    No embedding is computed here — see embed().
    """
    app = get_app()
    if app is None or frame is None:
        return []
    try:
        bboxes, kpss = app.det_model.detect(frame, max_num=0, metric="default")
    except Exception as e:
        logger.debug("Face detection failed: %s", e)
        return []
    if bboxes is None or len(bboxes) == 0:
        return []

    out = []
    for i in range(bboxes.shape[0]):
        x1, y1, x2, y2 = (float(v) for v in bboxes[i, 0:4])
        out.append({
            "bbox": (x1, y1, x2, y2),
            "kps": None if kpss is None else kpss[i],
            "score": float(bboxes[i, 4]),
        })
    return out


def embed(frame, detection: dict[str, Any]):
    """Compute the 512-d normed embedding for one detected face.

    Run only on demand — for a face that is new or not yet identified —
    never on every face on every frame.
    """
    app = get_app()
    if app is None:
        return None
    rec = app.models.get("recognition")
    if rec is None:
        return None
    try:
        import numpy as np
        from insightface.app.common import Face

        face = Face(
            bbox=np.array(detection["bbox"], dtype=np.float32),
            kps=detection.get("kps"),
            det_score=detection.get("score", 1.0),
        )
        rec.get(frame, face)
        return face.normed_embedding
    except Exception as e:
        logger.debug("Face embedding failed: %s", e)
        return None


# ── Store ────────────────────────────────────────────────────────────────────

def _read() -> dict[str, Any]:
    try:
        if _PATH.exists():
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("people"), dict):
                return data
    except Exception as e:
        logger.warning("Face store unreadable (%s) — starting empty", e)
    return {"people": {}}


def _write(data: dict[str, Any]) -> None:
    """Atomic write — a torn faces.json would lose every enrolled person."""
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, _PATH)


def _norm(name: str) -> str:
    return name.strip().lower()


def enroll(name: str, embeddings: list) -> int:
    """Store embeddings for a person. Returns the total kept for them.

    The only writer of face data in the system. Called exclusively by an
    explicit learn_face(name) — never by the perception loop.
    """
    key = _norm(name)
    if not key:
        raise ValueError("A name is required to enroll a face.")
    vectors = [[float(x) for x in e] for e in embeddings if e is not None]
    if not vectors:
        raise ValueError("No usable face embeddings to enroll.")

    with _lock:
        data = _read()
        person = data["people"].get(key) or {
            "enrolled_at": time.time(),
            "trusted": False,
            "embeddings": [],
        }
        person["embeddings"] = (person.get("embeddings", []) + vectors)[-20:]
        person["display_name"] = name.strip()
        person["updated_at"] = time.time()
        data["people"][key] = person
        _write(data)
        return len(person["embeddings"])


def identify(embedding) -> tuple[str | None, float]:
    """Cosine-match an embedding against the store.

    Returns (name, score), or (None, best_score) when nothing clears the
    configured threshold. Embeddings from insightface are already L2-normed,
    so the dot product is the cosine similarity.
    """
    if embedding is None:
        return None, 0.0

    import numpy as np

    threshold = float(_face_cfg().get("match_threshold", 0.5))
    query = np.asarray(embedding, dtype=np.float32)
    norm = float(np.linalg.norm(query))
    if norm == 0.0:
        return None, 0.0
    query = query / norm

    with _lock:
        people = _read()["people"]

    best_name, best_score = None, 0.0
    for key, person in people.items():
        for vec in person.get("embeddings", []):
            candidate = np.asarray(vec, dtype=np.float32)
            cnorm = float(np.linalg.norm(candidate))
            if cnorm == 0.0:
                continue
            score = float(np.dot(query, candidate / cnorm))
            if score > best_score:
                best_name, best_score = key, score

    if best_score >= threshold:
        return best_name, best_score
    return None, best_score


def display_name(key: str) -> str:
    with _lock:
        person = _read()["people"].get(_norm(key)) or {}
    return person.get("display_name") or key.title()


# ── Profiles and ownership (Phase 2) ─────────────────────────────────────────

PERSONA_KEYS = ("verbosity", "tone", "style")     # mirrors nora.persona.VALID


def owner() -> str | None:
    """The enrolled owner's key, or None if nobody is designated.

    `vision.face.owner` in config.yaml wins over the stored flag: a name in
    a file only the user edits is a stronger statement of intent than one
    set by voice, and it survives a face-memory wipe.
    """
    configured = str(_face_cfg().get("owner", "") or "").strip()
    if configured:
        return _norm(configured)
    with _lock:
        people = _read()["people"]
    for key, person in sorted(people.items()):
        if person.get("trusted"):
            return key
    return None


def is_trusted(key: str) -> bool:
    """Whether this person is the enrolled owner.

    Guest mode uses this to *restrict* behavior when someone else is in
    frame. It must never be read the other way round: a visible face is
    defeated by a printed photograph, so `is_trusted() is True` may not
    unlock anything, skip a confirmation, or raise a risk ceiling. See the
    asymmetry note in nora/security.py.
    """
    key = _norm(key)
    if not key:
        return False
    return key == owner()


def set_owner(name: str) -> str:
    """Designate one enrolled person as the owner. Returns their key.

    Exclusive: any previously trusted record is demoted in the same write,
    so `owner()` always has exactly one answer.
    """
    key = _norm(name)
    if not key:
        raise ValueError("A name is required to set the owner.")
    with _lock:
        data = _read()
        if key not in data["people"]:
            raise KeyError(name)
        for other, person in data["people"].items():
            person["trusted"] = (other == key)
        data["people"][key]["updated_at"] = time.time()
        _write(data)
    return key


def get_profile(key: str) -> dict[str, Any] | None:
    """Profile fields for one person, or None if they aren't enrolled."""
    key = _norm(key)
    with _lock:
        person = _read()["people"].get(key)
    if person is None:
        return None
    return {
        "key": key,
        "name": person.get("display_name") or key.title(),
        "greeting": person.get("greeting") or "",
        "persona": dict(person.get("persona") or {}),
        "trusted": is_trusted(key),
        "enrolled_at": person.get("enrolled_at", 0.0),
    }


def set_profile(
    name: str,
    *,
    greeting: str | None = None,
    persona: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Update a person's greeting or persona hints. Raises KeyError if unknown.

    Deliberately cannot set `trusted` — ownership goes through set_owner()
    so the exclusivity rule has one enforcement point.
    """
    key = _norm(name)
    with _lock:
        data = _read()
        person = data["people"].get(key)
        if person is None:
            raise KeyError(name)
        if greeting is not None:
            text = greeting.strip()
            if text:
                person["greeting"] = text
            else:
                person.pop("greeting", None)
        if persona is not None:
            merged = dict(person.get("persona") or {})
            for dim, value in persona.items():
                if dim not in PERSONA_KEYS:
                    continue
                if value:
                    merged[dim] = str(value).strip().lower()
                else:
                    merged.pop(dim, None)
            if merged:
                person["persona"] = merged
            else:
                person.pop("persona", None)
        person["updated_at"] = time.time()
        _write(data)
    return get_profile(key) or {}


def greeting_for(key: str) -> str:
    """What NORA says when this person arrives."""
    profile = get_profile(key)
    if profile and profile["greeting"]:
        return profile["greeting"]
    return f"Hey {display_name(key)}."


def persona_for(key: str) -> dict[str, str]:
    profile = get_profile(key)
    return profile["persona"] if profile else {}


def list_people() -> list[dict[str, Any]]:
    with _lock:
        people = _read()["people"]
    return [
        {
            "name": person.get("display_name") or key.title(),
            "key": key,
            "samples": len(person.get("embeddings", [])),
            "enrolled_at": person.get("enrolled_at", 0.0),
            "trusted": is_trusted(key),
            "greeting": person.get("greeting") or "",
            "persona": dict(person.get("persona") or {}),
        }
        for key, person in sorted(people.items())
    ]


def forget(name: str) -> bool:
    """Delete one person. Actually rewrites the file — no tombstones."""
    key = _norm(name)
    with _lock:
        data = _read()
        if key not in data["people"]:
            return False
        del data["people"][key]
        _write(data)
        return True


def forget_all() -> int:
    """Delete every enrolled person. Returns how many were removed."""
    with _lock:
        data = _read()
        count = len(data["people"])
        data["people"] = {}
        _write(data)
        return count


def count() -> int:
    with _lock:
        return len(_read()["people"])


def store_path() -> Path:
    return _PATH

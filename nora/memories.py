"""What NORA remembers, as the user sees it: listed, and forgotten (Sharp F).

"Remember that my locker code is 1234" lands in more places than the fact
itself: the knowledge collection keeps the fact and the utterance, the session
index and the ambient store keep the words, and the transcript keeps both
sides of the exchange. Forgetting only the fact left `recall` answering with
the rest. `forget` takes it out of each of them.

The list is what the user told NORA to keep (`inject_knowledge`), newest
first. Everything else in those stores is a log of what was said, which
`recall` searches but nobody asked to have kept as a fact.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("nora.memories")

# A fact shorter than this is only removed where it is the whole entry:
# "yes" inside every sentence that contains it is not the same memory.
_MIN_SUBSTRING = 8


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").lower())).strip()


def _same_memory(fact: str, text: str) -> bool:
    f, t = _norm(fact), _norm(text)
    if not f or not t:
        return False
    return f == t or (len(f) >= _MIN_SUBSTRING and f in t)


def list_memories(limit: int = 200) -> list[dict[str, Any]]:
    """[{id, text, ts}] for the facts the user asked NORA to keep, newest first."""
    from nora import cognitive_memory
    _, knowledge = cognitive_memory._get_collections()
    if knowledge is None:
        return []
    try:
        got = knowledge.get(where={"source": "manual"}, include=["metadatas", "documents"])
    except Exception as e:
        logger.warning("listing memories failed: %s", e)
        return []
    out = []
    for mid, doc, meta in zip(got.get("ids") or [], got.get("documents") or [],
                              got.get("metadatas") or []):
        meta = meta or {}
        out.append({"id": mid, "text": meta.get("text") or doc or "", "ts": float(meta.get("ts") or 0)})
    out.sort(key=lambda m: m["ts"], reverse=True)
    return out[:limit]


def forget(memory_id: str) -> dict[str, Any]:
    """Forget one remembered fact, wherever recall could find it again.
    Returns {"ok", "text", "removed": {store: count}}."""
    from nora import cognitive_memory
    episodes, knowledge = cognitive_memory._get_collections()
    if knowledge is None:
        return {"ok": False, "text": "", "removed": {}}
    try:
        got = knowledge.get(ids=[memory_id], include=["metadatas", "documents"])
    except Exception as e:
        logger.warning("memory lookup failed: %s", e)
        return {"ok": False, "text": "", "removed": {}}
    if not got.get("ids"):
        return {"ok": False, "text": "", "removed": {}}
    meta = (got.get("metadatas") or [{}])[0] or {}
    if meta.get("source") != "manual":
        return {"ok": False, "text": "", "removed": {}}     # not something the user kept
    fact = meta.get("text") or (got.get("documents") or [""])[0] or ""

    removed = {
        "knowledge": _purge_collection(knowledge, fact, always=[memory_id]),
        "episodes": _purge_collection(episodes, fact),
        "session_index": _purge_session_index(fact),
        "ambient": _purge_ambient(fact),
        "transcript": _purge_transcript(fact),
    }
    logger.info("forgot %r: %s", fact[:60], removed)
    return {"ok": True, "text": fact, "removed": removed}


def forget_matching(text: str) -> dict[str, Any]:
    """Forget the remembered fact that best matches `text` (by voice:
    "forget my locker code")."""
    words = set(_norm(text).split()) - {"my", "the", "a", "an", "that", "about", "is", "what"}
    best, score = None, 0
    for m in list_memories():
        s = len(words & set(_norm(m["text"]).split()))
        if s > score:
            best, score = m, s
    if best is None:
        return {"ok": False, "text": "", "removed": {}}
    return forget(best["id"])


# ── one store each ───────────────────────────────────────────────────────────

def _purge_collection(col, fact: str, always: list[str] | None = None) -> int:
    if col is None:
        return 0
    ids = list(always or [])
    try:
        got = col.get(include=["metadatas", "documents"])
        for mid, doc, meta in zip(got.get("ids") or [], got.get("documents") or [],
                                  got.get("metadatas") or []):
            meta = meta or {}
            texts = [doc or "", str(meta.get("text") or "")]
            if mid not in ids and any(_same_memory(fact, t) for t in texts):
                ids.append(mid)
        if ids:
            col.delete(ids=ids)
    except Exception as e:
        logger.warning("purging a collection failed: %s", e)
        return 0
    return len(ids)


def _purge_session_index(fact: str) -> int:
    from nora import session_index
    with session_index._lock:
        conn = session_index._connect()
        if conn is None:
            return 0
        try:
            rows = conn.execute("SELECT rowid, text FROM utterances").fetchall()
            gone = [r[0] for r in rows if _same_memory(fact, r[1])]
            for rowid in gone:
                conn.execute("DELETE FROM utterances WHERE rowid = ?", (rowid,))
            conn.commit()
            return len(gone)
        except Exception as e:
            logger.warning("purging the session index failed: %s", e)
            return 0


def _purge_ambient(fact: str) -> int:
    from nora import ambient
    with ambient._lock:
        data = ambient._read()
        entries = data.get("entries", [])
        kept = [e for e in entries if not _same_memory(fact, str(e.get("text", "")))]
        if len(kept) == len(entries):
            return 0
        data["entries"] = kept
        try:
            ambient._PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("purging the ambient store failed: %s", e)
            return 0
        return len(entries) - len(kept)


def _purge_transcript(fact: str) -> int:
    from nora import dialogue
    with dialogue._lock:
        before = list(dialogue._turns)
        kept = [u for u in before if not _same_memory(fact, u.text)]
        if len(kept) != len(before):
            dialogue._turns.clear()
            dialogue._turns.extend(kept)
        return len(before) - len(kept)

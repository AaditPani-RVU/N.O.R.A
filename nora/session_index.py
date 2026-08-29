"""Session index — exact-match recall over everything that was said.

`nora.cognitive_memory` already remembers, semantically: sentence-transformers
embeddings in a Chroma collection, nearest-neighbour lookup. That is the right
tool for "what was I worried about last week" and the wrong one for "what did I
say about FABSeg" — vector search retrieves things that are *like* the query,
and a proper noun has no neighbours. Ask for a specific token and an embedding
index returns five plausible paraphrases that don't contain it.

The other half of the problem is cost. `pipeline._warm_lazy_singletons` records
Chroma's embedding model at 12.4s cold against 22ms warm, and it holds an
80 MB model resident to answer a question that is, most of the time, a keyword
lookup. SQLite's FTS5 is in the standard library's sqlite3, needs no model, and
answers in single-digit milliseconds from a single file.

So this sits *beside* Chroma rather than replacing it, and `search_hybrid`
merges the two: exact hits first, semantic neighbours after, deduplicated. The
keyword index is cheap enough to always query and the vector index is left to
do the thing it is genuinely better at.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("nora.session_index")

_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _ROOT / "nora_sessions.db"

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_available: bool | None = None

# FTS5 treats these as syntax. A user saying "what about C++?" would otherwise
# produce a query error rather than a search.
_FTS_SPECIALS = re.compile(r'["*()^:,]|(?<!\w)-|\bNEAR\b|\bAND\b|\bOR\b|\bNOT\b')


@dataclass
class Entry:
    text: str
    role: str        # "user" | "nora"
    source: str      # "command" | "chat" | "ambient" | ...
    ts: float
    rank: float = 0.0

    def age(self) -> str:
        """Spoken age — 'yesterday', 'last Tuesday', 'three weeks ago'."""
        delta = time.time() - self.ts
        if delta < 3600:
            return "earlier today"
        if delta < 86400:
            return "today"
        if delta < 172800:
            return "yesterday"
        if delta < 604800:
            return time.strftime("%A", time.localtime(self.ts))
        weeks = int(delta // 604800)
        return "last week" if weeks == 1 else f"{weeks} weeks ago"


def _connect() -> sqlite3.Connection | None:
    """Open the index, creating the FTS5 table on first use.

    Returns None if the sqlite3 build has no FTS5 — it is compiled in on every
    mainstream Python, but a missing module here must degrade to "no keyword
    search" rather than take out the whole memory path.
    """
    global _conn, _available
    if _conn is not None:
        return _conn
    if _available is False:
        return None
    try:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS utterances "
            "USING fts5(text, role UNINDEXED, source UNINDEXED, ts UNINDEXED)"
        )
        conn.commit()
    except sqlite3.OperationalError as e:
        logger.warning("FTS5 unavailable, keyword recall disabled: %s", e)
        _available = False
        return None
    _conn = conn
    _available = True
    return _conn


def record(text: str, role: str = "user", source: str = "command") -> None:
    """Add one utterance to the index. Never raises."""
    text = (text or "").strip()
    if not text:
        return
    with _lock:
        conn = _connect()
        if conn is None:
            return
        try:
            conn.execute(
                "INSERT INTO utterances (text, role, source, ts) VALUES (?, ?, ?, ?)",
                (text, role, source, time.time()),
            )
            conn.commit()
        except Exception as e:
            logger.debug("session index write failed: %s", e)


def _sanitise(query: str) -> str:
    """Make a spoken phrase safe to hand to FTS5 as a MATCH expression."""
    cleaned = _FTS_SPECIALS.sub(" ", query or "")
    words = [w for w in cleaned.split() if w]
    if not words:
        return ""
    # Quote each term so a stray operator can never reach the query parser, and
    # OR them: a spoken question rarely repeats the indexed phrasing exactly, so
    # requiring every word finds nothing.
    return " OR ".join(f'"{w}"' for w in words)


def search(query: str, limit: int = 5, *, since: float | None = None) -> list[Entry]:
    """Keyword search, best match first. Returns [] on any failure."""
    expr = _sanitise(query)
    if not expr:
        return []
    with _lock:
        conn = _connect()
        if conn is None:
            return []
        sql = (
            "SELECT text, role, source, ts, rank FROM utterances "
            "WHERE utterances MATCH ?"
        )
        params: list = [expr]
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        sql += " ORDER BY rank LIMIT ?"
        params.append(int(limit))
        try:
            rows = conn.execute(sql, params).fetchall()
        except Exception as e:
            logger.debug("session index search failed: %s", e)
            return []
    return [Entry(text=r[0], role=r[1], source=r[2], ts=r[3], rank=r[4]) for r in rows]


def search_hybrid(query: str, limit: int = 5) -> list[Entry]:
    """Keyword hits first, then semantic neighbours, deduplicated.

    Exact matches lead deliberately. If the user named a thing, the utterance
    containing that name is the answer, and a semantically similar sentence is
    at best a consolation prize.
    """
    results = search(query, limit=limit)
    seen = {r.text.strip().lower() for r in results}

    if len(results) >= limit:
        return results

    try:
        from nora import cognitive_memory
        for hit in cognitive_memory.semantic_search(query, n=limit):
            text = (hit.get("text") or hit.get("document") or "").strip()
            if not text or text.lower() in seen:
                continue
            meta = hit.get("metadata") or {}
            results.append(Entry(
                text=text,
                role=meta.get("role", "user"),
                source=meta.get("source", "semantic"),
                ts=float(meta.get("timestamp", meta.get("ts", 0)) or 0),
            ))
            seen.add(text.lower())
            if len(results) >= limit:
                break
    except Exception as e:
        logger.debug("semantic half of hybrid search unavailable: %s", e)

    return results[:limit]


def count() -> int:
    with _lock:
        conn = _connect()
        if conn is None:
            return 0
        try:
            return int(conn.execute("SELECT count(*) FROM utterances").fetchone()[0])
        except Exception:
            return 0


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None


def _use_path_for_tests(path: Path) -> None:
    """Point the index at a temporary database."""
    global _DB_PATH, _available
    close()
    _DB_PATH = path
    _available = None

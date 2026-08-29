"""Recall command -- search the personal knowledge base by voice."""
from __future__ import annotations

import logging
import time

from nora import session_index
from nora.ambient import entry_count, search
from nora.command_engine import register

logger = logging.getLogger("nora.commands.recall")


@register("recall", sig="recall(query: str)",
           description="Search the personal knowledge base for past commands or things said.", category="memory")
def recall(query: str) -> str:
    """Search the personal knowledge base for things said or commanded before."""
    if not query or not query.strip():
        count = entry_count()
        return f"Your knowledge base has {count} entries. Ask me to recall something specific."

    # The FTS5 session index answers first. It is exact-match, so a proper noun
    # ("what did I say about FABSeg") lands on the utterance that actually
    # contains the word — which is the case pure vector search is worst at, and
    # it costs no embedding model. `search_hybrid` tops up with semantic
    # neighbours when keywords come up short.
    hits = session_index.search_hybrid(query.strip(), limit=4)
    if hits:
        parts = [
            f"{h.age()}, {'you said' if h.role == 'user' else 'I said'}: {h.text}"
            for h in hits
        ]
        intro = f"Found {len(hits)} match{'es' if len(hits) > 1 else ''}. "
        return intro + ". Next: ".join(parts[:2])

    # Fall back to the older ambient store, which holds everything logged
    # before the session index existed.
    results = search(query.strip(), limit=4)
    if not results:
        return f"Nothing in your knowledge base matches '{query}'."

    parts = []
    for entry in results:
        age = _age_label(entry["ts"])
        src = "you said" if entry["source"] == "ambient" else "you asked"
        parts.append(f"{age}, {src}: {entry['text']}")

    intro = f"Found {len(results)} match{'es' if len(results) > 1 else ''}. "
    return intro + ". Next: ".join(parts[:2])


def _age_label(ts: float) -> str:
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta / 60)} minutes ago"
    if delta < 86400:
        return f"{int(delta / 3600)} hours ago"
    return f"{int(delta / 86400)} days ago"

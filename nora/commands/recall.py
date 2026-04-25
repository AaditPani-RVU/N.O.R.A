"""Recall command â€” search the personal knowledge base by voice."""
from __future__ import annotations

import logging
import time

from nora.ambient import entry_count, search
from nora.command_engine import register

logger = logging.getLogger("nora.commands.recall")


@register("recall")
def recall(query: str) -> str:
    """Search the personal knowledge base for things said or commanded before."""
    if not query or not query.strip():
        count = entry_count()
        return f"Your knowledge base has {count} entries. Ask me to recall something specific."

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

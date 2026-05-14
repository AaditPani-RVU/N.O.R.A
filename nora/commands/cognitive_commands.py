"""Cognitive memory commands -- semantic recall, pattern introspection, knowledge injection."""
from __future__ import annotations

import time
from datetime import datetime

from nora.command_engine import register


@register("semantic_recall", sig="semantic_recall(query: str)",
           description="Deep semantic similarity search over all memory (smarter than recall)", category="memory")
def semantic_recall(query: str) -> str:
    """Semantic similarity search over episodes and knowledge base."""
    from nora.cognitive_memory import semantic_search
    hits = semantic_search(query, n=4, collection="both")
    if not hits:
        return "Nothing relevant found in memory."

    parts = []
    for h in hits:
        ts = h["meta"].get("ts", 0)
        when = datetime.fromtimestamp(ts).strftime("%b %d") if ts else "unknown"
        text = h["meta"].get("text", h["text"])[:120]
        parts.append(f"[{when}] {text}")

    return "Here's what I recall: " + ". Next: ".join(parts[:3])


@register("show_patterns", sig="show_patterns()",
           description="Show user's behavioral patterns (time-of-day habits, workflows)", category="memory")
def show_patterns() -> str:
    """Explain the user's behavioral patterns extracted from memory."""
    from nora.cognitive_memory import get_behavioral_patterns
    patterns = get_behavioral_patterns()

    lines = []
    time_pats = patterns.get("time_patterns", [])
    if time_pats:
        lines.append("Your time patterns:")
        for p in time_pats[:4]:
            lines.append(
                f"  On {p['when']}, you often {p['frequent_action'].replace('_', ' ')} "
                f"({p['count']} times)"
            )

    wf_pats = patterns.get("workflow_patterns", [])
    if wf_pats:
        lines.append("Your workflow habits:")
        for w in wf_pats[:3]:
            lines.append(
                f"  After {w['trigger'].replace('_', ' ')}, you usually "
                f"{w['follows'].replace('_', ' ')} ({w['confidence']} times)"
            )

    total = patterns.get("total_episodes", 0)
    lines.append(f"Total sessions analyzed: {total}")

    return " ".join(lines) if lines else "Not enough data yet to identify patterns."


@register("inject_knowledge", sig="inject_knowledge(text: str)",
           description="Store a fact or note in memory permanently", category="memory")
def inject_knowledge(text: str) -> str:
    """Manually inject a fact or note into the semantic knowledge base."""
    from nora.cognitive_memory import record_knowledge
    record_knowledge(text, source="manual", tags=["user_injected"])
    return f"Stored in memory: {text[:80]}"


@register("memory_status", sig="memory_status()",
           description="Show cognitive memory statistics", category="memory")
def memory_status() -> str:
    """Report cognitive memory system status and statistics."""
    from nora.cognitive_memory import _get_collections, _load_user_model
    eps, kn = _get_collections()
    eps_count = eps.count() if eps else 0
    kn_count = kn.count() if kn else 0
    model = _load_user_model()
    total = model.get("total_episodes", 0)
    bigrams = sum(len(v) for v in model.get("action_bigrams", {}).values())
    return (
        f"Cognitive memory: {eps_count} episodes, {kn_count} knowledge entries. "
        f"User model: {total} sessions, {bigrams} workflow transitions learned."
    )

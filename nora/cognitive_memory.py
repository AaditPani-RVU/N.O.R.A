"""Cognitive Memory System v2 â€” Semantic + Episodic memory engine.

Architecture:
  - ChromaDB persistent store (local, no cloud)
  - Two collections:
      "episodes"   : rich contextual events (commands, actions, outcomes)
      "knowledge"  : ambient speech + facts injected from any source
  - Embeddings: sentence-transformers all-MiniLM-L6-v2 (80 MB, GPU-optional)
  - Fallback: TF-IDF bag-of-words when sentence-transformers unavailable
  - Thread-safe; asyncio-compatible (no blocking the event loop)

User Model built on top:
  - time-of-day activity heatmap (6 bins Ã— 7 days)
  - action co-occurrence matrix (what follows what)
  - top contexts (what apps are open when user does X)
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("nora.cognitive_memory")

_ROOT = Path(__file__).resolve().parent.parent
_CHROMA_DIR = _ROOT / "nora_cognitive_db"
_USER_MODEL_PATH = _ROOT / "nora_user_model.json"

_lock = threading.RLock()

# â”€â”€ Lazy singletons â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_chroma_client = None
_episodes_col = None
_knowledge_col = None
_embedder = None
_embedder_ready = False
_tfidf_vocab: dict[str, int] = {}


def _get_embedder():
    global _embedder, _embedder_ready
    if _embedder_ready:
        return _embedder
    try:
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer("all-MiniLM-L6-v2")
        _embedder_ready = True
        logger.info("Embedder loaded: all-MiniLM-L6-v2")
    except Exception as e:
        logger.warning("sentence-transformers unavailable (%s) â€” using TF-IDF fallback", e)
        _embedder = None
        _embedder_ready = True
    return _embedder


def _tfidf_embed(text: str, dim: int = 384) -> list[float]:
    """Deterministic TF-IDF bag-of-words vector (fixed dim) as fallback embedding."""
    global _tfidf_vocab
    tokens = text.lower().split()
    vec = [0.0] * dim
    for token in tokens:
        if token not in _tfidf_vocab:
            idx = int(hashlib.md5(token.encode()).hexdigest(), 16) % dim
            _tfidf_vocab[token] = idx
        vec[_tfidf_vocab[token]] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _embed(text: str) -> list[float]:
    embedder = _get_embedder()
    if embedder is not None:
        return embedder.encode(text, normalize_embeddings=True).tolist()
    return _tfidf_embed(text)


def _get_collections():
    global _chroma_client, _episodes_col, _knowledge_col
    if _episodes_col is not None:
        return _episodes_col, _knowledge_col
    try:
        import chromadb
        _chroma_client = chromadb.PersistentClient(path=str(_CHROMA_DIR))
        _episodes_col = _chroma_client.get_or_create_collection(
            "episodes",
            metadata={"hnsw:space": "cosine"},
        )
        _knowledge_col = _chroma_client.get_or_create_collection(
            "knowledge",
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("ChromaDB loaded â€” episodes: %d, knowledge: %d",
                    _episodes_col.count(), _knowledge_col.count())
    except Exception as e:
        logger.error("ChromaDB init failed: %s â€” cognitive memory degraded", e)
        _episodes_col = None
        _knowledge_col = None
    return _episodes_col, _knowledge_col


# â”€â”€ User Model â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_user_model: dict[str, Any] | None = None

_TIME_BINS = ["midnight", "early_morning", "morning", "afternoon", "evening", "night"]


def _time_bin(dt: datetime) -> str:
    h = dt.hour
    if h < 4:   return "midnight"
    if h < 8:   return "early_morning"
    if h < 12:  return "morning"
    if h < 17:  return "afternoon"
    if h < 21:  return "evening"
    return "night"


def _load_user_model() -> dict[str, Any]:
    global _user_model
    if _user_model is not None:
        return _user_model
    try:
        if _USER_MODEL_PATH.exists():
            _user_model = json.loads(_USER_MODEL_PATH.read_text(encoding="utf-8"))
        else:
            _user_model = _blank_user_model()
    except Exception:
        _user_model = _blank_user_model()
    return _user_model


def _blank_user_model() -> dict[str, Any]:
    return {
        # time_bin -> day_of_week -> [action, ...]
        "activity_heatmap": {b: {str(d): [] for d in range(7)} for b in _TIME_BINS},
        # action -> {next_action: count}
        "action_bigrams": {},
        # action -> {app: count}  (which apps are open when action fires)
        "action_context": {},
        # Total episodes seen
        "total_episodes": 0,
    }


def _save_user_model() -> None:
    if _user_model is None:
        return
    try:
        _USER_MODEL_PATH.write_text(json.dumps(_user_model, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("User model save failed: %s", e)


def _update_user_model(actions: list[str], active_apps: list[str], ts: float) -> None:
    dt = datetime.fromtimestamp(ts)
    tb = _time_bin(dt)
    dow = str(dt.weekday())

    m = _load_user_model()

    for action in actions:
        m["activity_heatmap"][tb][dow].append(action)
        # trim to last 200 per slot
        m["activity_heatmap"][tb][dow] = m["activity_heatmap"][tb][dow][-200:]

    for i in range(len(actions) - 1):
        src, dst = actions[i], actions[i + 1]
        if src not in m["action_bigrams"]:
            m["action_bigrams"][src] = {}
        m["action_bigrams"][src][dst] = m["action_bigrams"][src].get(dst, 0) + 1

    for action in actions:
        if action not in m["action_context"]:
            m["action_context"][action] = {}
        for app in active_apps:
            m["action_context"][action][app] = m["action_context"][action].get(app, 0) + 1

    m["total_episodes"] = m.get("total_episodes", 0) + 1
    _save_user_model()


# â”€â”€ Public API â€” Episode recording â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def record_episode(
    text: str,
    intent: str,
    actions: list[str],
    outcomes: list[dict],          # [{action, success, message}]
    active_apps: list[str] | None = None,
    ts: float | None = None,
) -> None:
    """Persist a full interaction episode to semantic memory and update user model."""
    ts = ts or time.time()
    active_apps = active_apps or []
    dt = datetime.fromtimestamp(ts)

    doc = (
        f"User said: {text}. "
        f"Intent: {intent}. "
        f"Actions: {', '.join(actions)}. "
        f"Apps open: {', '.join(active_apps) or 'none'}."
    )

    meta = {
        "ts": ts,
        "intent": intent,
        "actions": json.dumps(actions),
        "apps": json.dumps(active_apps),
        "time_bin": _time_bin(dt),
        "day_of_week": dt.weekday(),
        "success": all(o.get("success", True) for o in outcomes),
        "text": text[:500],
    }

    uid = f"ep_{int(ts * 1000)}_{hashlib.md5(text.encode()).hexdigest()[:8]}"

    with _lock:
        try:
            episodes, _ = _get_collections()
            if episodes is not None:
                episodes.add(
                    documents=[doc],
                    embeddings=[_embed(doc)],
                    metadatas=[meta],
                    ids=[uid],
                )
        except Exception as e:
            logger.warning("Episode recording failed: %s", e)

        _update_user_model(actions, active_apps, ts)


def record_knowledge(text: str, source: str = "ambient", tags: list[str] | None = None) -> None:
    """Add a free-text fact/observation to the knowledge collection."""
    text = text.strip()
    if not text or len(text) < 5:
        return
    ts = time.time()
    uid = f"kn_{int(ts * 1000)}_{hashlib.md5(text.encode()).hexdigest()[:8]}"
    meta = {
        "ts": ts,
        "source": source,
        "tags": json.dumps(tags or []),
        "text": text[:500],
    }
    with _lock:
        try:
            _, knowledge = _get_collections()
            if knowledge is not None:
                knowledge.add(
                    documents=[text],
                    embeddings=[_embed(text)],
                    metadatas=[meta],
                    ids=[uid],
                )
        except Exception as e:
            logger.warning("Knowledge recording failed: %s", e)


# â”€â”€ Public API â€” Retrieval â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def semantic_search(query: str, n: int = 5, collection: str = "both") -> list[dict[str, Any]]:
    """Semantic similarity search. collection = 'episodes' | 'knowledge' | 'both'."""
    q_embed = _embed(query)
    results: list[dict[str, Any]] = []

    with _lock:
        eps, kn = _get_collections()
        try:
            if collection in ("episodes", "both") and eps and eps.count() > 0:
                r = eps.query(query_embeddings=[q_embed], n_results=min(n, eps.count()))
                for doc, meta in zip(r["documents"][0], r["metadatas"][0]):
                    results.append({"text": doc, "meta": meta, "source": "episode"})
        except Exception as e:
            logger.warning("Episode search failed: %s", e)
        try:
            if collection in ("knowledge", "both") and kn and kn.count() > 0:
                r = kn.query(query_embeddings=[q_embed], n_results=min(n, kn.count()))
                for doc, meta in zip(r["documents"][0], r["metadatas"][0]):
                    results.append({"text": doc, "meta": meta, "source": "knowledge"})
        except Exception as e:
            logger.warning("Knowledge search failed: %s", e)

    # Sort by recency (ts descending) as tiebreaker
    results.sort(key=lambda x: x["meta"].get("ts", 0), reverse=True)
    return results[:n]


def get_context_for_prompt(current_text: str = "", n: int = 3) -> dict[str, Any]:
    """Build LLM-ready context dict from cognitive memory."""
    m = _load_user_model()

    dt = datetime.now()
    tb = _time_bin(dt)
    dow = str(dt.weekday())

    # Most frequent actions in this time slot
    slot_actions = Counter(m["activity_heatmap"].get(tb, {}).get(dow, []))
    typical_actions = [a for a, _ in slot_actions.most_common(3)]

    # Top bigram predictions from top actions
    predictions = {}
    for action in typical_actions:
        bigrams = m["action_bigrams"].get(action, {})
        if bigrams:
            best = max(bigrams, key=bigrams.get)
            if bigrams[best] >= 2:
                predictions[action] = best

    # Semantic recall if query provided
    recent_relevant = []
    if current_text:
        hits = semantic_search(current_text, n=n, collection="both")
        recent_relevant = [h["text"][:150] for h in hits]

    return {
        "time_bin": tb,
        "typical_actions_now": typical_actions,
        "workflow_predictions": predictions,
        "relevant_context": recent_relevant,
        "total_episodes": m.get("total_episodes", 0),
    }


# â”€â”€ Behavioral pattern analysis â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def get_behavioral_patterns() -> list[dict[str, Any]]:
    """Extract human-readable behavioral patterns from user model."""
    m = _load_user_model()
    patterns = []

    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    for tb, days in m["activity_heatmap"].items():
        for dow, actions in days.items():
            if len(actions) < 3:
                continue
            c = Counter(actions)
            top = c.most_common(2)
            if top and top[0][1] >= 3:
                patterns.append({
                    "when": f"{day_names[int(dow)]} {tb.replace('_', ' ')}",
                    "frequent_action": top[0][0],
                    "count": top[0][1],
                    "secondary": top[1][0] if len(top) > 1 else None,
                })

    # Strong bigrams (seen 5+ times)
    strong_workflows = []
    for src, targets in m["action_bigrams"].items():
        for dst, count in targets.items():
            if count >= 5:
                strong_workflows.append({
                    "trigger": src,
                    "follows": dst,
                    "confidence": count,
                })
    strong_workflows.sort(key=lambda x: -x["confidence"])

    return {
        "time_patterns": sorted(patterns, key=lambda x: -x["count"]),
        "workflow_patterns": strong_workflows[:10],
        "total_episodes": m.get("total_episodes", 0),
    }


def predict_next_action(current_action: str, min_confidence: int = 3) -> str | None:
    """Predict the next most likely action (bigram model)."""
    m = _load_user_model()
    bigrams = m["action_bigrams"].get(current_action, {})
    if not bigrams:
        return None
    best = max(bigrams, key=bigrams.get)
    return best if bigrams[best] >= min_confidence else None


def get_proactive_suggestion() -> str | None:
    """Return a proactive action suggestion based on current time context."""
    m = _load_user_model()
    dt = datetime.now()
    tb = _time_bin(dt)
    dow = str(dt.weekday())

    slot_actions = Counter(m["activity_heatmap"].get(tb, {}).get(dow, []))
    if not slot_actions:
        return None

    top_action, count = slot_actions.most_common(1)[0]
    if count >= 5:
        return top_action
    return None


def get_recent_episodes(n: int = 20) -> list[dict[str, Any]]:
    """Return the n most recent interaction episodes (for history UI)."""
    with _lock:
        episodes, _ = _get_collections()
        if episodes is None:
            return []
        try:
            count = episodes.count()
            if count == 0:
                return []
            results = episodes.get(include=["metadatas", "documents"])
            metas = results.get("metadatas") or []
            docs  = results.get("documents") or []
            combined = []
            for meta, doc in zip(metas, docs):
                if not meta:
                    continue
                combined.append({
                    "ts":      meta.get("ts", 0),
                    "intent":  meta.get("intent", ""),
                    "text":    meta.get("text", doc[:120]),
                    "actions": json.loads(meta.get("actions", "[]")),
                    "success": meta.get("success", True),
                    "time_bin": meta.get("time_bin", ""),
                })
            combined.sort(key=lambda x: x["ts"], reverse=True)
            return combined[:n]
        except Exception as e:
            logger.warning("get_recent_episodes failed: %s", e)
            return []


def get_analytics() -> dict[str, Any]:
    """Return aggregate stats for the analytics UI panel."""
    m = _load_user_model()

    # Action frequencies from heatmap
    action_counts: Counter = Counter()
    for _tb, days in m.get("activity_heatmap", {}).items():
        for _dow, actions in days.items():
            action_counts.update(actions)

    top_actions = [{"action": a, "count": c} for a, c in action_counts.most_common(5)]

    # Success rate from episodes collection
    total_eps = m.get("total_episodes", 0)
    success_count = 0
    fail_count = 0
    with _lock:
        episodes, _ = _get_collections()
        if episodes is not None:
            try:
                count = episodes.count()
                if count > 0:
                    results = episodes.get(include=["metadatas"])
                    for meta in (results.get("metadatas") or []):
                        if meta:
                            if meta.get("success", True):
                                success_count += 1
                            else:
                                fail_count += 1
            except Exception:
                pass

    total_tracked = success_count + fail_count
    success_rate = round(success_count / total_tracked * 100) if total_tracked > 0 else 100

    return {
        "total_episodes": total_eps,
        "success_rate": success_rate,
        "success_count": success_count,
        "fail_count": fail_count,
        "top_actions": top_actions,
    }


def warm_up() -> None:
    """Initialize ChromaDB and embedder in a background thread."""
    def _init():
        _get_embedder()
        _get_collections()
        _load_user_model()
        logger.info("Cognitive memory warm-up complete")
    t = threading.Thread(target=_init, daemon=True, name="cognitive-warmup")
    t.start()

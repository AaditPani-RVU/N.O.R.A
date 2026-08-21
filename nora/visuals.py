"""Data producers for the dashboard's orb takeovers.

The dashboard's orb doubles as a display surface: on an intent it morphs
into a domain instrument, holds, then shrinks back (see the globe in
index.html). Each takeover needs a snapshot of some live subsystem, and
those snapshots have nothing to do with HTTP — so they live here rather
than growing ui_server.py, which stays a transport.

Two producers so far:

  process_snapshot()  the process constellation — psutil, sorted by load
  memory_graph()      the memory constellation — ChromaDB embeddings,
                      projected onto a sphere so semantically-near
                      memories land near each other

Both are read by GET routes in ui_server and are safe to call from the
HTTP thread: memory_graph() caches (the SVD is the expensive part) and
process_snapshot() never blocks on an interval.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any

logger = logging.getLogger("nora.visuals")

try:
    import psutil as _psutil
except ImportError:  # pragma: no cover - psutil is a hard dep in practice
    _psutil = None


# ══════════════════════════════════════════════════════════════════
#  PROCESS CONSTELLATION
# ══════════════════════════════════════════════════════════════════
#
# psutil's cpu_percent() is a *delta* measurement: the first call on a
# Process object always returns 0.0 because there is no previous sample
# to diff against. Passing an interval would fix that by sleeping, which
# would stall the single-threaded HTTP server for every other client.
#
# So instead the Process objects are kept alive between polls and each
# call diffs against the previous one. A brand-new process reads 0.0 on
# the poll that discovers it and reports truthfully from the next one.

_proc_cache: dict[int, Any] = {}
_proc_lock = threading.Lock()

# Ignore our own noise: these are the dashboard's own moving parts, and
# seeing NORA at the top of her own process chart is more confusing than
# useful when you are hunting for what is actually eating the machine.
_SELF_NAMES = {"nora", "nora-ui"}


def _proc_for(pid: int) -> Any:
    """A live Process object for *pid*, reused across polls for cpu deltas."""
    proc = _proc_cache.get(pid)
    if proc is None:
        proc = _psutil.Process(pid)
        proc.cpu_percent(None)          # prime; result is meaningless
        _proc_cache[pid] = proc
    return proc


def process_snapshot(limit: int = 44) -> dict[str, Any]:
    """Top processes by CPU then memory, shaped for the constellation.

    *limit* caps how many make the cut — the visual gets unreadable long
    before the machine runs out of processes, and the tail is all idle
    daemons anyway. `total` still reports the true count so the UI can
    say "44 of 312".
    """
    if _psutil is None:
        return {"procs": [], "total": 0, "error": "psutil not installed"}

    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    total = 0

    for handle in _psutil.process_iter(["pid", "name", "username"]):
        info = handle.info
        pid = info.get("pid")
        if pid is None:
            continue
        total += 1
        seen.add(pid)
        try:
            with _proc_lock:
                proc = _proc_for(pid)
            cpu = proc.cpu_percent(None)
            rss = proc.memory_info().rss
        except Exception:
            # Process exited between the iterator and the read, or it
            # belongs to another user and is not ours to inspect.
            continue
        rows.append({
            "pid": pid,
            "name": (info.get("name") or "?")[:28],
            "user": (info.get("username") or "")[:18],
            "cpu": round(cpu, 1),
            "rss": rss,
        })

    # Drop cached handles for processes that have exited, or the cache
    # grows for the lifetime of the daemon.
    with _proc_lock:
        for dead in [p for p in _proc_cache if p not in seen]:
            _proc_cache.pop(dead, None)

    try:
        vm = _psutil.virtual_memory()
        mem_total = vm.total
    except Exception:
        mem_total = 0

    for row in rows:
        row["mem_pct"] = round(row["rss"] / mem_total * 100, 2) if mem_total else 0.0
        row["self"] = row["name"].lower() in _SELF_NAMES

    # CPU first, memory as the tiebreak: a 0%-CPU process holding 4 GB is
    # exactly the thing you opened this view to find.
    rows.sort(key=lambda r: (r["cpu"], r["rss"]), reverse=True)

    try:
        cpu_count = _psutil.cpu_count() or 1
        load = _psutil.cpu_percent(None)
    except Exception:
        cpu_count, load = 1, 0.0

    return {
        "procs": rows[:limit],
        "total": total,
        "shown": min(len(rows), limit),
        "cpu_count": cpu_count,
        "cpu_total": round(load, 1),
        "mem_total": mem_total,
        "ts": time.time(),
    }


def kill_process(pid: int, force: bool = False) -> dict[str, Any]:
    """Terminate *pid*. SIGTERM by default, SIGKILL when *force*.

    Returns a result dict rather than raising: this is reached from a
    click on a canvas, and the caller wants something to render, not an
    exception to swallow.
    """
    if _psutil is None:
        return {"ok": False, "error": "psutil not installed"}
    try:
        proc = _psutil.Process(int(pid))
        name = proc.name()
        if force:
            proc.kill()
        else:
            proc.terminate()
        return {"ok": True, "pid": int(pid), "name": name,
                "signal": "KILL" if force else "TERM"}
    except _psutil.NoSuchProcess:
        return {"ok": False, "error": f"no process {pid}"}
    except _psutil.AccessDenied:
        return {"ok": False, "error": f"permission denied for {pid}"}
    except Exception as exc:                       # pragma: no cover
        logger.exception("kill_process(%s) failed", pid)
        return {"ok": False, "error": str(exc)}


# ══════════════════════════════════════════════════════════════════
#  MEMORY CONSTELLATION
# ══════════════════════════════════════════════════════════════════
#
# Every memory is already a 384-d embedding in ChromaDB. To draw them we
# need three dimensions, and the honest reduction for embeddings compared
# by cosine is: L2-normalise, take the top three principal components,
# then normalise again so every point lands on a unit sphere.
#
# Landing on a sphere is not a compromise, it is the point — the existing
# globe renderer already projects a unit sphere with rotation and tilt,
# so the constellation reuses that code path verbatim and every memory
# gets a lat/lon. Semantically similar memories come out near each other.
#
# Edges are computed in the ORIGINAL 384-d space, not the projected one.
# Nearest-neighbour in 3D after a lossy projection would draw lines
# between things that only look close on screen.

_graph_cache: dict[str, Any] | None = None
_graph_stamp: float = 0.0
_graph_counts: tuple[int, ...] = ()
_graph_lock = threading.Lock()

_GRAPH_TTL = 90.0          # seconds; the SVD is the expensive part
_MAX_NODES = 700           # keeps the payload and the frame budget sane


def _pca3(mat: Any) -> Any:
    """Top three principal components of *mat* (N x D), as N x 3."""
    import numpy as np

    centred = mat - mat.mean(axis=0, keepdims=True)
    # full_matrices=False keeps this an N x min(N,D) job rather than a
    # D x D one — at 384 dims either is fine, but it is free to be right.
    _u, sv, vt = np.linalg.svd(centred, full_matrices=False)
    scores = centred @ vt[:3].T
    # Whiten: divide each component by its singular value so all three carry
    # equal variance. Without this the first component dominates, every point
    # normalises toward the same axis, and the constellation bunches into one
    # hemisphere with the rest of the sphere empty. Whitening spends the whole
    # surface, which is the only reason to be on a sphere at all.
    scale = sv[:3].copy()
    scale[scale == 0] = 1.0
    return scores / scale


def _collection_rows(client: Any, name: str, kind: str) -> list[dict[str, Any]]:
    """Pull one Chroma collection into plain dicts, embeddings included."""
    import numpy as np

    try:
        col = client.get_collection(name)
    except Exception:
        logger.debug("collection %s unavailable", name, exc_info=True)
        return []

    try:
        got = col.get(include=["embeddings", "documents", "metadatas"])
    except Exception:
        logger.debug("collection %s read failed", name, exc_info=True)
        return []

    ids = got.get("ids") or []
    embs = got.get("embeddings")
    docs = got.get("documents") or []
    metas = got.get("metadatas") or []
    # Chroma returns a numpy array for embeddings and None when empty;
    # `or []` on an array raises, so the length check has to be explicit.
    if embs is None or len(embs) == 0:
        return []

    rows: list[dict[str, Any]] = []
    for i, node_id in enumerate(ids):
        meta = metas[i] if i < len(metas) and isinstance(metas[i], dict) else {}
        text = docs[i] if i < len(docs) else ""
        rows.append({
            "id": f"{kind}:{node_id}",
            "kind": kind,
            # Trimmed hard: 700 nodes of full episode text is a megabyte of
            # JSON for labels that render as one line.
            "text": (str(text or meta.get("text") or ""))[:160],
            "ts": float(meta.get("ts") or 0.0),
            "source": str(meta.get("source") or meta.get("intent") or ""),
            "ok": bool(meta.get("success", True)),
            "_emb": np.asarray(embs[i], dtype="float32"),
        })
    return rows


def memory_graph(neighbours: int = 3, force: bool = False) -> dict[str, Any]:
    """Memory embeddings projected onto a sphere, plus similarity edges.

    Cached for _GRAPH_TTL and invalidated early when a collection's row
    count changes, so a memory added mid-session shows up on the next
    look rather than ninety seconds later.
    """
    global _graph_cache, _graph_stamp, _graph_counts

    import numpy as np

    with _graph_lock:
        try:
            import chromadb
        except Exception as exc:
            return {"nodes": [], "edges": [], "error": f"chromadb unavailable: {exc}"}

        try:
            client = chromadb.PersistentClient(path=_db_path())
            counts = tuple(
                client.get_collection(n).count() for n in ("knowledge", "episodes")
            )
        except Exception as exc:
            logger.debug("chroma open failed", exc_info=True)
            return {"nodes": [], "edges": [], "error": f"memory store unreadable: {exc}"}

        fresh = (time.time() - _graph_stamp) < _GRAPH_TTL
        if _graph_cache is not None and fresh and counts == _graph_counts and not force:
            return _graph_cache

        rows = (_collection_rows(client, "knowledge", "knowledge")
                + _collection_rows(client, "episodes", "episode"))
        if not rows:
            return {"nodes": [], "edges": [], "total": 0}

        # Newest first, so the cap keeps what you are most likely to be
        # asking about rather than an arbitrary slice.
        rows.sort(key=lambda r: r["ts"], reverse=True)
        rows = rows[:_MAX_NODES]

        mat = np.vstack([r.pop("_emb") for r in rows])
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        unit = mat / np.where(norms == 0, 1.0, norms)

        pts = _pca3(unit)
        pnorm = np.linalg.norm(pts, axis=1, keepdims=True)
        sphere = pts / np.where(pnorm == 0, 1.0, pnorm)

        for row, (x, y, z) in zip(rows, sphere):
            row["lat"] = round(math.degrees(math.asin(max(-1.0, min(1.0, float(z))))), 3)
            row["lon"] = round(math.degrees(math.atan2(float(y), float(x))), 3)

        # Cosine similarity in the original space. unit is already L2
        # normalised, so the gram matrix *is* the cosine matrix.
        sim = unit @ unit.T
        np.fill_diagonal(sim, -1.0)
        k = max(1, min(neighbours, len(rows) - 1))
        top = np.argpartition(-sim, kth=k - 1, axis=1)[:, :k]

        edges: list[list[Any]] = []
        seen: set[tuple[int, int]] = set()
        for i, js in enumerate(top):
            for j in js:
                j = int(j)
                pair = (min(i, j), max(i, j))
                if pair in seen:
                    continue
                seen.add(pair)
                score = float(sim[i, j])
                # Below this the line says nothing except "both are text".
                if score < 0.45:
                    continue
                edges.append([pair[0], pair[1], round(score, 3)])

        result = {
            "nodes": rows,
            "edges": edges,
            "total": sum(counts),
            "counts": {"knowledge": counts[0], "episodes": counts[1]},
            "ts": time.time(),
        }
        _graph_cache, _graph_stamp, _graph_counts = result, time.time(), counts
        return result


# The cold build is a ~2 s SVD over every memory, and ui_server runs a
# single-threaded HTTPServer — computing that inline would freeze metrics
# polling for every other client until it finished. So the route asks for
# the cache, and a miss kicks a daemon thread and reports "building" until
# the next poll finds it ready.

_graph_building = False


def memory_graph_async() -> dict[str, Any]:
    """The cached graph, or a `building` marker while a thread computes it."""
    global _graph_building

    fresh = (_graph_cache is not None
             and (time.time() - _graph_stamp) < _GRAPH_TTL)
    if fresh:
        return _graph_cache                    # type: ignore[return-value]

    if not _graph_building:
        _graph_building = True

        def _build() -> None:
            global _graph_building
            try:
                memory_graph(force=True)
            except Exception:
                logger.exception("memory graph build failed")
            finally:
                _graph_building = False

        threading.Thread(target=_build, daemon=True,
                         name="nora-memory-graph").start()

    # A stale cache still beats a spinner: hand back what we have and let
    # the rebuild swap it in underneath.
    if _graph_cache is not None:
        return {**_graph_cache, "stale": True}
    return {"nodes": [], "edges": [], "building": True}


def warm() -> None:
    """Kick the first graph build at startup so the first look is instant."""
    memory_graph_async()


def _db_path() -> str:
    """Where cognitive_memory keeps its Chroma store."""
    try:
        from nora import cognitive_memory
        for attr in ("_CHROMA_DIR", "DB_PATH", "CHROMA_PATH"):
            val = getattr(cognitive_memory, attr, None)
            if val:
                return str(val)
    except Exception:
        pass
    return "nora_cognitive_db"


def memory_focus(query: str, top: int = 3) -> list[str]:
    """Node ids most similar to *query*, for highlighting after a recall.

    Returns ids from the same graph the dashboard is drawing, so the
    frontend can light them up without a second projection.
    """
    graph = memory_graph()
    nodes = graph.get("nodes") or []
    if not nodes or not query.strip():
        return []
    needle = query.strip().lower()
    scored = [
        (sum(w in (n.get("text") or "").lower() for w in needle.split()), n["id"])
        for n in nodes
    ]
    scored.sort(reverse=True)
    return [node_id for hits, node_id in scored[:top] if hits]

"""Background job queue — work that outlives the turn that asked for it.

Every path through `pipeline.run_turn` is synchronous with the microphone: the
user speaks, NORA works, NORA answers, the loop comes back around. That is the
right shape for "open Chrome" and the wrong shape for "why is my training loss
diverging" — and the seam between them is a hard timeout. `command_engine`
kills any step at `timeouts.command_sec` (45s); the conversation path gives up
at `timeouts.llm_sec`. A question that genuinely needs two minutes of a large
model cannot be answered inside either budget, so it wasn't.

What made that worse than a plain timeout: the chat model, asked something
hard, would say *"let me get back to you on that"* — and then nothing. There
was no queue to get back from. It was a promise the process had no machinery
to keep, and the only honest fix is to build the machinery rather than to
prompt the model out of making the promise.

So: `submit()` puts a callable on a worker thread and returns immediately.
When it finishes, the result is *spoken unprompted* through the same
focus-gated channel `proactive`, `terminal_monitor` and `anomaly_watchdog`
already use — NORA comes back to you, in speech, without being asked again.

Jobs are durable. They live in `nora_jobs.json` alongside the other root state
files, so a job that was still running when NORA went down is visible on the
next boot instead of vanishing silently. Delivery is *not* replayed across a
restart — speaking the answer to a question asked yesterday, out of nowhere,
would be worse than not answering — but the answer is kept and `pending()` /
`recent()` let the user ask for it ("what did you find out about X?").
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("nora.jobs")

_ROOT = Path(__file__).resolve().parent.parent
_JOBS_PATH = _ROOT / "nora_jobs.json"

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

# Terminal states — a job in one of these will never run again.
_FINISHED = (STATUS_DONE, STATUS_FAILED)

# How many jobs run at once. Deliberately small: these are LLM calls and
# subprocesses, and a voice assistant answering three things at once over the
# top of each other is a worse experience than one that answers them in order.
_MAX_WORKERS = 2

# Keep the tail of finished jobs so "what did you find out about X" has
# something to search. Older entries are dropped on save.
_HISTORY_LIMIT = 50


@dataclass
class Job:
    id: str
    title: str            # short, spoken back to the user ("your question about X")
    kind: str = "answer"  # "answer" | "cron" | free-form; used for phrasing only
    status: str = STATUS_QUEUED
    result: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    delivered: bool = False
    # Whether to speak the result when it lands. Cron jobs that only write a
    # file set this False.
    deliver: bool = True
    # Set on jobs restored from disk at boot: their result is retrievable but
    # is never spoken unprompted, because the moment for it has passed.
    stale: bool = False

    def duration(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.time()
        return end - self.started_at


_lock = threading.RLock()
_jobs: dict[str, Job] = {}
_queue: "queue.Queue[str]" = queue.Queue()
# job id -> the callable to run. Deliberately *not* persisted: a function
# reference cannot be JSON, and resurrecting arbitrary callables across a
# restart is not a thing to do by accident.
_work: dict[str, Callable[[], str]] = {}

_workers: list[threading.Thread] = []
_stop = threading.Event()
_speak: Callable[[str], None] | None = None
_started = False


# ── Persistence ──────────────────────────────────────────────────────────────

def _load() -> None:
    """Read jobs from disk. Anything left mid-flight is marked failed.

    A job in `running` at load time means the process died holding it. It has
    no callable any more (see `_work`), so it can never complete — recording
    that honestly beats leaving a permanent phantom in `pending()`.
    """
    if not _JOBS_PATH.exists():
        return
    try:
        raw = json.loads(_JOBS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Could not read %s: %s", _JOBS_PATH.name, e)
        return

    known = set(Job.__dataclass_fields__)
    for entry in raw.get("jobs", []):
        try:
            job = Job(**{k: v for k, v in entry.items() if k in known})
        except Exception:
            continue
        if job.status in (STATUS_QUEUED, STATUS_RUNNING):
            job.status = STATUS_FAILED
            job.error = "Interrupted by shutdown."
            job.finished_at = time.time()
        # Nothing recovered from disk gets spoken out of the blue.
        job.stale = True
        job.delivered = True
        _jobs[job.id] = job


def _save() -> None:
    """Write jobs to disk, newest first, trimmed to `_HISTORY_LIMIT`."""
    with _lock:
        ordered = sorted(_jobs.values(), key=lambda j: j.created_at, reverse=True)
        keep = ordered[:_HISTORY_LIMIT]
        for job in ordered[_HISTORY_LIMIT:]:
            _jobs.pop(job.id, None)
            _work.pop(job.id, None)
        payload = {"jobs": [asdict(j) for j in keep]}
    try:
        tmp = _JOBS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(_JOBS_PATH)
    except Exception as e:
        logger.warning("Could not save jobs: %s", e)


# ── Public API ───────────────────────────────────────────────────────────────

def submit(
    title: str,
    work: Callable[[], str],
    *,
    kind: str = "answer",
    deliver: bool = True,
) -> str:
    """Queue `work` to run off-turn. Returns the job id immediately.

    `work` takes no arguments and returns the text to speak — bind arguments
    with a lambda or `functools.partial` at the call site. It runs on a worker
    thread, so it must not touch the asyncio loop.
    """
    job = Job(id=uuid.uuid4().hex[:12], title=title.strip(), kind=kind, deliver=deliver)
    with _lock:
        _jobs[job.id] = job
        _work[job.id] = work
    _queue.put(job.id)
    _save()
    logger.info("Job queued [%s] %s", job.id, job.title)
    return job.id


def get(job_id: str) -> Job | None:
    with _lock:
        return _jobs.get(job_id)


def pending() -> list[Job]:
    """Jobs still queued or running, oldest first."""
    with _lock:
        live = [j for j in _jobs.values() if j.status not in _FINISHED]
    return sorted(live, key=lambda j: j.created_at)


def recent(limit: int = 5, *, kind: str | None = None) -> list[Job]:
    """Finished jobs, newest first."""
    with _lock:
        done = [
            j for j in _jobs.values()
            if j.status in _FINISHED and (kind is None or j.kind == kind)
        ]
    return sorted(done, key=lambda j: j.finished_at, reverse=True)[:limit]


def find(query: str) -> Job | None:
    """Best-effort lookup for "what did you find out about X?".

    Word-overlap against the title, which is the user's own phrasing of the
    question. Finished jobs win over running ones — if two match, the one with
    an answer is the more useful reply.
    """
    words = {w for w in query.lower().split() if len(w) > 3}
    if not words:
        return None
    best: tuple[int, float, Job] | None = None
    with _lock:
        candidates = list(_jobs.values())
    for job in candidates:
        overlap = len(words & set(job.title.lower().split()))
        if not overlap:
            continue
        rank = (overlap + (2 if job.status == STATUS_DONE else 0), job.created_at, job)
        if best is None or rank[:2] > best[:2]:
            best = rank
    return best[2] if best else None


def describe_pending() -> str:
    """One spoken line about what's still in flight."""
    live = pending()
    if not live:
        return "Nothing pending."
    if len(live) == 1:
        return f"Still working on {live[0].title}."
    titles = ", ".join(j.title for j in live[:3])
    return f"{len(live)} things in flight: {titles}."


# ── Worker loop ──────────────────────────────────────────────────────────────

def _deliver(job: Job) -> None:
    """Speak a finished job's result, unprompted.

    Routed through the focus-gated speak callback, so a job that lands while
    the user is in a meeting or deep in focus is held and flushed on their next
    interaction rather than barging in — that gating is `nora.focus`'s job, not
    this module's.
    """
    if _speak is None or not job.deliver or job.stale:
        return
    if job.status == STATUS_FAILED:
        text = f"I couldn't finish {job.title}. {job.error}".strip()
    else:
        text = _phrase_answer(job)
    try:
        _speak(text)
        with _lock:
            job.delivered = True
        _save()
    except Exception as e:
        logger.warning("Delivery failed for job %s: %s", job.id, e)


def _phrase_answer(job: Job) -> str:
    """Frame a result as a callback rather than a reply.

    The user asked this minutes ago and has been doing something else since.
    Dropping straight into the answer with no referent is disorienting, so
    every delivery re-states what it is answering first.
    """
    if job.kind == "cron":
        return job.result
    return f"Back to {job.title} — {job.result}"


def _run_one(job_id: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        work = _work.get(job_id)
    if job is None or work is None:
        return

    with _lock:
        job.status = STATUS_RUNNING
        job.started_at = time.time()
    _save()

    try:
        result = work() or ""
        with _lock:
            job.result = str(result).strip()
            job.status = STATUS_DONE
    except Exception as e:
        logger.error("Job %s failed: %s\n%s", job_id, e, traceback.format_exc())
        with _lock:
            job.error = str(e)
            job.status = STATUS_FAILED
    finally:
        with _lock:
            job.finished_at = time.time()
            _work.pop(job_id, None)
        _save()
        logger.info("Job %s %s in %.1fs", job_id, job.status, job.duration())
        _deliver(job)


def _worker(index: int) -> None:
    while not _stop.is_set():
        try:
            job_id = _queue.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            _run_one(job_id)
        finally:
            _queue.task_done()


def start(speak_callback: Callable[[str], None] | None = None) -> None:
    """Start the worker threads. Safe to call twice."""
    global _speak, _started
    if speak_callback is not None:
        _speak = speak_callback
    if _started:
        return
    _load()
    _stop.clear()
    for i in range(_MAX_WORKERS):
        t = threading.Thread(target=_worker, args=(i,), daemon=True, name=f"nora-job-{i}")
        t.start()
        _workers.append(t)
    _started = True
    logger.info("Job queue started with %d workers", _MAX_WORKERS)


def stop() -> None:
    """Signal the workers to finish the current job and exit."""
    global _started
    _stop.set()
    for t in _workers:
        t.join(timeout=2.0)
    _workers.clear()
    _started = False


def reset_for_tests(speak_callback: Callable[[str], None] | None = None) -> None:
    """Clear in-memory state. Tests only — does not touch the on-disk file."""
    global _speak, _started
    stop()
    with _lock:
        _jobs.clear()
        _work.clear()
    while not _queue.empty():
        try:
            _queue.get_nowait()
            _queue.task_done()
        except queue.Empty:
            break
    _speak = speak_callback
    _started = False


def drain(timeout: float = 10.0) -> bool:
    """Block until every queued job has finished. Tests and shutdown only."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pending():
            return True
        time.sleep(0.02)
    return False

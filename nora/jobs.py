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

Jobs are durable. They live in the `jobs` table of the core store
(`nora.store`), so a job that was still running when NORA went down is visible on the
next boot instead of vanishing silently. Delivery is *not* replayed across a
restart — speaking the answer to a question asked yesterday, out of nowhere,
would be worse than not answering — but the answer is kept and `pending()` /
`recent()` let the user ask for it ("what did you find out about X?").
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
import uuid
from dataclasses import astuple, dataclass, field, fields
from typing import Callable

from nora import channel, delivery, store

logger = logging.getLogger("nora.jobs")

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
    # The device whose request queued this job; the answer goes back there.
    device: str = "local"

    def duration(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.time()
        return end - self.started_at


_COLUMNS = tuple(f.name for f in fields(Job))
_BOOL_COLUMNS = ("delivered", "deliver", "stale")

# Serialises this process's read-modify-write of a job row. Cross-process
# safety comes from the store's transactions; this only keeps the two workers
# and the submitting thread from racing each other.
_lock = threading.RLock()
_queue: "queue.Queue[str]" = queue.Queue()
# job id -> the callable to run. Deliberately *not* persisted: a function
# reference cannot be stored, and resurrecting arbitrary callables across a
# restart is not a thing to do by accident.
_work: dict[str, Callable[[], str]] = {}

_workers: list[threading.Thread] = []
_stop = threading.Event()
_speak: Callable[[str], None] | None = None
_started = False


# ── Persistence ──────────────────────────────────────────────────────────────

def _row_to_job(row) -> Job:
    data = {k: row[k] for k in _COLUMNS}
    for k in _BOOL_COLUMNS:
        data[k] = bool(data[k])
    return Job(**data)


def _insert(job: Job) -> None:
    with store.transaction() as conn:
        conn.execute(
            f"INSERT INTO jobs ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})",
            astuple(job),
        )
    _trim()


def _update(job_id: str, **changes) -> None:
    assigns = ", ".join(f"{k} = ?" for k in changes)
    with store.transaction() as conn:
        conn.execute(f"UPDATE jobs SET {assigns} WHERE id = ?", (*changes.values(), job_id))


def _select(where: str = "", params: tuple = (), order: str = "created_at") -> list[Job]:
    sql = f"SELECT {', '.join(_COLUMNS)} FROM jobs"
    if where:
        sql += f" WHERE {where}"
    sql += f" ORDER BY {order}"
    return [_row_to_job(r) for r in store.query(sql, params)]


def _trim() -> None:
    """Keep the newest `_HISTORY_LIMIT` finished jobs; live ones are never dropped."""
    with store.transaction() as conn:
        conn.execute(
            "DELETE FROM jobs WHERE status IN (?, ?) AND id NOT IN ("
            " SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?)",
            (*_FINISHED, _HISTORY_LIMIT),
        )


def _load() -> None:
    """Settle jobs left behind by the last run. Anything mid-flight is failed.

    A job still `queued` or `running` at boot means the process died holding
    it. It has no callable any more (see `_work`), so it can never complete —
    recording that honestly beats leaving a permanent phantom in `pending()`.
    Nothing recovered from a previous run gets spoken out of the blue.
    """
    with store.transaction() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, error = 'Interrupted by shutdown.', finished_at = ?"
            " WHERE status IN (?, ?)",
            (STATUS_FAILED, time.time(), STATUS_QUEUED, STATUS_RUNNING),
        )
        conn.execute("UPDATE jobs SET stale = 1, delivered = 1 WHERE stale = 0")


# ── Public API ───────────────────────────────────────────────────────────────

def submit(
    title: str,
    work: Callable[[], str],
    *,
    kind: str = "answer",
    deliver: bool = True,
    device: str | None = None,
) -> str:
    """Queue `work` to run off-turn. Returns the job id immediately.

    `work` takes no arguments and returns the text to speak — bind arguments
    with a lambda or `functools.partial` at the call site. It runs on a worker
    thread, so it must not touch the asyncio loop.

    `device` is where the answer goes; by default, the device whose turn is
    queueing the job (`nora.channel.current()`), else the local speaker.
    """
    if device is None:
        ch = channel.current()
        device = ch.device_id if ch else channel.LOCAL_DEVICE
    job = Job(id=uuid.uuid4().hex[:12], title=title.strip(), kind=kind,
              deliver=deliver, device=device)
    _insert(job)
    with _lock:
        _work[job.id] = work
    _queue.put(job.id)
    logger.info("Job queued [%s] %s", job.id, job.title)
    return job.id


def get(job_id: str) -> Job | None:
    found = _select("id = ?", (job_id,))
    return found[0] if found else None


def pending() -> list[Job]:
    """Jobs still queued or running, oldest first."""
    return _select("status NOT IN (?, ?)", _FINISHED)


def recent(limit: int = 5, *, kind: str | None = None) -> list[Job]:
    """Finished jobs, newest first."""
    where, params = "status IN (?, ?)", _FINISHED
    if kind is not None:
        where, params = where + " AND kind = ?", (*_FINISHED, kind)
    return _select(where, params, order="finished_at DESC")[:limit]


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
    for job in _select():
        overlap = len(words & set(job.title.lower().split()))
        if not overlap:
            continue
        rank = (overlap + (2 if job.status == STATUS_DONE else 0), job.created_at, job)
        if best is None or rank[:2] > best[:2]:
            best = rank
    return best[2] if best else None


# Held answers older than this are left for recall instead of being pushed:
# a reply to something asked yesterday, arriving out of nowhere, is noise.
_FLUSH_MAX_AGE_SEC = 12 * 3600


def flush(device_id: str) -> int:
    """Deliver answers held while `device_id` was offline. Returns how many."""
    held = _select(
        "device = ? AND delivered = 0 AND deliver = 1 AND stale = 0"
        " AND status IN (?, ?) AND finished_at > ?",
        (device_id, *_FINISHED, time.time() - _FLUSH_MAX_AGE_SEC),
        order="finished_at",
    )
    for job in held:
        _deliver(job)
    return sum(1 for j in held if j.delivered)


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
    """Say a finished job's result, unprompted, to the device that asked.

    A device job goes through `nora.delivery`; if that device is offline the
    row stays undelivered and `flush()` sends it when the device reconnects.
    A local job goes through the focus-gated speak callback, so one that lands
    while the user is in a meeting or deep in focus is held and flushed on
    their next interaction rather than barging in — that gating is
    `nora.focus`'s job, not this module's.
    """
    if not job.deliver or job.stale:
        return
    if job.status == STATUS_FAILED:
        text = f"I couldn't finish {job.title}. {job.error}".strip()
    else:
        text = _phrase_answer(job)
    if job.device != channel.LOCAL_DEVICE:
        if delivery.deliver(text, device=job.device, kind=job.kind):
            _update(job.id, delivered=1)
            job.delivered = True
        else:
            logger.info("Job %s held for %s (not connected)", job.id, job.device)
        return
    if job.kind == "reminder":
        # The core stays at home; the user may not. A reminder set by voice
        # is said in the room and also lands on every connected phone.
        delivery.broadcast(text, kind=job.kind)
    if _speak is None:
        return
    try:
        _speak(text)
        _update(job.id, delivered=1)
        job.delivered = True
    except Exception as e:
        logger.warning("Delivery failed for job %s: %s", job.id, e)


def _phrase_answer(job: Job) -> str:
    """Frame a result as a callback rather than a reply.

    The user asked this minutes ago and has been doing something else since.
    Dropping straight into the answer with no referent is disorienting, so
    every delivery re-states what it is answering first.
    """
    if job.kind in ("cron", "reminder"):
        return job.result
    return f"Back to {job.title} — {job.result}"


def _run_one(job_id: str) -> None:
    with _lock:
        work = _work.get(job_id)
    job = get(job_id)
    if job is None or work is None:
        return

    job.status = STATUS_RUNNING
    job.started_at = time.time()
    _update(job_id, status=job.status, started_at=job.started_at)

    try:
        result = work() or ""
        job.result = str(result).strip()
        job.status = STATUS_DONE
    except Exception as e:
        logger.error("Job %s failed: %s\n%s", job_id, e, traceback.format_exc())
        job.error = str(e)
        job.status = STATUS_FAILED
    finally:
        job.finished_at = time.time()
        with _lock:
            _work.pop(job_id, None)
        _update(job_id, status=job.status, result=job.result, error=job.error,
                finished_at=job.finished_at)
        _trim()
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
    """Clear queued work and the jobs table. Tests only — refuses the live store."""
    global _speak, _started
    stop()
    with _lock:
        _work.clear()
    if store.path() == store._DEFAULT_PATH:
        raise RuntimeError("refusing to wipe the live store; set NORA_STORE_PATH")
    with store.transaction() as conn:
        conn.execute("DELETE FROM jobs")
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

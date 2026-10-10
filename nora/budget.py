"""One token budget for every model call on a free tier (Sharp Phase E).

Groq's free tier allows each model 8k tokens a minute and 200k a day, and the
live NORA, her background jobs, the scheduler and the nightly evals all spend
the same key. Each used to find the limit by hitting it: a night of eval
trials left the next morning's turns on the fallbacks, and a background
research job could take the minute's last tokens just before the user spoke.

Every call to a model with a limit in config (`budget.limits`) now asks here
first and reports what it spent afterwards. The ledger is a file under a
lock, so the evals process and the core see the same spend.

- **Live** turns never wait. A candidate whose window is already full is
  skipped like a rate-limited one, without the round trip that would learn
  the same thing from a 429.
- **Background** work (jobs, schedules, scout trials, evals, the doctor)
  leaves `budget.live_reserve` untouched, and waits for the minute window to
  free up, never for the day's.

Work is background inside `with budget.background():`; the default is live.
Calls to a model with no configured limit (NVIDIA, Ollama) pass straight
through and are not recorded.
"""
from __future__ import annotations

import contextlib
import contextvars
import fcntl
import json
import logging
import os
import time
from pathlib import Path
from typing import Iterator

from nora.config import get_config

logger = logging.getLogger("nora.budget")

_ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = Path(os.environ.get("NORA_BUDGET_PATH", _ROOT / "nora_budget.json"))

MINUTE = 60.0
DAY = 86400.0

_priority: contextvars.ContextVar[str] = contextvars.ContextVar("nora_budget_priority", default="live")


class OverBudget(RuntimeError):
    """The call would go over the model's free-tier window (or, for
    background work, into what is kept for live turns). Not a health problem:
    the candidate is skipped this once, not cooled down."""


@contextlib.contextmanager
def background() -> Iterator[None]:
    """Mark the calls made inside as background work."""
    token = _priority.set("background")
    try:
        yield
    finally:
        _priority.reset(token)


def is_background() -> bool:
    return _priority.get() == "background"


def run_in_background(fn, *args, **kwargs):
    """Call `fn` as background work (for thread targets and job callables)."""
    with background():
        return fn(*args, **kwargs)


# ── Configuration ────────────────────────────────────────────────────────────

def _cfg() -> dict:
    return get_config().get("budget", {}) or {}


def key(base_url: str | None, model: str) -> str:
    """Ledger key: host and model, which is what a free tier limits."""
    host = (base_url or "").split("://", 1)[-1].split("/", 1)[0]
    return f"{host}/{model}"


def limits(k: str) -> dict | None:
    """{"tpm": .., "tpd": ..} for a ledger key, or None if it has no limit."""
    lim = (_cfg().get("limits") or {}).get(k)
    return lim if isinstance(lim, dict) else None


def _reserve() -> tuple[int, int]:
    r = _cfg().get("live_reserve") or {}
    return int(r.get("tpm", 0) or 0), int(r.get("tpd", 0) or 0)


def estimate(messages: list[dict], max_tokens: int = 0) -> int:
    """A rough token count for a call: ~4 characters a token for the prompt,
    plus what the reply is likely to cost (capped by max_tokens)."""
    chars = sum(len(str(m.get("content") or "")) for m in messages)
    reply = min(int(max_tokens or 0), 300)
    return chars // 4 + reply


# ── The ledger ───────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _locked() -> Iterator[list]:
    """The ledger's events ([ts, key, tokens], newest last), under an
    exclusive lock; changes to the list are written back."""
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER_PATH.with_suffix(".lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            try:
                events = json.loads(LEDGER_PATH.read_text(encoding="utf-8")).get("events", [])
            except (OSError, ValueError, AttributeError):
                events = []
            before = list(events)
            yield events
            cutoff = time.time() - DAY
            events[:] = [e for e in events if e[0] >= cutoff]
            if events != before:
                tmp = LEDGER_PATH.with_suffix(".tmp")
                tmp.write_text(json.dumps({"events": events}), encoding="utf-8")
                os.replace(tmp, LEDGER_PATH)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _used(events: list, k: str, now: float) -> tuple[int, int, float]:
    """(tokens in the last minute, tokens in the last day, when the oldest
    event in the minute window leaves it)."""
    minute = day = 0
    oldest = now
    for ts, ek, tokens in events:
        if ek != k:
            continue
        if ts >= now - DAY:
            day += tokens
        if ts >= now - MINUTE:
            minute += tokens
            oldest = min(oldest, ts)
    return minute, day, oldest + MINUTE


def usage(k: str) -> dict:
    """What a key has spent: {"minute": .., "day": ..}."""
    try:
        with _locked() as events:
            minute, day, _ = _used(events, k, time.time())
    except OSError:
        minute = day = 0
    return {"minute": minute, "day": day}


def admit(base_url: str | None, model: str, tokens: int) -> None:
    """Return when a call of about `tokens` may go ahead; raise OverBudget
    when it may not. Background work waits up to `budget.background_wait_sec`
    for the minute window; live work never waits."""
    k = key(base_url, model)
    lim = limits(k)
    if lim is None:
        return
    tpm, tpd = int(lim.get("tpm", 0) or 0), int(lim.get("tpd", 0) or 0)
    bg = is_background()
    keep_m, keep_d = _reserve() if bg else (0, 0)
    deadline = time.monotonic() + (float(_cfg().get("background_wait_sec", 30)) if bg else 0.0)
    while True:
        try:
            with _locked() as events:
                minute, day, frees_at = _used(events, k, time.time())
        except OSError as exc:              # an unreadable ledger never blocks a turn
            logger.debug("budget ledger unavailable: %s", exc)
            return
        if tpd and day + tokens > tpd - keep_d:
            raise OverBudget(f"{k}: {day:,} of {tpd:,} tokens used today"
                             + (" (the rest is kept for live turns)" if bg else ""))
        if not tpm or minute + tokens <= tpm - keep_m:
            return
        wait = frees_at - time.time()
        if time.monotonic() + wait > deadline:
            raise OverBudget(f"{k}: {minute:,} of {tpm:,} tokens used this minute"
                             + (" (the rest is kept for live turns)" if bg else ""))
        logger.info("budget: background call to %s waits %.0fs for the minute window", k, wait)
        time.sleep(max(0.5, wait))


def record(base_url: str | None, model: str, tokens: int) -> None:
    """Note what a call to a limited model actually spent."""
    k = key(base_url, model)
    if limits(k) is None or tokens <= 0:
        return
    try:
        with _locked() as events:
            events.append([time.time(), k, int(tokens)])
    except OSError as exc:
        logger.debug("budget ledger write failed: %s", exc)


def status() -> str:
    """One line per limited model, for `nora doctor` and the voice."""
    lines = []
    for k, lim in (_cfg().get("limits") or {}).items():
        u = usage(k)
        lines.append(f"{k}: {u['minute']:,}/{int(lim.get('tpm', 0)):,} this minute, "
                     f"{u['day']:,}/{int(lim.get('tpd', 0)):,} today")
    return "; ".join(lines) or "No model budgets configured."

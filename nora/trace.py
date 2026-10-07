"""Per-turn latency trace: where the time in a turn went.

Every turn gets one record in `nora_turn_trace.jsonl`: milliseconds from the
moment the core had the text to each stage it reached, plus every model call
the turn made (role, candidate, seconds, tokens, outcome). The device's own
timings (speech end, recognition, first audio) arrive separately as the
`voice.turn` event in `nora_voice_latency.jsonl`; both carry the turn's `corr`
so a report can join them.

Stages, in the order a turn meets them:

    routed      the turn knows its route: stop, fast, chat or model
    model       the intent or chat model answered (once per turn)
    first_say   the first words were handed to the speaker or device
    done        the turn returned

Only the first time a stage is marked counts. Marking with no trace open (a
proactive line, a job) does nothing, so callers never need to check.
"""
from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger("nora.trace")

TRACE_PATH = Path(__file__).resolve().parent.parent / "nora_turn_trace.jsonl"

_lock = threading.Lock()


@dataclass
class Trace:
    channel: str = "local"
    device: str = ""
    corr: str = ""
    test: bool = False
    ts: float = field(default_factory=time.time)
    route: str = ""
    outcome: str = ""
    marks: dict[str, int] = field(default_factory=dict)
    models: list[dict] = field(default_factory=list)
    _t0: float = field(default_factory=time.monotonic, repr=False)

    def ms(self) -> int:
        return round((time.monotonic() - self._t0) * 1000)

    def mark(self, stage: str) -> None:
        self.marks.setdefault(stage, self.ms())

    def record(self) -> dict:
        d = asdict(self)
        d.pop("_t0", None)
        return d


_current: contextvars.ContextVar[Trace | None] = contextvars.ContextVar("nora_trace", default=None)


def start(*, channel: str = "local", device: str = "", corr: str = "",
          test: bool = False) -> contextvars.Token:
    """Open a trace for the turn running in this context."""
    return _current.set(Trace(channel=channel, device=device, corr=corr, test=test))


def current() -> Trace | None:
    return _current.get()


def mark(stage: str) -> None:
    t = _current.get()
    if t is not None:
        t.mark(stage)


def route(kind: str) -> None:
    """The turn's route is known: stop, wake, fast, chat or model."""
    t = _current.get()
    if t is not None and not t.route:
        t.route = kind
        t.mark("routed")


def note_model(role: str, candidate: str, seconds: float, *, ok: bool = True,
               prompt_tokens: int | None = None, completion_tokens: int | None = None,
               error: str = "") -> None:
    """One model call made on behalf of this turn, successful or not."""
    t = _current.get()
    if t is None:
        return
    t.models.append({"role": role, "candidate": candidate, "ms": round(seconds * 1000),
                     "ok": ok, "prompt_tokens": prompt_tokens,
                     "completion_tokens": completion_tokens, **({"error": error[:120]} if error else {})})
    if ok:
        t.mark("model")


def finish(token: contextvars.Token, outcome: str = "") -> dict | None:
    """Close the trace, append it to the trace log, and return the record."""
    t = _current.get()
    _current.reset(token)
    if t is None:
        return None
    t.outcome = outcome
    t.mark("done")
    rec = t.record()
    logger.info("turn %s route=%s %s%s", t.corr or "-", t.route or "?",
                " ".join(f"{k}={v}ms" for k, v in t.marks.items()),
                "".join(f" [{m['role']}:{m['candidate']} {m['ms']}ms"
                        f"{'' if m['ok'] else ' failed'}]" for m in t.models))
    try:
        with _lock, TRACE_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError as e:
        logger.debug("trace not written: %s", e)
    return rec

"""Adaptive multi-provider LLM router (NORA_ODYSSEUS_PLAN.md Part 2 / §4.7).

Roles (e.g. "reasoning", "chat") map to an ordered list of candidate
endpoints — mixing local (Ollama) and cloud (Groq, NVIDIA NIM, ...) — in
config.yaml under ``llm_router:``. ``complete(role, messages)`` tries each
candidate in order, skipping any still in cooldown from a prior rate limit,
and falls through on failure instead of hard-failing the caller.

Mirrors nora.tool_trust's persisted-JSON-ledger pattern, but the state
tracked is per-endpoint cooldown (rate-limit backoff), not reliability
score — a different axis, same storage shape.

Every attempt is appended to nora_model_router_log.jsonl — the "who saw
this prompt" audit trail NORA_ODYSSEUS_PLAN.md §3 calls for once traffic is
spread across multiple vendors.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from nora import trace
from nora.config import get_config

logger = logging.getLogger("nora.model_router")

_ROOT = Path(__file__).resolve().parent.parent
_STATE_PATH = _ROOT / "nora_model_router_state.json"
_LOG_PATH = _ROOT / "nora_model_router_log.jsonl"
_lock = threading.Lock()

_state: dict[str, dict[str, Any]] = {}
_loaded = False

# The last call that answered, for the dashboard's model pill. In memory only:
# the log file has the history, this is just "who spoke last".
_last_ok: dict[str, Any] | None = None

_DEFAULT_COOLDOWN_SEC = 90.0


class AllCandidatesFailed(RuntimeError):
    """Raised when every candidate for a role failed or was in cooldown."""


# ---------------------------------------------------------------------------
# Cooldown state — persisted, same shape/lock pattern as tool_trust.py
# ---------------------------------------------------------------------------

def _load() -> None:
    global _state, _loaded
    if _loaded:
        return
    if _STATE_PATH.exists():
        try:
            raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            _state = raw if isinstance(raw, dict) else {}
        except Exception as e:
            logger.warning("model_router state load failed: %s", e)
            _state = {}
    _loaded = True


def _save() -> None:
    try:
        _STATE_PATH.write_text(json.dumps(_state, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("model_router state save failed: %s", e)


def _cooldown_until(candidate_name: str) -> float:
    with _lock:
        _load()
        return _state.get(candidate_name, {}).get("cooldown_until", 0.0)


def _mark_exhausted(candidate_name: str, retry_after_sec: float | None) -> None:
    until = time.time() + (retry_after_sec if retry_after_sec and retry_after_sec > 0 else _DEFAULT_COOLDOWN_SEC)
    with _lock:
        _load()
        _state[candidate_name] = {"cooldown_until": until, "reason": "rate_limited"}
        _save()
    logger.warning("model_router: %s cooling down for %.0fs", candidate_name, until - time.time())


def _clear_cooldown(candidate_name: str) -> None:
    with _lock:
        _load()
        entry = _state.get(candidate_name)
        if entry and (entry.get("cooldown_until") or entry.get("strikes")):
            _state[candidate_name] = {"cooldown_until": 0.0}
            _save()


# ---------------------------------------------------------------------------
# Health (Sharp Phase C): a candidate that is gone, down or hanging is skipped
# until it is likely back, the same way a rate-limited one is. Before, only a
# 429 did that, so a withdrawn model (404) or an overloaded one (503, a
# timeout) was tried again on every turn, and every turn paid for it first.
# ---------------------------------------------------------------------------

# Withdrawn (404/410): the scout drops it from the chains after two weekly
# scans; until then, try it again a few times a day, not every turn.
GONE_COOLDOWN_SEC = 6 * 3600
# Key missing or refused: nothing changes until someone fixes .env.
AUTH_COOLDOWN_SEC = 3600
# Down, overloaded or hanging: 30 s, doubling with each failure in a row, up
# to 10 minutes. One success clears it.
BUSY_COOLDOWN_SEC = 30.0
BUSY_COOLDOWN_MAX_SEC = 600.0


def classify_failure(exc: BaseException) -> str:
    """What a failed call says about the candidate's health:

    rate_limited  429: over quota, back after retry-after
    gone          404 / 410: the model was withdrawn or renamed
    auth          401 / 403, or no key: needs a person
    busy          5xx, timeout, connection refused, over capacity
    reply         it answered, but the answer was unusable: not a health
                  problem, the next turn may well be fine
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    text = str(exc).lower()
    if status == 429 or "rate limit" in text:
        return "rate_limited"
    if status in (404, 410):
        return "gone"
    if status in (401, 403) or "not set" in text or "api key" in text or "api_key" in text:
        return "auth"
    if (isinstance(status, int) and status >= 500) or "timed out" in text or "timeout" in text \
            or "connection" in text or "capacity" in text or "overloaded" in text:
        return "busy"
    return "reply"


def note_failure(candidate_name: str, exc: BaseException) -> str:
    """Record what a failure says about the candidate, and cool it down for as
    long as that kind of failure usually lasts. Returns the kind."""
    kind = classify_failure(exc)
    if kind == "rate_limited":
        retry_after = None
        try:
            retry_after = float(exc.response.headers.get("retry-after", ""))   # type: ignore[attr-defined]
        except (TypeError, ValueError, AttributeError):
            pass
        _mark_exhausted(candidate_name, retry_after)
        return kind
    if kind == "reply":
        return kind
    with _lock:
        _load()
        prev = _state.get(candidate_name, {})
        strikes = int(prev.get("strikes", 0)) + 1 if prev.get("reason") == kind else 1
        if kind == "gone":
            secs = GONE_COOLDOWN_SEC
        elif kind == "auth":
            secs = AUTH_COOLDOWN_SEC
        else:
            secs = min(BUSY_COOLDOWN_SEC * 2 ** (strikes - 1), BUSY_COOLDOWN_MAX_SEC)
        _state[candidate_name] = {"cooldown_until": time.time() + secs, "reason": kind,
                                  "strikes": strikes}
        _save()
    logger.warning("model_router: %s is %s — skipped for %.0fs", candidate_name, kind, secs)
    return kind


def cooldown_reason(candidate_name: str) -> str:
    with _lock:
        _load()
        return _state.get(candidate_name, {}).get("reason", "")


def attempt_timeout(total_sec: float, is_last: bool) -> float:
    """How long one candidate gets. The turn's whole budget used to go to the
    first candidate (45 s), so a hanging one left no time for the fallbacks
    behind it. Each now gets `llm_router.attempt_timeout_sec`, and the last
    one the whole budget."""
    per = float(get_config().get("llm_router", {}).get("attempt_timeout_sec", 0) or 0)
    if is_last or per <= 0:
        return total_sec
    return min(per, total_sec)


def order(candidates: list[dict]) -> list[tuple[dict, bool]]:
    """(candidate, skip) in the order to try them. A candidate cooling down is
    skipped, unless every one is: then the one back soonest is tried anyway
    (never a withdrawn one), rather than failing the turn flat."""
    now = time.time()
    names = [c.get("name", c.get("model", "?")) for c in candidates]
    until = {n: _cooldown_until(n) for n in names}
    if any(until[n] <= now for n in names):
        return [(c, until[n] > now) for c, n in zip(candidates, names)]
    fallback = [n for n in names if cooldown_reason(n) != "gone"]
    pick = min(fallback, key=lambda n: until[n]) if fallback else None
    return [(c, n != pick) for c, n in zip(candidates, names)]


def status() -> str:
    """Natural-language summary for the voice interface."""
    with _lock:
        _load()
        items = dict(_state)
    now = time.time()
    cooling = {k: v for k, v in items.items() if v.get("cooldown_until", 0) > now}
    if not cooling:
        return "All configured model endpoints are available."
    said = {"rate_limited": "rate-limited", "gone": "withdrawn", "auth": "refusing its key",
            "busy": "down or overloaded"}
    lines = [f"{name}: {said.get(v.get('reason', ''), 'cooling down')}, "
             f"skipped for {int(v['cooldown_until'] - now)}s" for name, v in cooling.items()]
    return ". ".join(lines)


def last_used() -> dict[str, Any] | None:
    """The most recent successful call: role, provider, model, ts, latency_ms."""
    return dict(_last_ok) if _last_ok else None


def _log_attempt(role: str, candidate: dict, outcome: str, latency_ms: float, error: str = "") -> None:
    entry = {
        "ts": time.time(),
        "role": role,
        "candidate": candidate.get("name"),
        "provider": candidate.get("provider"),
        "model": candidate.get("model"),
        "outcome": outcome,
        "latency_ms": round(latency_ms, 1),
    }
    if error:
        entry["error"] = error[:300]
    if outcome == "ok":
        global _last_ok
        _last_ok = entry
    try:
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.debug("model_router log write failed: %s", e)


# ---------------------------------------------------------------------------
# Candidate call implementations
# ---------------------------------------------------------------------------

def _call_openai_compatible(
    candidate: dict,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    timeout_sec: float,
    extras_out: dict | None = None,
    on_text: "Callable[[str], None] | None" = None,
) -> str:
    from openai import OpenAI

    api_key = os.environ.get(candidate.get("api_key_env", ""), "")
    if candidate.get("api_key_env") and not api_key:
        raise EnvironmentError(f"{candidate['api_key_env']} not set")

    # max_retries=0: the SDK's own 429 retries sleep out retry-after before
    # the caller ever sees the 429, so the router's cooldown never engaged.
    client = OpenAI(api_key=api_key, base_url=candidate["base_url"], timeout=timeout_sec,
                    max_retries=0)
    # Per-candidate provider extensions, e.g. Groq's reasoning_format:"hidden"
    # for the gpt-oss models — without it their analysis channel is appended to
    # `content` and gets spoken aloud ("We need to consider the conversation...").
    extra_body = candidate.get("extra_body") or None
    if on_text is not None:
        # Streamed: each piece goes to `on_text` as it arrives, so the first
        # sentence can be spoken while the rest is still being written.
        parts: list[str] = []
        stream = client.chat.completions.create(
            model=candidate["model"],
            max_tokens=max_tokens,
            temperature=temperature,
            messages=messages,
            extra_body=extra_body,
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            piece = chunk.choices[0].delta.content or ""
            if piece:
                parts.append(piece)
                on_text(piece)
        return "".join(parts)
    resp = client.chat.completions.create(
        model=candidate["model"],
        max_tokens=max_tokens,
        temperature=temperature,
        messages=messages,
        extra_body=extra_body,
    )
    message = resp.choices[0].message
    # Provider-specific fields the OpenAI schema has no slot for land in
    # model_extra — Groq's agentic models return the searches they ran there
    # (`executed_tools`), which is how web_search gets its source URLs.
    if extras_out is not None:
        extras_out.update(getattr(message, "model_extra", None) or {})
    return message.content or ""


def _call_ollama(
    candidate: dict,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    timeout_sec: float,
    extras_out: dict | None = None,
) -> str:
    import requests

    base_url = candidate.get("base_url", "http://localhost:11434")
    resp = requests.post(
        f"{base_url}/api/chat",
        json={
            "model": candidate["model"],
            "messages": messages,
            "stream": False,
            # Hybrid-reasoning models (qwen3, ...) burn the token budget on
            # chain-of-thought before ever emitting `content` — a modest
            # max_tokens leaves content empty. These router roles want a
            # direct answer; genuine deep reasoning already routes to a
            # dedicated model via the "reasoning" role instead.
            "think": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        },
        timeout=timeout_sec,
    )
    resp.raise_for_status()
    return resp.json().get("message", {}).get("content", "")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _candidates_for(role: str) -> list[dict]:
    from nora import scout
    roles_cfg = get_config().get("llm_router", {}).get("roles", {})
    return scout.live(roles_cfg.get(role, []))      # minus models the scout saw withdrawn


def complete(
    role: str,
    messages: list[dict],
    max_tokens: int = 512,
    temperature: float = 0.2,
    timeout_sec: float | None = None,
    extras_out: dict | None = None,
    validate: "Callable[[str], bool] | None" = None,
    on_text: "Callable[[str], None] | None" = None,
) -> tuple[str, str]:
    """Try each configured candidate for `role` in order; return (text, candidate_name).

    Skips candidates still in rate-limit cooldown. Falls through on any
    error (auth, connection, timeout, rate limit) to the next candidate.
    Raises AllCandidatesFailed only if every candidate failed or was skipped.

    `extras_out`, if given, is filled with the winning candidate's
    non-standard response fields (see _call_openai_compatible).

    `validate`, if given, is called with each candidate's reply and must
    return True for it to be accepted. A rejected reply is treated exactly
    like a failed call — the candidate is logged and the chain falls through
    to the next one. This is how a reply that came back *syntactically* fine
    but semantically unusable (a model dumping its chain-of-thought into
    `content`) gets a second chance on a different model instead of being
    handed to the speaker.

    `on_text`, if given, streams the reply: it is called with each piece as
    it arrives (Ollama candidates call it once, with the whole reply). Once a
    candidate has streamed anything, its failure is final: the next one would
    start the answer again over what was already said.
    """
    candidates = _candidates_for(role)
    if not candidates:
        raise AllCandidatesFailed(f"No candidates configured for role {role!r}")

    timeout_sec = timeout_sec or float(get_config().get("timeouts", {}).get("llm_sec", 45))
    now = time.time()
    errors: list[str] = []
    plan = order(candidates)
    last_tried = max((i for i, (_, skip) in enumerate(plan) if not skip), default=-1)

    for i, (candidate, skip) in enumerate(plan):
        name = candidate.get("name", candidate.get("model", "?"))
        if skip:
            remaining = _cooldown_until(name) - now
            logger.info("model_router: skipping %s (%s, %.0fs left)", name,
                        cooldown_reason(name) or "cooling down", remaining)
            errors.append(f"{name}: cooling down ({int(remaining)}s left)")
            continue

        start = time.monotonic()
        streamed = [False]
        try:
            if extras_out is not None:
                extras_out.clear()
            budget = attempt_timeout(timeout_sec, i == last_tried)

            def _piece(piece: str) -> None:
                streamed[0] = True
                on_text(piece)

            if candidate.get("provider") == "ollama":
                text = _call_ollama(candidate, messages, max_tokens, temperature, budget, extras_out)
                if on_text is not None and text:
                    _piece(text)
            else:
                stream_kw = {"on_text": _piece} if on_text is not None else {}
                text = _call_openai_compatible(candidate, messages, max_tokens, temperature, budget,
                                               extras_out, **stream_kw)
            latency_ms = (time.monotonic() - start) * 1000
            if not text.strip():
                raise RuntimeError("empty response")
            if validate is not None and not validate(text):
                raise RuntimeError("reply rejected by validator")
            _clear_cooldown(name)
            _log_attempt(role, candidate, "ok", latency_ms)
            trace.note_model(role, name, latency_ms / 1000)
            logger.info("model_router: role=%s -> %s (%.0fms)", role, name, latency_ms)
            return text, name
        except Exception as e:
            latency_ms = (time.monotonic() - start) * 1000
            kind = note_failure(name, e)
            outcome = "rate_limited" if kind == "rate_limited" else "error"
            _log_attempt(role, candidate, outcome, latency_ms, error=str(e))
            trace.note_model(role, name, latency_ms / 1000, ok=False, error=outcome)
            logger.warning("model_router: %s failed for role=%s (%s) — %s", name, role, kind, e)
            errors.append(f"{name}: {e}")
            if on_text is not None and streamed[0]:
                break
            continue

    raise AllCandidatesFailed(f"role={role}: " + " | ".join(errors))

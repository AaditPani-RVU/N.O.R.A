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
from typing import Any

from nora.config import get_config

logger = logging.getLogger("nora.model_router")

_ROOT = Path(__file__).resolve().parent.parent
_STATE_PATH = _ROOT / "nora_model_router_state.json"
_LOG_PATH = _ROOT / "nora_model_router_log.jsonl"
_lock = threading.Lock()

_state: dict[str, dict[str, Any]] = {}
_loaded = False

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
        _state[candidate_name] = {"cooldown_until": until}
        _save()
    logger.warning("model_router: %s cooling down for %.0fs", candidate_name, until - time.time())


def _clear_cooldown(candidate_name: str) -> None:
    with _lock:
        _load()
        if candidate_name in _state and _state[candidate_name].get("cooldown_until"):
            _state[candidate_name] = {"cooldown_until": 0.0}
            _save()


def status() -> str:
    """Natural-language summary for the voice interface."""
    with _lock:
        _load()
        items = dict(_state)
    now = time.time()
    cooling = {k: v["cooldown_until"] - now for k, v in items.items() if v.get("cooldown_until", 0) > now}
    if not cooling:
        return "All configured model endpoints are available."
    lines = [f"{name}: cooling down for {int(secs)}s" for name, secs in cooling.items()]
    return ". ".join(lines)


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
    try:
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.debug("model_router log write failed: %s", e)


# ---------------------------------------------------------------------------
# Candidate call implementations
# ---------------------------------------------------------------------------

def _call_openai_compatible(candidate: dict, messages: list[dict], max_tokens: int, temperature: float, timeout_sec: float) -> str:
    from openai import OpenAI, APIStatusError, APIConnectionError, APITimeoutError

    api_key = os.environ.get(candidate.get("api_key_env", ""), "")
    if candidate.get("api_key_env") and not api_key:
        raise EnvironmentError(f"{candidate['api_key_env']} not set")

    client = OpenAI(api_key=api_key, base_url=candidate["base_url"], timeout=timeout_sec)
    # Per-candidate provider extensions, e.g. Groq's reasoning_format:"hidden"
    # for the gpt-oss models — without it their analysis channel is appended to
    # `content` and gets spoken aloud ("We need to consider the conversation...").
    extra_body = candidate.get("extra_body") or None
    try:
        resp = client.chat.completions.create(
            model=candidate["model"],
            max_tokens=max_tokens,
            temperature=temperature,
            messages=messages,
            extra_body=extra_body,
        )
    except APIStatusError as e:
        if e.status_code == 429:
            retry_after = None
            try:
                retry_after = float(e.response.headers.get("retry-after", ""))
            except (TypeError, ValueError):
                pass
            _mark_exhausted(candidate["name"], retry_after)
        raise
    except (APIConnectionError, APITimeoutError):
        raise
    return resp.choices[0].message.content or ""


def _call_ollama(candidate: dict, messages: list[dict], max_tokens: int, temperature: float, timeout_sec: float) -> str:
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
    roles_cfg = get_config().get("llm_router", {}).get("roles", {})
    return roles_cfg.get(role, [])


def complete(
    role: str,
    messages: list[dict],
    max_tokens: int = 512,
    temperature: float = 0.2,
    timeout_sec: float | None = None,
) -> tuple[str, str]:
    """Try each configured candidate for `role` in order; return (text, candidate_name).

    Skips candidates still in rate-limit cooldown. Falls through on any
    error (auth, connection, timeout, rate limit) to the next candidate.
    Raises AllCandidatesFailed only if every candidate failed or was skipped.
    """
    candidates = _candidates_for(role)
    if not candidates:
        raise AllCandidatesFailed(f"No candidates configured for role {role!r}")

    timeout_sec = timeout_sec or float(get_config().get("timeouts", {}).get("llm_sec", 45))
    now = time.time()
    errors: list[str] = []

    for candidate in candidates:
        name = candidate.get("name", candidate.get("model", "?"))
        remaining = _cooldown_until(name) - now
        if remaining > 0:
            logger.info("model_router: skipping %s (cooling down %.0fs)", name, remaining)
            errors.append(f"{name}: cooling down ({int(remaining)}s left)")
            continue

        start = time.monotonic()
        try:
            if candidate.get("provider") == "ollama":
                text = _call_ollama(candidate, messages, max_tokens, temperature, timeout_sec)
            else:
                text = _call_openai_compatible(candidate, messages, max_tokens, temperature, timeout_sec)
            latency_ms = (time.monotonic() - start) * 1000
            if not text.strip():
                raise RuntimeError("empty response")
            _clear_cooldown(name)
            _log_attempt(role, candidate, "ok", latency_ms)
            logger.info("model_router: role=%s -> %s (%.0fms)", role, name, latency_ms)
            return text, name
        except Exception as e:
            latency_ms = (time.monotonic() - start) * 1000
            outcome = "rate_limited" if "429" in str(e) or "rate" in str(e).lower() else "error"
            _log_attempt(role, candidate, outcome, latency_ms, error=str(e))
            logger.warning("model_router: %s failed for role=%s — %s", name, role, e)
            errors.append(f"{name}: {e}")
            continue

    raise AllCandidatesFailed(f"role={role}: " + " | ".join(errors))

"""Route an utterance the way a live turn would, without acting on it.

`route_offline` walks the same deterministic gates as
`pipeline._handle_turn`, in the same order: stop, wake phrase, fast path,
conversation. It imports those gates rather than copying them, so a change to
any of them shows up here. What reaches the end needs the intent model, and
`route_model` sends it to one named candidate.

Nothing is executed and nothing is spoken: the fast path and the model only
decide, and the decision is what is scored.
"""
from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Iterator

from nora.evals.cases import Route

EVAL_DEVICE = "d_eval_phone"
_UNAVAILABLE = ("APITimeoutError", "APIConnectionError", "InternalServerError",
                "ServiceUnavailable", "NotFoundError", "timed out", "503", "502", "404")
_MANIFEST = Path(__file__).with_name("phone_manifest.json")


def phone_manifest() -> list[dict]:
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))["capabilities"]


@contextlib.contextmanager
def phone_connected() -> Iterator[None]:
    """Register the phone's capabilities as the hub does when it connects, so
    phone-only fast-path rules fire and the intent prompt lists phone actions."""
    from nora import command_engine
    from nora.hub import protocol
    from nora.hub.server import _TIER_RISK

    async def _never(**_params):        # pragma: no cover - nothing executes in an eval
        raise RuntimeError("evals never execute a capability")

    for raw in phone_manifest():
        entry = protocol.check_manifest_entry(raw)
        desc = entry["description"]
        if entry["tier"] >= protocol.TIER_CONFIRM:
            desc = f"{desc} (asks on the device first)".strip()
        command_engine.register_device_capability(
            entry["name"], _never, device=EVAL_DEVICE, sig=protocol.signature_hint(entry),
            description=desc, risk=_TIER_RISK.get(entry["tier"], "high"), tier=entry["tier"])
    try:
        yield
    finally:
        command_engine.unregister_device(EVAL_DEVICE)


@contextlib.contextmanager
def on_phone(via: str = "phone") -> Iterator[None]:
    """Bind the turn's channel: said to the phone, or to the laptop's mic."""
    from nora import channel
    device = EVAL_DEVICE if via == "phone" else channel.LOCAL_DEVICE
    token = channel.bind(channel.Channel(device_id=device, kind="voice", speak=lambda *a, **k: None))
    try:
        yield
    finally:
        channel.unbind(token)


def _steps(intent) -> list[tuple[str, dict]]:
    return [(s.action, dict(s.parameters or {})) for s in intent.steps]


def route_offline(text: str, *, prior: bool = False) -> tuple[Route | None, str]:
    """(route, text) for what the deterministic gates decide, or (None, text)
    when the turn would go to the intent model. The text returned is what the
    model would be sent ("stop, what day is it" → "what day is it")."""
    from nora import conversation, dialogue, fast_path, pipeline

    question = pipeline._question_after_stop(text)
    if question is not None:
        text = question
    else:
        low = text.lower().strip().rstrip(".,!?")
        if any(p == low or low.startswith(p + " ") for p in pipeline.STOP_PHRASES):
            return Route("stop"), text
    if pipeline.is_wake_phrase(text.lower().strip().rstrip(".,!?")):
        return Route("wake"), text

    fast = fast_path.resolve(text)
    if fast is not None and (fast.steps or fast.response):
        return Route("fast", steps=_steps(fast), reply=fast.response or "", detail=fast.intent), text

    act = dialogue.classify(text, has_prior_turn=prior)
    if conversation.should_handle(act):
        return Route("chat", detail=act.value), text
    return None, text


def route_model(text: str, candidate: dict) -> tuple[Route, dict]:
    """Send one utterance to one intent candidate. Returns the route and the
    model call's record (ms, tokens), or an error route if the call failed."""
    from nora import intent_parser, trace

    token = trace.start(channel="eval", test=True)
    t0 = time.monotonic()
    try:
        intent = intent_parser._parse_via_groq(
            text, intent_parser.candidate_cfg(candidate), {"session_turns": []}, None,
            net_attempts=1)
        route = (Route("model", steps=_steps(intent), reply=intent.response or "",
                       detail=intent.intent) if not intent.error
                 else Route("model", reply=intent.error, detail="error-reply"))
    except Exception as e:
        route = Route("error", detail=f"{type(e).__name__}: {str(e)[:120]}")
    t = trace.current()
    calls = list(t.models) if t else []
    trace._current.reset(token)          # an eval call is not a turn: no trace line
    ok = [c for c in calls if c["ok"]]
    return route, {
        "ms": round((time.monotonic() - t0) * 1000),
        "prompt_tokens": sum(c["prompt_tokens"] or 0 for c in calls),
        "completion_tokens": sum(c["completion_tokens"] or 0 for c in calls),
        "json_retry": len(ok) > 1,      # the first reply was not valid JSON
        "rate_limited": any(c.get("error") == "rate-limited" for c in calls),
        # Timed out or unreachable: says nothing about the model's answers.
        "unavailable": route.kind == "error" and any(
            n in route.detail for n in _UNAVAILABLE),
    }

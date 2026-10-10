"""An eval case, what it expects, and whether a route meets it.

One JSON object per line in evals/cases.jsonl:

    {"id": "battery-1", "text": "What's my phone battery looking like?",
     "expect": {"action": "device.status"}, "tags": ["phone"]}

`expect` is one of:

    {"action": NAME, "params": {...}}   the first step runs NAME; each listed
                                        param must appear in the actual value
                                        (case and punctuation ignored)
    {"steps": [{"action": ..., "params": {...}}, ...]}
                                        a multi-step plan, in order
    {"chat": true}                      answered by talking, no action
    {"stop": true}                      stops playback and ends the turn
    {"any": [expect, ...]}              any one of these is right

Optional fields: `via` ("phone", the default, or "laptop": which device heard
it, since "play X" means the phone only when said to the phone), `tags`
(families, for the report), `prior` (true when the utterance follows an
earlier turn, so follow-ups classify as they did live),
`known` (the Sharp phase expected to fix a known miss, e.g. "D"; a known miss
is reported but does not count as a regression) and `note`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Case:
    id: str
    text: str
    expect: dict
    via: str = "phone"
    tags: list[str] = field(default_factory=list)
    prior: bool = False
    known: str = ""
    note: str = ""


@dataclass
class Route:
    """Where a turn went and what it decided."""
    kind: str                                   # stop | wake | fast | chat | model | error
    steps: list[tuple[str, dict]] = field(default_factory=list)
    reply: str = ""
    detail: str = ""                            # the conversation act, model name, error

    @property
    def is_chat(self) -> bool:
        return self.kind in ("chat", "wake") or (self.kind in ("fast", "model") and not self.steps)

    def describe(self) -> str:
        if self.kind == "error":
            return f"error: {self.detail}"
        if self.steps:
            return " → ".join(f"{a}({_short(p)})" if p else a for a, p in self.steps)
        return self.kind + (f":{self.detail}" if self.detail else "")


def _short(params: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in list(params.items())[:3])


def load(path: Path) -> list[Case]:
    cases: list[Case] = []
    seen: set[str] = set()
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        raw = json.loads(line)
        case = Case(id=raw.get("id") or f"line-{n}", text=raw["text"], expect=raw["expect"],
                    via=raw.get("via", "phone"), tags=raw.get("tags", []),
                    prior=bool(raw.get("prior")),
                    known=raw.get("known", ""), note=raw.get("note", ""))
        if case.via not in ("phone", "laptop"):
            raise ValueError(f"{path}:{n}: via must be phone or laptop, not {case.via!r}")
        if case.id in seen:
            raise ValueError(f"{path}:{n}: duplicate case id {case.id!r}")
        seen.add(case.id)
        cases.append(case)
    return cases


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _canonical(action: str, params: dict) -> dict:
    """Params as the command will read them, where it accepts more than one
    spelling: remind_me takes `duration: "one minute"` as well as minutes."""
    if action != "remind_me":
        return params
    from nora.commands.notifications import duration_minutes
    spoken = params.get("duration") or params.get("delay_minutes")
    minutes = duration_minutes(spoken) if spoken else None
    return {**params, "delay_minutes": minutes} if minutes is not None else params


def _params_match(want: dict, got: dict) -> bool:
    for key, value in (want or {}).items():
        if key not in got:
            return False
        if isinstance(value, (bool, int, float)) and not isinstance(value, str):
            if got[key] != value:
                return False
        elif _norm(value) not in _norm(got[key]):
            return False
    return True


def matches(expect: dict, route: Route) -> bool:
    if "any" in expect:
        return any(matches(e, route) for e in expect["any"])
    if expect.get("stop"):
        return route.kind == "stop"
    if expect.get("chat"):
        return route.is_chat
    if "action" in expect:
        return (bool(route.steps) and route.steps[0][0] == expect["action"]
                and _params_match(expect.get("params", {}), _canonical(*route.steps[0])))
    if "steps" in expect:
        want = expect["steps"]
        return (len(route.steps) == len(want)
                and all(a == w["action"] and _params_match(w.get("params", {}), _canonical(a, p))
                        for (a, p), w in zip(route.steps, want)))
    raise ValueError(f"unknown expectation: {expect}")


def expected_actions(expect: dict) -> list[set[str]]:
    """The action sets that would satisfy `expect`, one per alternative; empty
    for a chat or stop expectation. Tool retrieval is scored against these:
    a case is covered when every action of one alternative was picked."""
    if "any" in expect:
        return [a for e in expect["any"] for a in expected_actions(e)]
    if "action" in expect:
        return [{expect["action"]}]
    if "steps" in expect:
        return [{s["action"] for s in expect["steps"]}]
    return []

"""Sharp Phase A: the eval harness, and the offline eval as a gate.

The labelled set (evals/cases.jsonl) is private and gitignored, so these
tests check the harness on synthetic cases, and run the real set only when
it is present: on the core, a change that breaks a case the gates used to
get right fails here like any other test.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from nora.evals import CASES_PATH, harvest
from nora.evals.cases import Route, load, matches
from nora.evals.harness import on_phone, phone_connected, phone_manifest, route_offline

ROOT = Path(__file__).resolve().parents[1]


def test_manifest_matches_the_app():
    """The fixture lists exactly what the Android app declares."""
    kotlin = (ROOT / "android/app/src/main/java/com/aaditpani/nora/phone").glob("*.kt")
    declared = {m for f in kotlin for m in re.findall(r'override val name = "([\w.]+)"', f.read_text())}
    assert {c["name"] for c in phone_manifest()} == declared


@pytest.mark.parametrize("expect, route, ok", [
    ({"action": "get_time"}, Route("fast", steps=[("get_time", {})]), True),
    ({"action": "get_time"}, Route("chat"), False),
    ({"chat": True}, Route("chat", detail="question"), True),
    ({"chat": True}, Route("model", reply="Hi!"), True),          # a model chat reply
    ({"chat": True}, Route("wake"), True),
    ({"stop": True}, Route("stop"), True),
    ({"stop": True}, Route("chat"), False),
    ({"action": "play_on_phone", "params": {"query": "pink floyd"}},
     Route("fast", steps=[("play_on_phone", {"query": "Pink Floyd!", "kind": "any"})]), True),
    ({"action": "play_on_phone", "params": {"query": "deftones"}},
     Route("fast", steps=[("play_on_phone", {"query": "risk by deaf tools"})]), False),
    ({"action": "phone.volume", "params": {"level": 30}},
     Route("fast", steps=[("phone.volume", {"action": "set", "level": 30})]), True),
    ({"action": "remind_me", "params": {"delay_minutes": 1}},
     Route("fast", steps=[("remind_me", {"message": "stretch", "duration": "one minute"})]), True),
    ({"action": "remind_me", "params": {"delay_minutes": 5}},
     Route("fast", steps=[("remind_me", {"message": "stretch", "duration": "one minute"})]), False),
    ({"any": [{"action": "agenda"}, {"action": "check_calendar"}]},
     Route("model", steps=[("check_calendar", {})]), True),
    ({"steps": [{"action": "open_app"}, {"action": "play_music"}]},
     Route("model", steps=[("open_app", {}), ("play_music", {})]), True),
    ({"steps": [{"action": "open_app"}, {"action": "play_music"}]},
     Route("model", steps=[("open_app", {})]), False),
])
def test_matches(expect, route, ok):
    assert matches(expect, route) is ok


def test_load_rejects_duplicates_and_bad_via(tmp_path):
    p = tmp_path / "cases.jsonl"
    p.write_text('{"id": "a", "text": "x", "expect": {"chat": true}}\n'
                 '{"id": "a", "text": "y", "expect": {"chat": true}}\n')
    with pytest.raises(ValueError, match="duplicate"):
        load(p)
    p.write_text('{"text": "x", "expect": {"chat": true}, "via": "watch"}\n')
    with pytest.raises(ValueError, match="via"):
        load(p)


@pytest.fixture(scope="module")
def commands():
    from nora import command_engine
    command_engine.discover_commands()


@pytest.fixture
def phone(commands):
    with phone_connected():
        yield


@pytest.mark.parametrize("text, kind, action", [
    ("What's my phone battery?", "fast", "device.status"),
    ("Set my phone volume to 30.", "fast", "phone.volume"),
    ("stop", "stop", None),
    ("thanks", "fast", None),                              # the fast path's own small talk
    ("What do you think about beagles", "chat", None),
    ("wake up, Nora", "wake", None),
    ("Set an alarm to wake up at 6", None, None),         # the model's to decide, not "awake"
])
def test_route_offline(phone, text, kind, action):
    with on_phone("phone"):
        route, _ = route_offline(text)
    assert (route.kind if route else None) == kind
    if action:
        assert route.steps[0][0] == action


def test_question_after_stop_is_what_the_model_sees(phone):
    with on_phone("phone"):
        route, sent = route_offline("Stop. Who won the match")
    assert route is None and sent == "Who won the match"


def test_phone_rules_step_aside_without_the_phone(commands):
    with on_phone("laptop"):
        route, _ = route_offline("What's my phone battery?")
    assert route is None or route.steps[:1] != [("device.status", {})]


def test_harvest_pairs_each_utterance_with_its_route():
    lines = [
        "[11:43:42] INFO nora.pipeline: Heard: what is on my calendar today",
        "[11:43:46] INFO nora.intent_parser: Sending to openai/gpt-oss-120b",
        '[11:43:48] DEBUG nora.intent_parser: Groq raw response: {"intent":"x","steps":[{"action":"check_calendar"}]}',
        "[11:44:24] INFO nora.pipeline: Heard: thanks",
        "[11:44:24] INFO nora.pipeline: Conversational act: small_talk",
        "[11:45:00] INFO nora.pipeline: Heard: What time is it",
        "[11:45:00] INFO nora.pipeline: Fast-path hit: get current time",
        "[11:46:00] INFO nora.pipeline: Heard: crazy",
        '[11:46:01] DEBUG nora.intent_parser: Groq raw response: {"intent":"chat","steps":[]}',
    ]
    found: dict = {}
    harvest._scan(lines, found)
    observed = {t: list(v["observed"]) for t, v in found.items()}
    assert observed == {"what is on my calendar today": ["model:check_calendar"],
                        "thanks": ["chat:small_talk"],
                        "What time is it": ["fast:get current time"],
                        "crazy": ["model:chat"]}


@pytest.mark.skipif(not CASES_PATH.exists(), reason="no private eval set on this machine")
def test_offline_eval_has_no_regressions():
    """Every case the deterministic gates decide is decided right, apart from
    misses marked `known` for a later phase."""
    from nora.evals.__main__ import run
    lines: list[str] = []
    report = run(load(CASES_PATH), out=lines.append)
    assert report["regressions"] == 0, "\n".join(lines)


def test_trace_records_stages_and_model_calls(tmp_path, monkeypatch):
    from nora import trace
    monkeypatch.setattr(trace, "TRACE_PATH", tmp_path / "t.jsonl")
    token = trace.start(channel="device", device="d_x", corr="c1")
    trace.route("model")
    trace.route("chat")                                   # the first route sticks
    trace.note_model("intent", "groq_x", 1.234, prompt_tokens=5000, completion_tokens=40)
    trace.mark("first_say")
    trace.mark("first_say")
    rec = trace.finish(token, "action")
    assert trace.current() is None
    assert rec["route"] == "model" and rec["outcome"] == "action" and rec["corr"] == "c1"
    assert list(rec["marks"]) == ["routed", "model", "first_say", "done"]
    assert rec["models"] == [{"role": "intent", "candidate": "groq_x", "ms": 1234, "ok": True,
                              "prompt_tokens": 5000, "completion_tokens": 40}]
    assert json.loads((tmp_path / "t.jsonl").read_text()) == rec


def test_trace_calls_without_a_turn_do_nothing():
    from nora import trace
    trace.mark("first_say")
    trace.route("fast")
    trace.note_model("chat", "x", 1.0)
    assert trace.current() is None

"""nora.scout: new models noticed, tried, judged and proposed; withdrawn ones dropped.

No network: provider listings and eval runs are stubbed.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest import mock

import pytest

from nora import scout

ROOT = Path(__file__).resolve().parents[1]
CFG = {"llm_router": {"roles": {
    "intent": [{"name": "groq_big", "provider": "groq", "base_url": "https://g/v1",
                "api_key_env": "G_KEY", "model": "openai/gpt-oss-120b"},
               {"name": "nv_super", "provider": "nvidia", "base_url": "https://n/v1",
                "api_key_env": "N_KEY", "model": "nvidia/super"}],
}}}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(scout, "get_config", lambda: CFG)
    monkeypatch.setattr(scout, "STATE_PATH", tmp_path / "scout.json")
    scout._wd_cache = (None, set())


def lister(listing: dict[str, set[str]]):
    return lambda p: listing.get(p["name"])


def test_first_scan_is_a_baseline_and_later_ones_find_new_models(cfg):
    state = {"providers": {}, "trials": {}}
    news = scout.scan(state, lister=lister({"groq": {"openai/gpt-oss-120b"}, "nvidia": {"nvidia/super"}}))
    assert news["new"] == [] and state["trials"] == {}

    news = scout.scan(state, lister=lister({
        "groq": {"openai/gpt-oss-120b", "qwen/qwen4-32b", "whisper-large-v4"},
        "nvidia": {"nvidia/super", "nvidia/llama-nemotron-embed-1b", "meta/llama-5-3b"}}))
    assert sorted(m for _, m in news["new"]) == [
        "meta/llama-5-3b", "nvidia/llama-nemotron-embed-1b", "qwen/qwen4-32b", "whisper-large-v4"]
    # Only a chat model of a useful size is worth a trial.
    assert [t["model"] for t in state["trials"].values()] == ["qwen/qwen4-32b"]


def test_a_model_is_withdrawn_after_two_missing_scans_and_the_router_skips_it(cfg):
    state = {"providers": {}, "trials": {}}
    both = lister({"groq": {"openai/gpt-oss-120b"}, "nvidia": {"nvidia/super"}})
    gone = lister({"groq": set(), "nvidia": {"nvidia/super"}})
    scout.scan(state, lister=both)
    assert scout.scan(state, lister=gone)["withdrawn"] == []        # one bad listing: no
    assert scout.scan(state, lister=gone)["withdrawn"] == [("groq", "openai/gpt-oss-120b")]
    scout._save(state)

    live = scout.live(CFG["llm_router"]["roles"]["intent"])
    assert [c["name"] for c in live] == ["nv_super"]
    # Never empties a role: the last resort is still tried.
    assert scout.live(CFG["llm_router"]["roles"]["intent"][:1])[0]["name"] == "groq_big"

    back = scout.scan(state, lister=both)
    assert back["back"] == [("groq", "openai/gpt-oss-120b")]
    scout._save(state)
    assert len(scout.live(CFG["llm_router"]["roles"]["intent"])) == 2


def test_an_unreachable_provider_decides_nothing(cfg):
    state = {"providers": {}, "trials": {}}
    scout.scan(state, lister=lister({"groq": {"openai/gpt-oss-120b"}, "nvidia": {"nvidia/super"}}))
    for _ in range(3):
        news = scout.scan(state, lister=lister({"groq": None, "nvidia": {"nvidia/super"}}))
        assert news["withdrawn"] == []


def _res(oks: list[bool], ms: int = 2000, errors: int = 0) -> dict:
    return {f"c{i}": {"ok": ok, "ms": ms, "prompt_tokens": 5000, "error": i < errors}
            for i, ok in enumerate(oks)}


@pytest.mark.parametrize("cand, inc, verdict", [
    (_res([True] * 30), _res([True] * 30), "no better"),
    (_res([True] * 40), _res([True] * 30 + [False] * 10), "better"),
    (_res([True] * 40, ms=1000), _res([True] * 40, ms=9000), "better"),         # as right, far faster
    (_res([True] * 30 + [False] * 10), _res([True] * 40), "worse"),
    (_res([True] * 20), _res([True] * 20), "waiting"),                          # too few cases
    (_res([True] * 38 + [False] * 2, ms=20000), _res([True] * 34 + [False] * 6), "no better"),  # too slow
])
def test_judge(cand, inc, verdict):
    assert scout.judge(cand, inc)["verdict"] == verdict


def test_more_right_overall_but_wrong_where_the_incumbent_was_right_is_not_better():
    inc = _res([True] * 34 + [False] * 6)
    cand = _res([False] * 4 + [True] * 36)          # +2 overall, but 4 regressions
    v = scout.judge(cand, inc)
    assert v["verdict"] != "better" and len(v["regressions"]) == 4


def test_trials_accumulate_across_nights_and_propose_a_winner(cfg, monkeypatch):
    state = {"providers": {}, "trials": {"k": {
        "provider": "nvidia", "provider_key": "https://n/v1|N_KEY", "model": "qwen/qwen4-32b",
        "role": "intent", "status": "pending", "queued": 1.0, "results": {}}}}
    inc = _res([True] * 30 + [False] * 10)
    monkeypatch.setattr(scout, "scored", lambda name: inc)
    monkeypatch.setattr(scout, "_keep_report", lambda *a: None)
    nights = iter([dict(list(_res([True] * 40).items())[:25]), dict(list(_res([True] * 40).items())[25:])])
    seen_skip = []

    def fake_run(cases, *, candidate, limit, skip, out):
        seen_skip.append(set(skip))
        by_case = next(nights)
        return {"cases": 60, "decided_offline": 20, "by_case": by_case}

    with mock.patch("nora.evals.__main__.run", fake_run), \
            mock.patch("nora.evals.cases.load", return_value=[]):
        assert scout.run_trials(state, out=lambda *_: None) == []
        assert state["trials"]["k"]["status"] == "running"
        proposed = scout.run_trials(state, out=lambda *_: None)
    assert seen_skip[1] == set(f"c{i}" for i in range(25))       # night two skips night one
    assert [t["model"] for t in proposed] == ["qwen/qwen4-32b"]
    assert state["trials"]["k"]["status"] == "proposed"


def test_promote_and_rollback_round_trip_on_the_real_config(cfg, tmp_path, monkeypatch):
    cfg_copy = tmp_path / "config.yaml"
    shutil.copy(ROOT / "config.yaml", cfg_copy)
    monkeypatch.setattr(scout, "CONFIG_PATH", cfg_copy)
    monkeypatch.setattr(scout, "providers", lambda: {
        "https://n/v1|N_KEY": {"name": "nvidia", "base_url": "https://n/v1", "api_key_env": "N_KEY"}})
    state = {"providers": {}, "trials": {"k": {
        "provider": "nvidia", "provider_key": "https://n/v1|N_KEY", "model": "qwen/qwen4-32b",
        "role": "intent", "status": "proposed", "queued": 1.0, "results": {}, "vs": "groq_big",
        "verdict": {"overlap": 40, "cand_correct": 40, "inc_correct": 30}}}}
    scout._save(state)
    before = cfg_copy.read_text()

    print(scout.promote("qwen/qwen4-32b"))
    import yaml
    intent = yaml.safe_load(cfg_copy.read_text())["llm_router"]["roles"]["intent"]
    assert intent[0]["model"] == "qwen/qwen4-32b" and intent[0]["base_url"] == "https://n/v1"
    assert len(intent) == len(yaml.safe_load(before)["llm_router"]["roles"]["intent"]) + 1
    assert json.loads(scout.STATE_PATH.read_text())["trials"]["k"]["status"] == "promoted"

    scout.rollback("qwen/qwen4-32b")
    assert cfg_copy.read_text() == before


def test_promote_refuses_a_model_that_was_not_proposed(cfg):
    scout._save({"providers": {}, "trials": {"k": {
        "provider": "nvidia", "provider_key": "https://n/v1|N_KEY", "model": "bad/model",
        "role": "intent", "status": "rejected", "queued": 1.0, "results": {}}}})
    with pytest.raises(SystemExit, match="rejected"):
        scout.promote("bad/model")


def test_a_listed_model_that_never_answers_is_dropped_after_three_nights(cfg, monkeypatch):
    state = {"providers": {}, "trials": {"k": {
        "provider": "nvidia", "provider_key": "https://n/v1|N_KEY", "model": "ghost/model",
        "role": "intent", "status": "pending", "queued": 1.0, "results": {}}}}
    monkeypatch.setattr(scout, "scored", lambda name: {})
    monkeypatch.setattr(scout, "_keep_report", lambda *a: None)
    timed_out = {f"c{i}": {"ok": False, "error": True, "unavailable": True} for i in range(3)}

    def fake_run(cases, **kw):
        return {"cases": 60, "decided_offline": 20, "by_case": timed_out}

    with mock.patch("nora.evals.__main__.run", fake_run), \
            mock.patch("nora.evals.cases.load", return_value=[]):
        for night in range(3):
            scout.run_trials(state, out=lambda *_: None)
            assert state["trials"]["k"]["results"] == {}          # never scored as wrong
    assert state["trials"]["k"]["status"] == "unavailable"


def test_a_run_stops_calling_after_three_unanswered_calls(monkeypatch):
    from nora.evals import __main__ as ev
    from nora.evals.cases import Case, Route
    cases = [Case(id=f"c{i}", text=f"plan my week {i}", expect={"chat": True}) for i in range(6)]
    calls = []

    def fake_model(text, candidate):
        calls.append(text)
        return Route("error", detail="APITimeoutError: Request timed out."), {
            "ms": 50000, "prompt_tokens": 0, "completion_tokens": 0, "unavailable": True}

    monkeypatch.setattr(ev, "route_offline", lambda text, prior=False: (None, text))
    monkeypatch.setattr(ev, "route_model", fake_model)
    report = ev.run(cases, candidate={"name": "x", "provider": "nvidia"}, space=0,
                    out=lambda *_: None)
    assert len(calls) == 3 and report["pending"] == 3
    assert all(r["unavailable"] for r in report["by_case"].values())

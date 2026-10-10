"""Sharp Phase E: one token budget, and an honest answer when no model can.

The exit criteria, as tests: a simulated outage leaves fast-path turns
working and every other turn answered within 2 s with "can't right now";
background work never takes the tokens a live turn needs.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx
from openai import APIConnectionError, APITimeoutError

from nora import budget, intent_parser, model_router, pipeline

_GROQ = "https://api.groq.com/openai/v1"
_NVIDIA = "https://integrate.api.nvidia.com/v1"
_KEY = "api.groq.com/openai/gpt-oss-120b"
_CFG = {
    "budget": {"limits": {_KEY: {"tpm": 8000, "tpd": 200000}},
               "live_reserve": {"tpm": 4000, "tpd": 40000},
               "background_wait_sec": 0},
    "timeouts": {"llm_sec": 45},
    "llm_router": {"attempt_timeout_sec": 8, "probe_timeout_sec": 1.5},
}
_CANDIDATES = [
    {"name": "groq", "provider": "groq", "model": "openai/gpt-oss-120b", "base_url": _GROQ,
     "api_key_env": "K"},
    {"name": "nvidia", "provider": "nvidia", "model": "nemotron", "base_url": _NVIDIA,
     "api_key_env": "K"},
]


class _Isolated(unittest.TestCase):
    """A private ledger, router state and config for each test."""

    cfg = _CFG

    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="nora-budget-"))
        self.ledger = tmp / "budget.json"
        for p in (
            mock.patch.object(budget, "LEDGER_PATH", self.ledger),
            mock.patch.object(budget, "get_config", return_value=self.cfg),
            mock.patch.object(model_router, "get_config", return_value=self.cfg),
            mock.patch.object(intent_parser, "get_config", return_value=self.cfg),
            mock.patch.object(model_router, "_STATE_PATH", tmp / "state.json"),
            mock.patch.object(model_router, "_state", {}),
            mock.patch.object(model_router, "_loaded", True),
            mock.patch.object(model_router, "_log_attempt"),
            mock.patch.dict(os.environ, {"K": "test-key"}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def spend(self, tokens: int, ago: float = 0.0) -> None:
        with budget._locked() as events:
            events.append([time.time() - ago, _KEY, tokens])


class GovernorTest(_Isolated):
    def test_a_live_call_goes_ahead_under_the_limit_and_is_recorded(self) -> None:
        budget.admit(_GROQ, "openai/gpt-oss-120b", 2000)
        budget.record(_GROQ, "openai/gpt-oss-120b", 1900)
        self.assertEqual(budget.usage(_KEY), {"minute": 1900, "day": 1900})

    def test_a_live_call_is_refused_when_the_minute_is_full_and_never_waits(self) -> None:
        self.spend(7000)
        t0 = time.monotonic()
        with self.assertRaises(budget.OverBudget):
            budget.admit(_GROQ, "openai/gpt-oss-120b", 2000)
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_background_work_leaves_the_live_reserve_alone(self) -> None:
        self.spend(3000)                       # live could still spend 5,000
        budget.admit(_GROQ, "openai/gpt-oss-120b", 2000)
        with budget.background(), self.assertRaises(budget.OverBudget) as raised:
            budget.admit(_GROQ, "openai/gpt-oss-120b", 2000)
        self.assertIn("kept for live turns", str(raised.exception))

    def test_background_work_waits_for_the_minute_window(self) -> None:
        cfg = {**_CFG, "budget": {**_CFG["budget"], "background_wait_sec": 5}}
        self.spend(3000, ago=budget.MINUTE - 1.0)   # leaves the window in ~1 s
        t0 = time.monotonic()
        with mock.patch.object(budget, "get_config", return_value=cfg), budget.background():
            budget.admit(_GROQ, "openai/gpt-oss-120b", 2000)
        self.assertGreater(time.monotonic() - t0, 0.4)

    def test_the_days_limit_is_never_waited_for(self) -> None:
        cfg = {**_CFG, "budget": {**_CFG["budget"], "background_wait_sec": 30}}
        self.spend(165000, ago=3600)
        t0 = time.monotonic()
        with mock.patch.object(budget, "get_config", return_value=cfg), budget.background(), \
                self.assertRaises(budget.OverBudget):
            budget.admit(_GROQ, "openai/gpt-oss-120b", 2000)
        self.assertLess(time.monotonic() - t0, 0.5)
        budget.admit(_GROQ, "openai/gpt-oss-120b", 2000)   # live still may

    def test_a_model_without_a_limit_is_not_metered(self) -> None:
        self.spend(8000)
        budget.admit(_NVIDIA, "nemotron", 100000)
        budget.record(_NVIDIA, "nemotron", 5000)
        self.assertEqual(budget.usage("integrate.api.nvidia.com/nemotron")["day"], 0)

    def test_spend_older_than_a_day_is_dropped(self) -> None:
        self.spend(5000, ago=budget.DAY + 60)
        budget.record(_GROQ, "openai/gpt-oss-120b", 10)
        self.assertEqual(budget.usage(_KEY)["day"], 10)

    def test_the_ledger_is_shared_between_processes(self) -> None:
        # The nightly evals run in a process of their own, on the same key.
        code = ("import sys; from unittest import mock; from pathlib import Path; "
                "from nora import budget; "
                f"budget.LEDGER_PATH = Path({str(self.ledger)!r}); "
                f"budget.get_config = lambda: {_CFG!r}; "
                "budget.record('https://api.groq.com/openai/v1', 'openai/gpt-oss-120b', 1234)")
        subprocess.run([sys.executable, "-c", code], check=True,
                       cwd=Path(__file__).resolve().parent.parent)
        self.assertEqual(budget.usage(_KEY)["minute"], 1234)

    def test_background_is_per_thread_of_work(self) -> None:
        seen = {}
        with budget.background():
            t = threading.Thread(target=lambda: seen.update(other=budget.is_background()))
            t.start()
            t.join()
            seen["here"] = budget.is_background()
        self.assertEqual(seen, {"other": False, "here": True})
        self.assertFalse(budget.is_background())


class RouterBudgetTest(_Isolated):
    def test_a_live_turn_skips_a_full_model_without_asking_it_or_cooling_it(self) -> None:
        self.spend(7900)
        asked = []

        class Completions:
            def create(self, **kw):
                asked.append(kw["model"])
                msg = mock.Mock(content="hello", model_extra={})
                return mock.Mock(choices=[mock.Mock(message=msg)], usage=mock.Mock(total_tokens=50))

        client = mock.Mock()
        client.chat.completions = Completions()
        with mock.patch.object(model_router, "_candidates_for", return_value=_CANDIDATES), \
                mock.patch("openai.OpenAI", return_value=client):
            text, name = model_router.complete("chat", [{"role": "user", "content": "hi"}])
        self.assertEqual((text, name), ("hello", "nvidia"))
        self.assertEqual(asked, ["nemotron"])
        self.assertLessEqual(model_router._cooldown_until("groq"), time.time())

    def test_what_a_call_spent_is_recorded(self) -> None:
        class Completions:
            def create(self, **kw):
                msg = mock.Mock(content="hello", model_extra={})
                return mock.Mock(choices=[mock.Mock(message=msg)], usage=mock.Mock(total_tokens=777))

        client = mock.Mock()
        client.chat.completions = Completions()
        with mock.patch.object(model_router, "_candidates_for", return_value=_CANDIDATES[:1]), \
                mock.patch("openai.OpenAI", return_value=client):
            model_router.complete("chat", [{"role": "user", "content": "hi"}])
        self.assertEqual(budget.usage(_KEY)["minute"], 777)

    def test_background_jobs_never_cause_a_live_rate_limit(self) -> None:
        # Background keeps going until it reaches the reserve...
        done = 0
        with budget.background():
            for _ in range(10):
                try:
                    budget.admit(_GROQ, "openai/gpt-oss-120b", 1500)
                except budget.OverBudget:
                    break
                budget.record(_GROQ, "openai/gpt-oss-120b", 1500)
                done += 1
        self.assertEqual(done, 2)              # 3,000 of the 4,000 it may use
        # ...and a live turn still fits under Groq's own limit.
        budget.admit(_GROQ, "openai/gpt-oss-120b", 2500)

    def test_the_intent_parser_counts_against_the_same_budget(self) -> None:
        self.spend(7900)
        with mock.patch.object(intent_parser, "_build_system_prompt", return_value="sys"), \
                mock.patch.object(intent_parser, "_get_groq_client") as client, \
                self.assertRaises(budget.OverBudget):
            intent_parser._parse_via_groq("open firefox", {"model": "openai/gpt-oss-120b",
                                                           "api_base": _GROQ, "api_key_env": "K"},
                                          net_attempts=1)
        client.return_value.chat.completions.create.assert_not_called()


# ── Degraded mode: a simulated outage through the real turn path ─────────────

class _DeadProvider:
    """An OpenAI-compatible client for a provider that is down: refuses the
    connection, or hangs until the client's timeout."""

    def __init__(self, base_url, timeout_sec, hang: bool):
        self.timeout_sec = timeout_sec
        self.hang = hang
        self.chat = mock.Mock()
        self.chat.completions.create = self.create

    def create(self, **kw):
        req = httpx.Request("POST", "https://x.example/v1/chat/completions")
        if self.hang:
            time.sleep(self.timeout_sec)
            raise APITimeoutError(request=req)
        raise APIConnectionError(request=req)


class DegradedModeTest(_Isolated):
    cfg = {**_CFG, "llm_router": {"attempt_timeout_sec": 3, "probe_timeout_sec": 1.5}}

    def setUp(self) -> None:
        super().setUp()
        from tests.test_pipeline_smoke import scripted, Turn
        from nora import command_engine
        command_engine.discover_commands()
        self.scripted, self.Turn = scripted, Turn
        for p in (
            mock.patch.object(intent_parser, "_intent_candidates", return_value=_CANDIDATES),
            mock.patch.object(intent_parser, "_build_system_prompt", return_value="sys"),
            mock.patch.object(intent_parser, "_groq_clients", {}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def turn(self, text: str, hang: bool = False):
        turn = self.Turn()
        deps = turn.deps()
        deps.parse_intent = intent_parser.parse_intent      # the real one
        deps.llm_timeout = 45
        make = lambda api_key, timeout_sec, base_url=None, key_env=None: \
            _DeadProvider(base_url, timeout_sec, hang)
        t0 = time.monotonic()
        with self.scripted(turn), mock.patch.object(intent_parser, "_get_groq_client", side_effect=make):
            outcome = asyncio.run(pipeline.handle_turn(text, deps))
        return outcome, turn, time.monotonic() - t0

    def test_with_every_model_down_a_turn_says_so_within_two_seconds(self) -> None:
        outcome, turn, took = self.turn("book me a table somewhere quiet tonight")
        self.assertEqual(outcome.stage, "models_down")
        self.assertIn("can't reach my models", turn.spoken[-1])
        self.assertIn("quick things still work", turn.spoken[-1])
        self.assertLess(took, 2.0)

    def test_once_every_model_is_known_down_even_a_hanging_one_costs_under_two_seconds(self) -> None:
        self.turn("book me a table somewhere quiet tonight")          # learns both are down
        outcome, turn, took = self.turn("and order a cab for eight", hang=True)
        self.assertEqual(outcome.stage, "models_down")
        self.assertLess(took, 2.5)       # one 1.5 s look, not the 45 s timeout

    def test_the_fast_path_still_works_in_an_outage(self) -> None:
        self.turn("book me a table somewhere quiet tonight")
        outcome, turn, took = self.turn("play music")
        self.assertEqual(outcome.kind, "executed")
        self.assertEqual(outcome.actions, ["play_music"])

    def test_a_rate_limited_outage_is_named_as_one(self) -> None:
        model_router._mark_exhausted("groq", 60)
        model_router._mark_exhausted("nvidia", 60)
        self.assertIn("allowance", model_router.outage_line())

    def test_chat_does_not_ask_a_second_time_after_every_model_failed(self) -> None:
        from nora import conversation
        with mock.patch.object(model_router, "complete",
                               side_effect=model_router.AllCandidatesFailed("all down")), \
                mock.patch.object(intent_parser, "_call_llm") as again:
            self.assertEqual(conversation._generate("sys", [], conversation.Act.QUESTION), "")
        again.assert_not_called()


if __name__ == "__main__":
    unittest.main()

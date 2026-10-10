"""Turn latency on Groq's free tier: rate limits, the prompt's size, reminders.

The first live week of typed chat showed single model calls taking 36-44 s.
They were rate limits (Groq's free tier allows 8,000 tokens a minute, and the
intent prompt was about 6,400) being sat out silently by the OpenAI SDK's own
retries, while the fallback models behind Groq went unused. Covered here:

  * a 429 fails over to the next candidate at once, and that candidate is
    skipped while its cooldown lasts;
  * the intent prompt stays under budget, and carries no examples the fast
    path answers before the model is ever asked;
  * "remind me in one minute to …" — the phrasing that set a 5-minute
    reminder — is fast-pathed, and durations are read, not dropped.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx
from openai import RateLimitError

from nora import fast_path, intent_parser, model_router, scheduler
from nora.commands import notifications
from nora.schemas import IntentResponse

_CANDIDATES = [
    {"name": "fast", "model": "m1", "base_url": "https://a.example/v1", "api_key_env": "K"},
    {"name": "backup", "model": "m2", "base_url": "https://b.example/v1", "api_key_env": "K"},
]


def _rate_limited(retry_after: str = "30") -> RateLimitError:
    req = httpx.Request("POST", "https://a.example/v1/chat/completions")
    resp = httpx.Response(429, headers={"retry-after": retry_after}, request=req)
    return RateLimitError("rate limited", response=resp, body=None)


class RateLimitFailoverTest(unittest.TestCase):
    def setUp(self) -> None:
        # The router keeps its cooldowns in a file in the repo root; never the live one.
        tmp = Path(tempfile.mkdtemp(prefix="nora-router-"))
        patches = [
            mock.patch.object(intent_parser, "_intent_candidates", return_value=_CANDIDATES),
            mock.patch.object(model_router, "_STATE_PATH", tmp / "state.json"),
            mock.patch.object(model_router, "_state", {}),
            mock.patch.object(model_router, "_loaded", True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.calls: list[str] = []

    def _script(self, fast_fails: bool):
        def fake(text, cfg, memory_ctx=None, screen_ctx=None, net_attempts=3, timeout_sec=None):
            self.calls.append(cfg["model"])
            if cfg["model"] == "m1" and fast_fails:
                raise _rate_limited("30")
            return IntentResponse(intent="ok", steps=[])
        return mock.patch.object(intent_parser, "_parse_via_groq", side_effect=fake)

    def test_a_rate_limit_fails_over_at_once_and_is_remembered(self) -> None:
        with self._script(fast_fails=True):
            intent_parser._parse_via_router("hi")
            self.assertEqual(self.calls, ["m1", "m2"])
            self.assertGreater(model_router._cooldown_until("fast"), time.time() + 20)
            # The next turn doesn't spend a round trip on the limited one.
            intent_parser._parse_via_router("hi again")
            self.assertEqual(self.calls, ["m1", "m2", "m2"])

    def test_the_cooldown_clears_on_success(self) -> None:
        model_router._mark_exhausted("fast", 0.01)
        time.sleep(0.02)
        with self._script(fast_fails=False):
            intent_parser._parse_via_router("hi")
        self.assertEqual(self.calls, ["m1"])
        self.assertLessEqual(model_router._cooldown_until("fast"), time.time())

    def test_when_all_are_cooling_the_one_back_soonest_is_tried(self) -> None:
        model_router._mark_exhausted("fast", 120)
        model_router._mark_exhausted("backup", 60)
        with self._script(fast_fails=False):
            intent_parser._parse_via_router("hi")
        self.assertEqual(self.calls, ["m2"])

    def test_the_sdk_does_not_retry_by_itself(self) -> None:
        intent_parser._groq_clients.clear()
        client = intent_parser._get_groq_client("k", 5.0, base_url="https://a.example/v1", key_env="K")
        self.assertEqual(client.max_retries, 0)


def _status_error(code: int):
    from openai import APIStatusError
    req = httpx.Request("POST", "https://a.example/v1/chat/completions")
    return APIStatusError(f"Error code: {code}", response=httpx.Response(code, request=req), body=None)


class CandidateHealthTest(unittest.TestCase):
    """Sharp Phase C: a candidate that is gone, down or hanging is skipped
    for a while, the way a rate-limited one already was."""

    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="nora-router-"))
        for p in (mock.patch.object(intent_parser, "_intent_candidates", return_value=_CANDIDATES),
                  mock.patch.object(model_router, "_STATE_PATH", tmp / "state.json"),
                  mock.patch.object(model_router, "_state", {}),
                  mock.patch.object(model_router, "_loaded", True)):
            p.start()
            self.addCleanup(p.stop)

    def test_failures_are_told_apart(self) -> None:
        from openai import APIConnectionError, APITimeoutError
        req = httpx.Request("POST", "https://a.example/v1")
        kinds = {
            "rate_limited": _rate_limited(),
            "gone": _status_error(404),
            "auth": EnvironmentError("NVIDIA_API_KEY not set"),
            "busy": _status_error(503),
        }
        for want, exc in kinds.items():
            self.assertEqual(model_router.classify_failure(exc), want)
        self.assertEqual(model_router.classify_failure(_status_error(410)), "gone")
        self.assertEqual(model_router.classify_failure(APITimeoutError(request=req)), "busy")
        self.assertEqual(model_router.classify_failure(APIConnectionError(request=req)), "busy")
        # An unusable answer says nothing about the candidate's health.
        self.assertEqual(model_router.classify_failure(ValueError("No valid JSON found")), "reply")
        self.assertEqual(model_router.classify_failure(_status_error(400)), "reply")

    def test_a_withdrawn_model_is_skipped_for_hours(self) -> None:
        model_router.note_failure("fast", _status_error(404))
        self.assertGreater(model_router._cooldown_until("fast"), time.time() + 3 * 3600)

    def test_a_busy_model_backs_off_and_recovers(self) -> None:
        waits = []
        for _ in range(7):
            model_router.note_failure("fast", _status_error(503))
            waits.append(round(model_router._cooldown_until("fast") - time.time()))
        self.assertEqual(waits[:3], [30, 60, 120])
        self.assertEqual(waits[-1], model_router.BUSY_COOLDOWN_MAX_SEC)
        model_router._clear_cooldown("fast")
        model_router.note_failure("fast", _status_error(503))
        self.assertAlmostEqual(model_router._cooldown_until("fast") - time.time(), 30, delta=1)

    def test_a_bad_reply_does_not_cool_a_candidate(self) -> None:
        model_router.note_failure("fast", ValueError("No valid JSON found in response"))
        self.assertLessEqual(model_router._cooldown_until("fast"), time.time())

    def test_a_hanging_candidate_fails_over_and_is_then_skipped(self) -> None:
        from openai import APITimeoutError
        calls, timeouts = [], []

        def fake(text, cfg, memory_ctx=None, screen_ctx=None, net_attempts=3, timeout_sec=None):
            calls.append(cfg["model"])
            timeouts.append(timeout_sec)
            if cfg["model"] == "m1":
                raise APITimeoutError(request=httpx.Request("POST", "https://a.example/v1"))
            return IntentResponse(intent="ok", steps=[])

        cfg = {"timeouts": {"llm_sec": 45}, "llm_router": {"attempt_timeout_sec": 8}}
        with mock.patch.object(intent_parser, "_parse_via_groq", side_effect=fake), \
                mock.patch.object(intent_parser, "get_config", return_value=cfg), \
                mock.patch.object(model_router, "get_config", return_value=cfg):
            intent_parser._parse_via_router("hi")
            intent_parser._parse_via_router("hi again")
        self.assertEqual(calls, ["m1", "m2", "m2"])
        # The first gets its share, not the turn's whole 45 s; the last, all of it.
        self.assertEqual(timeouts, [8, 45, 45])

    def test_the_chat_path_skips_a_gone_model_too(self) -> None:
        calls = []

        def call(candidate, *a, **k):
            calls.append(candidate["name"])
            if candidate["name"] == "fast":
                raise _status_error(404)
            return "hello"

        with mock.patch.object(model_router, "_candidates_for", return_value=_CANDIDATES), \
                mock.patch.object(model_router, "_call_openai_compatible", side_effect=call), \
                mock.patch.object(model_router, "_log_attempt"):
            self.assertEqual(model_router.complete("chat", [])[1], "backup")
            self.assertEqual(model_router.complete("chat", [])[1], "backup")
        self.assertEqual(calls, ["fast", "backup", "backup"])
        self.assertEqual(model_router.cooldown_reason("fast"), "gone")

    def test_a_gone_model_is_not_the_last_resort(self) -> None:
        model_router.note_failure("fast", _status_error(404))
        model_router._mark_exhausted("backup", 600)
        plan = model_router.order(_CANDIDATES)
        self.assertEqual([c["name"] for c, skip in plan if not skip], ["backup"])


class PromptBudgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from nora import command_engine
        logging.disable(logging.CRITICAL)
        command_engine.discover_commands()
        logging.disable(logging.NOTSET)

    def test_the_intent_prompt_stays_under_budget(self) -> None:
        # Sharp Phase C: <= 2k tokens, at ~3.7 characters a token. It was
        # 23,800 characters (~6,400 tokens) once, and ~18,400 with every action
        # listed by name: two turns a minute could never fit in Groq's 8,000
        # tokens a minute. Now only the actions picked for the utterance go in.
        for text in ("how do black holes form", "play something by radiohead",
                     "set a reminder for tomorrow at 6pm to call mom",
                     "connect to my bluetooth headphones", "what's on my screen"):
            with self.subTest(text):
                prompt = intent_parser._build_system_prompt(text=text)
                self.assertLess(len(prompt), 7_400, f"intent prompt grew to {len(prompt)} chars")

    def test_rules_and_examples_follow_the_picked_actions(self) -> None:
        music = intent_parser._build_system_prompt(text="play something by radiohead")
        self.assertIn("MUSIC (Spotify)", music)
        self.assertIn("spotify_play_artist(", music)
        other = intent_parser._build_system_prompt(text="connect to wifi CoffeeShop")
        self.assertNotIn("MUSIC (Spotify)", other)
        self.assertIn("wifi_connect", other)
        self.assertIn('User: "connect to wifi CoffeeShop"', other)
        self.assertNotIn('User: "duck Spotify when I speak"', other)
        for core in ("ask_claude(", "tell_me_about(", "read_screen(", "stop_all("):
            self.assertIn(core, other)

    def test_no_example_is_one_the_fast_path_answers(self) -> None:
        # Those never reach the model; as examples they are pure cost.
        examples = [re.match(r'User: "(.*?)" →', line).group(1)
                    for _, line in intent_parser.EXAMPLES]
        self.assertGreater(len(examples), 15)
        answered = [e for e in examples if fast_path.resolve(e) is not None]
        self.assertEqual(answered, [])

    def test_every_example_and_rule_names_a_real_action(self) -> None:
        from nora import command_engine
        known = set(command_engine.get_available_actions())
        for action, _ in intent_parser.EXAMPLES:
            if action:
                self.assertIn(action, known)
        for actions, _ in intent_parser.RULES:
            self.assertTrue(known.intersection(actions), actions)

    def test_the_template_has_no_mojibake(self) -> None:
        self.assertNotIn("â†", intent_parser.SYSTEM_PROMPT_TEMPLATE)
        for _, line in intent_parser.EXAMPLES:
            self.assertNotIn("â†", line)


class ReminderDurationTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="nora-remind-"))
        env = mock.patch.dict(os.environ, {"NORA_STORE_PATH": str(tmp / "nora_core.db")})
        env.start()
        self.addCleanup(env.stop)
        scheduler.reset_for_tests()
        self.addCleanup(scheduler.reset_for_tests)

    def _in(self) -> list[int]:
        return [round(s.next_run - time.time()) for s in scheduler.listing()]

    def test_the_phrasings_that_set_five_minutes_are_fast_pathed(self) -> None:
        cases = {
            "Set a reminder for one minute to wash the dishes.": ("wash the dishes", "one minute"),
            "remind me in 1 minute to stretch": ("stretch", "1 minute"),
            "Remind me in one minute to drink water.": ("drink water", "one minute"),
            "remind me to call mom in 2 hours": ("call mom", "2 hours"),
            "set a reminder for 90 seconds to check the tea": ("check the tea", "90 seconds"),
        }
        for text, (msg, dur) in cases.items():
            with self.subTest(text):
                r = fast_path.resolve(text)
                self.assertEqual((r.steps[0].action, r.steps[0].parameters),
                                 ("remind_me", {"message": msg, "duration": dur}))
        # A day with no time still goes to the model (and schedule_task).
        self.assertIsNone(fast_path.resolve("remind me to call mom tomorrow"))

    def test_durations_are_read_whichever_parameter_they_come_in(self) -> None:
        self.assertEqual(notifications.remind_me("stretch", duration="one minute"),
                         "I'll remind you about that in 1 minute.")
        self.assertEqual(notifications.remind_me("tea", duration="90 seconds"),
                         "I'll remind you about that in 90 seconds.")
        self.assertEqual(notifications.remind_me("call", delay_minutes="2 hours"),
                         "I'll remind you about that in 2 hours.")
        self.assertEqual(notifications.remind_me("oven", delay_minutes=45),
                         "I'll remind you about that in 45 minutes.")
        got = sorted(self._in())
        for want, seen in zip([60, 90, 2700, 7200], got):
            self.assertAlmostEqual(want, seen, delta=2)

    def test_an_unreadable_duration_is_a_question_not_a_default(self) -> None:
        self.assertIn("couldn't tell when", notifications.remind_me("x", duration="soon"))
        self.assertEqual(scheduler.listing(), [])

    def test_fractional_minutes_are_not_a_clock_time(self) -> None:
        # "in 1.5 minutes" used to reach the clock parser and land at 1:05.
        self.assertAlmostEqual(scheduler.parse_spec("in 1.5 minutes").next_run - time.time(), 90, delta=2)
        notifications.remind_me("x", delay_minutes=0.5)
        self.assertAlmostEqual(self._in()[0], 30, delta=2)


if __name__ == "__main__":
    unittest.main()

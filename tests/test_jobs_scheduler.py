"""Tests for the off-turn work path: nora.jobs, nora.scheduler, deferred answers.

The behaviour under test is the one the user actually reported missing: NORA
says it will get back to you, and then does — unprompted, in speech, minutes
later, on a turn nobody triggered.

That makes delivery the thing worth asserting on. A job that computes the right
answer into a dict nobody speaks is exactly the old bug wearing a queue, so
every test here checks what reached the speak callback, not just what the job
returned.

Stdlib unittest only — run with:
    python -m unittest tests.test_jobs_scheduler -v
"""
from __future__ import annotations

import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

from nora import jobs, scheduler


class SpeakRecorder:
    """Stands in for the focus-gated speak callback."""

    def __init__(self) -> None:
        self.said: list[str] = []

    def __call__(self, text: str, *a, **kw) -> None:
        self.said.append(text)

    def joined(self) -> str:
        return " || ".join(self.said)


class JobQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.speak = SpeakRecorder()
        jobs.reset_for_tests(self.speak)
        # Persistence is the one edge these tests must not exercise: the store
        # is a real file in the repo root.
        self._save = mock.patch.object(jobs, "_save", lambda: None)
        self._save.start()
        self.addCleanup(self._save.stop)
        jobs.start(self.speak)
        self.addCleanup(jobs.stop)

    def test_result_is_spoken_unprompted(self) -> None:
        jobs.submit("your question about gradients", lambda: "They vanish in deep nets.")
        self.assertTrue(jobs.drain(timeout=5), "job never finished")
        self.assertEqual(len(self.speak.said), 1)
        said = self.speak.said[0]
        self.assertIn("They vanish in deep nets.", said)
        # The delivery re-states what it is answering — the user asked minutes
        # ago and has been doing something else since.
        self.assertIn("your question about gradients", said)

    def test_failure_is_spoken_not_swallowed(self) -> None:
        def boom() -> str:
            raise RuntimeError("model unreachable")

        jobs.submit("your question about x", boom)
        self.assertTrue(jobs.drain(timeout=5))
        self.assertIn("couldn't finish", self.speak.joined())
        self.assertIn("model unreachable", self.speak.joined())

    def test_submit_returns_immediately(self) -> None:
        """The turn must not block on the work — that is the whole point."""
        started = time.monotonic()
        jobs.submit("slow thing", lambda: (time.sleep(0.4), "done")[1])
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.15, "submit blocked the calling turn")
        self.assertTrue(jobs.drain(timeout=5))

    def test_pending_then_recent(self) -> None:
        gate = __import__("threading").Event()
        jobs.submit("held job", lambda: (gate.wait(2), "released")[1])
        # Give the worker a moment to pick it up.
        deadline = time.time() + 2
        while time.time() < deadline and not jobs.pending():
            time.sleep(0.01)
        self.assertEqual(len(jobs.pending()), 1)
        self.assertIn("held job", jobs.describe_pending())
        gate.set()
        self.assertTrue(jobs.drain(timeout=5))
        self.assertEqual(len(jobs.pending()), 0)
        self.assertEqual(jobs.recent()[0].result, "released")

    def test_find_recovers_answer_by_topic(self) -> None:
        jobs.submit("your question about spotify playlists", lambda: "Forty-two of them.")
        self.assertTrue(jobs.drain(timeout=5))
        found = jobs.find("what did you find out about spotify")
        self.assertIsNotNone(found)
        self.assertEqual(found.result, "Forty-two of them.")

    def test_cron_result_is_spoken_bare(self) -> None:
        """A scheduled brief is not an answer to a question — no 'Back to' frame."""
        jobs.submit("morning brief", lambda: "Three meetings today.", kind="cron")
        self.assertTrue(jobs.drain(timeout=5))
        self.assertEqual(self.speak.said, ["Three meetings today."])

    def test_deliver_false_stays_silent(self) -> None:
        jobs.submit("quiet job", lambda: "nothing to say", deliver=False)
        self.assertTrue(jobs.drain(timeout=5))
        self.assertEqual(self.speak.said, [])
        self.assertEqual(jobs.recent()[0].result, "nothing to say")


class SpecParsingTest(unittest.TestCase):
    def setUp(self) -> None:
        scheduler.reset_for_tests()

    def test_relative_one_shot(self) -> None:
        s = scheduler.parse_spec("in 20 minutes")
        self.assertIsNotNone(s)
        self.assertFalse(s.recurring)
        self.assertAlmostEqual(s.next_run - time.time(), 1200, delta=5)

    def test_in_an_hour(self) -> None:
        s = scheduler.parse_spec("in an hour")
        self.assertAlmostEqual(s.next_run - time.time(), 3600, delta=5)

    def test_recurring_interval(self) -> None:
        s = scheduler.parse_spec("every 30 minutes")
        self.assertTrue(s.recurring)
        self.assertEqual(s.interval_sec, 1800)

    def test_half_hour(self) -> None:
        s = scheduler.parse_spec("every half hour")
        self.assertTrue(s.recurring)
        self.assertEqual(s.interval_sec, 1800)

    def test_interval_has_a_floor(self) -> None:
        """A voice assistant speaking every 5 seconds is a fault, not a feature."""
        s = scheduler.parse_spec("every 5 seconds")
        self.assertGreaterEqual(s.interval_sec, 30)

    def test_daily_at_time(self) -> None:
        s = scheduler.parse_spec("every day at 7am")
        self.assertTrue(s.recurring)
        self.assertEqual(s.daily_at, [7, 0])
        self.assertEqual(datetime.fromtimestamp(s.next_run).hour, 7)

    def test_bare_evening_hour_is_pm(self) -> None:
        """'remind me at 6' means six in the evening."""
        s = scheduler.parse_spec("at 6")
        self.assertEqual(s.daily_at, [18, 0])

    def test_explicit_am_is_respected(self) -> None:
        s = scheduler.parse_spec("at 6am")
        self.assertEqual(s.daily_at, [6, 0])

    def test_weekday(self) -> None:
        s = scheduler.parse_spec("every Monday at 9am")
        self.assertTrue(s.recurring)
        self.assertEqual(s.weekday, 0)
        self.assertEqual(datetime.fromtimestamp(s.next_run).weekday(), 0)

    def test_next_run_is_always_future(self) -> None:
        for spec in ("at 7am", "at 9pm", "every Monday at 9am", "in 5 minutes",
                     "tomorrow at 8am", "every day at 7:30"):
            with self.subTest(spec=spec):
                s = scheduler.parse_spec(spec)
                self.assertIsNotNone(s, f"failed to parse {spec!r}")
                self.assertGreater(s.next_run, time.time())

    def test_literal_crontab(self) -> None:
        s = scheduler.parse_spec("30 7 * * *")
        self.assertIsNotNone(s)
        self.assertEqual(s.daily_at, [7, 30])
        self.assertTrue(s.recurring)

    def test_unparseable_returns_none(self) -> None:
        self.assertIsNone(scheduler.parse_spec("whenever you feel like it"))


class SchedulerFiringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.speak = SpeakRecorder()
        jobs.reset_for_tests(self.speak)
        scheduler.reset_for_tests()
        for mod in (jobs, scheduler):
            patcher = mock.patch.object(mod, "_save", lambda: None)
            patcher.start()
            self.addCleanup(patcher.stop)
        jobs.start(self.speak)
        self.addCleanup(jobs.stop)
        self.addCleanup(scheduler.stop)

    def test_due_schedule_fires_and_speaks(self) -> None:
        scheduler.set_runner(lambda what: f"ran {what}")
        sched = scheduler.add("in 1 hour", "the morning brief")
        sched.next_run = time.time() - 1          # force it due

        self.assertEqual(scheduler.tick(), 1)
        self.assertTrue(jobs.drain(timeout=5))
        self.assertEqual(self.speak.said, ["ran the morning brief"])

    def test_one_shot_does_not_repeat(self) -> None:
        scheduler.set_runner(lambda what: "fired")
        sched = scheduler.add("in 1 hour", "one time thing")
        sched.next_run = time.time() - 1

        self.assertEqual(scheduler.tick(), 1)
        self.assertTrue(jobs.drain(timeout=5))
        self.assertEqual(scheduler.tick(), 0, "one-shot fired twice")
        self.assertEqual(scheduler.listing(), [])

    def test_recurring_reschedules_into_the_future(self) -> None:
        scheduler.set_runner(lambda what: "fired")
        sched = scheduler.add("every 30 minutes", "check the build")
        sched.next_run = time.time() - 1

        self.assertEqual(scheduler.tick(), 1)
        self.assertTrue(jobs.drain(timeout=5))
        self.assertGreater(sched.next_run, time.time())
        self.assertTrue(sched.enabled)
        self.assertEqual(sched.run_count, 1)

    def test_reminder_payload_is_spoken_not_executed(self) -> None:
        """A reminder is a sentence to say, not a command to run."""
        with mock.patch("nora.commands.notifications._toast"):
            result = scheduler._default_runner("remind: buy milk")
        self.assertIn("buy milk", result)
        self.assertIn("Reminder", result)

    def test_cancel_by_description(self) -> None:
        scheduler.add("every day at 7am", "the morning brief")
        self.assertEqual(len(scheduler.listing()), 1)
        removed = scheduler.remove("morning brief")
        self.assertIsNotNone(removed)
        self.assertEqual(scheduler.listing(), [])


class DeferredAnswerTest(unittest.TestCase):
    """The reported bug, end to end: a promise that now gets kept."""

    def setUp(self) -> None:
        self.speak = SpeakRecorder()
        jobs.reset_for_tests(self.speak)
        patcher = mock.patch.object(jobs, "_save", lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        jobs.start(self.speak)
        self.addCleanup(jobs.stop)

    def test_answer_later_acks_now_and_answers_later(self) -> None:
        from nora.commands import deferred

        with mock.patch.object(deferred, "_answer_with_claude",
                               return_value="Because the learning rate is too high."):
            ack = deferred.answer_later("why is my training loss diverging")

            # The turn ends with an honest handoff, not an answer.
            self.assertNotIn("learning rate", ack)
            # Drained inside the patch: the worker resolves the callable when it
            # runs, so letting the mock expire first calls the real Claude CLI.
            self.assertTrue(jobs.drain(timeout=5))

        # ...and the answer arrives unprompted.
        said = self.speak.joined()
        self.assertIn("Because the learning rate is too high.", said)
        self.assertIn("training loss", said)

    def test_repo_questions_get_the_repo_reading_path(self) -> None:
        from nora.commands import deferred

        with mock.patch.object(deferred, "_answer_with_claude",
                               return_value="ok") as call:
            deferred.answer_later("why does my pipeline crash on startup", read_repo=True)
            self.assertTrue(jobs.drain(timeout=5))
        _q, model, read_repo = call.call_args[0]
        self.assertTrue(read_repo)
        self.assertEqual(model, "opus")

    def test_recall_before_and_after_completion(self) -> None:
        from nora.commands import deferred

        gate = __import__("threading").Event()
        with mock.patch.object(deferred, "_answer_with_claude",
                               side_effect=lambda *a: (gate.wait(2), "Forty-two.")[1]):
            deferred.answer_later("what is the meaning of life")
            deadline = time.time() + 2
            while time.time() < deadline and not jobs.pending():
                time.sleep(0.01)
            self.assertIn("Still working", deferred.recall_answer("meaning of life"))
            gate.set()
            self.assertTrue(jobs.drain(timeout=5))
        self.assertEqual(deferred.recall_answer("meaning of life"), "Forty-two.")

    def test_title_reads_as_a_noun_phrase(self) -> None:
        from nora.commands.deferred import _shorten

        self.assertEqual(
            _shorten("can you explain how gradient descent works"),
            "your question about how gradient descent works",
        )


class PromiseInterceptionTest(unittest.TestCase):
    """The chat path can no longer promise something with nothing behind it."""

    def test_detects_real_promises(self) -> None:
        from nora import conversation as c

        for reply in (
            "Let me look into that and get back to you.",
            "I'll get back to you on this one.",
            "Give me a minute on that.",
            "Let me dig into it.",
        ):
            with self.subTest(reply=reply):
                self.assertTrue(c.promises_follow_up(reply))

    def test_ignores_ordinary_replies(self) -> None:
        from nora import conversation as c

        for reply in (
            "It's about twenty past four.",
            "Spotify's playing now.",
            "That's a fair point, though I'd push back on the second half.",
        ):
            with self.subTest(reply=reply):
                self.assertFalse(c.promises_follow_up(reply))

    def test_promise_becomes_a_real_job(self) -> None:
        from nora import conversation as c
        from nora.dialogue import Act

        with mock.patch("nora.commands.deferred.answer_later") as later:
            out = c._honour_promise(
                "why is my training loss diverging so badly",
                "Let me look into that and get back to you.",
                Act.QUESTION,
            )
        later.assert_called_once()
        self.assertEqual(later.call_args[0][0], "why is my training loss diverging so badly")
        # The spoken reply is unchanged — it was already the right sentence.
        self.assertEqual(out, "Let me look into that and get back to you.")

    def test_repo_questions_route_to_the_repo_path(self) -> None:
        from nora import conversation as c
        from nora.dialogue import Act

        with mock.patch("nora.commands.deferred.answer_later") as later:
            c._honour_promise(
                "why does my pipeline module keep crashing",
                "Let me look into that.",
                Act.QUESTION,
            )
        self.assertTrue(later.call_args[1]["read_repo"])

    def test_short_utterances_are_left_alone(self) -> None:
        """'let me think about that' after a joke stays a figure of speech."""
        from nora import conversation as c
        from nora.dialogue import Act

        with mock.patch("nora.commands.deferred.answer_later") as later:
            c._honour_promise("hm ok", "Let me think about that.", Act.QUESTION)
        later.assert_not_called()


if __name__ == "__main__":
    unittest.main()

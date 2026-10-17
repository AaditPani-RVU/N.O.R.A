"""NORA ringing the phone on her own (nora.calls): retries, quiet hours, the
calendar heads-up, and long answers that ring.

Run with:  python -m pytest tests/test_calls.py
"""
from __future__ import annotations

import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

from nora import calls, jobs


def _cfg(**over) -> dict:
    base = {"enabled": True, "retry_minutes": 5, "calendar_minutes": 10,
            "long_job_minutes": 3, "quiet_hours": "23:00-07:00"}
    return {"calls": {**base, **over}}


class CallTestCase(unittest.TestCase):
    def setUp(self) -> None:
        calls.reset_for_tests()
        self.addCleanup(calls.reset_for_tests)
        self.rings: list[dict] = []
        self.answer = True

        def invoke(params):
            self.rings.append(params)
            return self.answer, ""

        for target, kw in (("nora.calls._invoke", {"side_effect": invoke}),
                           ("nora.calls._phone", {"return_value": "pixel"}),
                           ("nora.calls.get_config", {"return_value": _cfg()}),
                           ("nora.calls.notify_laptop", {})):
            patcher = mock.patch(target, **kw)
            setattr(self, target.rsplit(".", 1)[1].lstrip("_"), patcher.start())
            self.addCleanup(patcher.stop)
        # Timers fire the retry at once rather than in five minutes.
        timer = mock.patch("nora.calls.threading.Timer", side_effect=self._timer)
        timer.start()
        self.addCleanup(timer.stop)

    def _timer(self, delay, fn, args):
        t = mock.Mock()
        t.delay = delay
        t.start.side_effect = lambda: fn(*args)
        t.is_alive.return_value = False
        return t


class RetryTest(CallTestCase):
    def test_a_missed_call_rings_once_more(self) -> None:
        self.assertTrue(calls.ring("Reminder: the laundry"))
        calls.ended("pixel", "missed")
        self.assertEqual([r["reason"] for r in self.rings], ["Reminder: the laundry"] * 2)
        self.notify_laptop.assert_not_called()

    def test_missed_twice_lands_on_the_laptop(self) -> None:
        calls.ring("Reminder: the laundry")
        calls.ended("pixel", "missed")
        with mock.patch("nora.calls.threading.Thread") as thread:
            thread.side_effect = lambda target, args, daemon: mock.Mock(start=lambda: target(*args))
            calls.ended("pixel", "missed")
        self.assertEqual(len(self.rings), 2, "rang a third time")
        title, body = self.notify_laptop.call_args.args
        self.assertIn("Missed call from NORA", title)
        self.assertEqual(body, "Reminder: the laundry")

    def test_declined_is_an_answer(self) -> None:
        calls.ring("Reminder: the laundry")
        calls.ended("pixel", "declined")
        self.assertEqual(len(self.rings), 1)
        self.notify_laptop.assert_not_called()

    def test_answered_ends_it(self) -> None:
        calls.ring("Reminder: the laundry")
        calls.ended("pixel", "answered")
        calls.ended("pixel", "missed")          # stale: nothing outstanding
        self.assertEqual(len(self.rings), 1)

    def test_the_retry_rebuilds_what_she_says(self) -> None:
        fresh = iter(["Briefing at 7:05."])
        calls.ring("Good morning.", say="Briefing at 7:00.", refresh=lambda: next(fresh))
        calls.ended("pixel", "missed")
        self.assertEqual([r.get("say") for r in self.rings], ["Briefing at 7:00.", "Briefing at 7:05."])

    def test_a_long_message_missed_twice_is_sent_to_the_phone(self) -> None:
        calls.ring("Good morning.", say="Good morning. Sunny today.")
        calls.ended("pixel", "missed")
        with mock.patch("nora.calls.threading.Thread") as thread, \
                mock.patch("nora.delivery.deliver") as deliver:
            thread.side_effect = lambda target, args, daemon: mock.Mock(start=lambda: target(*args))
            calls.ended("pixel", "missed")
        deliver.assert_called_once_with("Good morning. Sunny today.", device="pixel", kind="call")

    def test_unrung_calls_leave_nothing_outstanding(self) -> None:
        self.answer = False
        self.assertFalse(calls.ring("Reminder: the laundry"))
        calls.ended("pixel", "missed")
        self.assertEqual(len(self.rings), 1)


class QuietHoursTest(CallTestCase):
    def test_spans_midnight(self) -> None:
        for hour, quiet in ((22, False), (23, True), (2, True), (6, True), (7, False), (12, False)):
            with self.subTest(hour=hour):
                self.assertEqual(calls.quiet_now(datetime(2026, 10, 17, hour, 30)), quiet)

    def test_only_unasked_calls_wait(self) -> None:
        with mock.patch("nora.calls.quiet_now", return_value=True):
            self.assertFalse(calls.ring("Standup starts in 10 minutes.", asked=False))
            self.assertTrue(calls.ring("Reminder: the laundry"))
        self.assertEqual(len(self.rings), 1)


class CalendarTest(CallTestCase):
    def _event(self, at: datetime, summary="Standup", ev_id="e1") -> dict:
        return {"id": ev_id, "summary": summary,
                "start": {"dateTime": at.astimezone().isoformat()}}

    def test_due_events(self) -> None:
        now = datetime(2026, 10, 17, 9, 50)
        events = [self._event(now + timedelta(minutes=10)),
                  self._event(now + timedelta(minutes=11), ev_id="later"),
                  self._event(now - timedelta(minutes=1), ev_id="started"),
                  {"id": "allday", "start": {"date": "2026-10-17"}}]
        due = calls.due_events(events, now, 10)
        self.assertEqual([(ev["id"], away) for _, ev, away in due], [("e1", 10)])

    def test_rings_once_per_event(self) -> None:
        now = datetime.now().replace(second=0, microsecond=0)
        if now.hour == 23 and now.minute >= 50:
            self.skipTest("window crosses midnight")
        ev = self._event(now + timedelta(minutes=7))
        state = {}
        with mock.patch("nora.commands.google_services._TOKEN_FILE") as token, \
                mock.patch("nora.commands.google_services._events_between", return_value=[ev]), \
                mock.patch("nora.calls._load_rung", side_effect=lambda: dict(state)), \
                mock.patch("nora.calls._save_rung", side_effect=state.update), \
                mock.patch("nora.calls.quiet_now", return_value=False):
            token.exists.return_value = True
            self.assertEqual(calls.check_calendar(now), 1)
            self.assertEqual(calls.check_calendar(now + timedelta(minutes=1)), 0)
        [params] = self.rings
        self.assertTrue(params["reason"].startswith("Standup starts in 7 minutes, at"), params)

    def test_unrung_heads_up_is_a_notification(self) -> None:
        now = datetime.now().replace(second=0, microsecond=0)
        if now.hour == 23 and now.minute >= 50:
            self.skipTest("window crosses midnight")
        ev = self._event(now + timedelta(minutes=5))
        with mock.patch("nora.commands.google_services._TOKEN_FILE") as token, \
                mock.patch("nora.commands.google_services._events_between", return_value=[ev]), \
                mock.patch("nora.calls._load_rung", return_value={}), \
                mock.patch("nora.calls._save_rung"), \
                mock.patch("nora.calls.quiet_now", return_value=True), \
                mock.patch("nora.delivery.broadcast") as broadcast:
            token.exists.return_value = True
            self.assertEqual(calls.check_calendar(now), 1)
        self.assertEqual(self.rings, [])
        self.assertIn("Standup starts in 5 minutes", broadcast.call_args.args[0])
        self.notify_laptop.assert_called_once()


class LongJobTest(unittest.TestCase):
    def setUp(self) -> None:
        self.said: list[str] = []
        jobs.reset_for_tests(self.said.append)

    def _job(self, minutes: float, **kw) -> jobs.Job:
        now = time.time()
        return jobs.Job(id="j1", title="your question about gradients", status=jobs.STATUS_DONE,
                        result="They vanish.", started_at=now - minutes * 60, finished_at=now,
                        device="pixel", **kw)

    def test_a_long_answer_rings(self) -> None:
        with mock.patch("nora.calls.ring", return_value=True) as ring, \
                mock.patch("nora.jobs._update"), mock.patch("nora.delivery.deliver") as deliver:
            jobs._deliver(self._job(5))
        reason = ring.call_args.args[0]
        self.assertEqual(reason, "I've finished your question about gradients.")
        self.assertIn("They vanish.", ring.call_args.kwargs["say"])
        self.assertFalse(ring.call_args.kwargs["asked"])
        deliver.assert_not_called()

    def test_a_quick_answer_doesnt(self) -> None:
        with mock.patch("nora.calls.ring") as ring, mock.patch("nora.jobs._update"), \
                mock.patch("nora.delivery.deliver", return_value=True) as deliver:
            jobs._deliver(self._job(1))
        ring.assert_not_called()
        deliver.assert_called_once()

    def test_an_unrung_long_answer_is_delivered(self) -> None:
        with mock.patch("nora.calls.ring", return_value=False), mock.patch("nora.jobs._update"), \
                mock.patch("nora.delivery.deliver", return_value=True) as deliver:
            jobs._deliver(self._job(5))
        deliver.assert_called_once()

    def test_reminders_never_ring_from_here(self) -> None:
        with mock.patch("nora.calls.ring") as ring, mock.patch("nora.jobs._update"), \
                mock.patch("nora.delivery.deliver", return_value=True):
            jobs._deliver(self._job(5, kind="reminder"))
        ring.assert_not_called()


if __name__ == "__main__":
    unittest.main()

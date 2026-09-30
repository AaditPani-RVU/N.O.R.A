"""Phase 5: typed chat from the phone, dated tasks, and what reaches the phone.

The exit criterion (NORA_DISTRIBUTED_PLAN.md §10): "Remember I have to submit
the assignment tomorrow" typed on the phone, then "what do I need to do
today?" asked on the laptop the next day, answers with the assignment. It is
tested end to end here: a fake phone over the real hub, the real pipeline and
fast path (the model is never called), the real store, and the laptop turn
after the calendar moves on a day.

Also covered: the day words (`nora.days`), dated tasks and the agenda, the
fast-path routes, and reminders reaching the phone with their own title.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from nora import channel, days, delivery, fast_path, jobs, scheduler, store, task_ledger
from nora.commands import task_commands

try:
    from tests.test_hub import HubTestCase
    from tests.test_pipeline_smoke import SIDE_EFFECTS
except ImportError:          # run from inside tests/
    from test_hub import HubTestCase
    from test_pipeline_smoke import SIDE_EFFECTS

WED = date(2026, 9, 30)          # a Wednesday


def _fake_date(today: date):
    class FakeDate(date):
        @classmethod
        def today(cls):
            return today
    return FakeDate


class _Store(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="nora-chat-"))
        env = mock.patch.dict(os.environ, {"NORA_STORE_PATH": str(tmp / "nora_core.db")})
        env.start()
        self.addCleanup(env.stop)
        scheduler.reset_for_tests()
        self.addCleanup(scheduler.reset_for_tests)
        cal = mock.patch.object(task_commands, "_calendar_on", return_value=[])
        self.calendar = cal.start()
        self.addCleanup(cal.stop)

    def on(self, day: date):
        """Run the task commands as if today were `day`."""
        return mock.patch.object(task_commands, "date", _fake_date(day))


class DaysTest(unittest.TestCase):
    def test_split_takes_the_day_off_the_end_or_the_start(self) -> None:
        cases = {
            "submit the assignment tomorrow": ("submit the assignment", date(2026, 10, 1)),
            "submit the assignment tomorrow morning": ("submit the assignment", date(2026, 10, 1)),
            "pay rent by the 5th": ("pay rent", date(2026, 10, 5)),
            "call mom on friday": ("call mom", date(2026, 10, 2)),
            "exam on 3rd October": ("exam", date(2026, 10, 3)),
            "exam on October 3": ("exam", date(2026, 10, 3)),
            "report is due tomorrow": ("report", date(2026, 10, 1)),
            "tomorrow, call dad": ("call dad", date(2026, 10, 1)),
            "the day after tomorrow meet raj": ("meet raj", date(2026, 10, 2)),
            "renew passport in 2 weeks": ("renew passport", date(2026, 10, 14)),
            "renew passport by 2026-12-01": ("renew passport", date(2026, 12, 1)),
            # Typed on a phone: the first live run was "Tommorow".
            "submit the assignment Tommorow": ("submit the assignment", date(2026, 10, 1)),
            "submit it tomorow": ("submit it", date(2026, 10, 1)),
            "submit it tmrw": ("submit it", date(2026, 10, 1)),
            "meet raj the day after tommorrow": ("meet raj", date(2026, 10, 2)),
        }
        for text, want in cases.items():
            with self.subTest(text):
                self.assertEqual(days.split(text, WED), want)

    def test_nothing_is_guessed(self) -> None:
        for text in ("buy milk", "buy tomatoes", "call tom", "the cat sat", "wash the sun", "tomorrow", "finish the may report"):
            with self.subTest(text):
                self.assertEqual(days.split(text, WED), (text, None))
        self.assertIsNone(days.parse("someday", WED))
        self.assertIsNone(days.parse("february 30", WED))

    def test_weekdays(self) -> None:
        # Said on a Wednesday: "Wednesday" is next week's, "this Wednesday" is today.
        self.assertEqual(days.parse("wednesday", WED), date(2026, 10, 7))
        self.assertEqual(days.parse("this wednesday", WED), WED)
        self.assertEqual(days.parse("on monday", WED), date(2026, 10, 5))

    def test_day_of_month_rolls_forward(self) -> None:
        self.assertEqual(days.parse("the 30th", WED), WED)
        self.assertEqual(days.parse("the 29th", WED), date(2026, 10, 29))
        self.assertEqual(days.parse("the 31st", WED), date(2026, 10, 31))
        self.assertEqual(days.parse("september 1", WED), date(2027, 9, 1))

    def test_said_back(self) -> None:
        self.assertEqual(days.say(WED, WED), "today")
        self.assertEqual(days.say(date(2026, 10, 1), WED), "tomorrow")
        self.assertEqual(days.say(date(2026, 10, 2), WED), "Friday, 2 October")
        self.assertEqual(days.say(date(2026, 9, 27), WED), "Sunday, 27 September, 3 days ago")
        self.assertEqual(days.say(date(2027, 1, 4), WED), "Monday, 4 January 2027")


class TasksAndAgendaTest(_Store):
    def test_a_dated_task_is_stored_as_a_date_and_said_back(self) -> None:
        with self.on(WED):
            reply = task_commands.add_task("submit the assignment tomorrow")
        self.assertEqual(reply, "Got it: Submit the assignment, due tomorrow.")
        [task] = task_ledger.get_open_tasks()
        self.assertEqual((task["title"], task["due_on"]), ("Submit the assignment", "2026-10-01"))

    def test_saying_it_twice_is_one_task(self) -> None:
        with self.on(WED):
            task_commands.add_task("submit the assignment tomorrow")
            self.assertEqual(task_commands.add_task("Submit the assignment tomorrow"),
                             "That's already on your list, due tomorrow.")
            self.assertEqual(task_commands.add_task("submit the assignment on friday"),
                             "That's already on your list. Moved it to Friday, 2 October.")
            self.assertEqual(task_commands.add_task("submit the assignment"),
                             "That's already on your list, due Friday, 2 October.")
        [task] = task_ledger.get_open_tasks()
        self.assertEqual(task["due_on"], "2026-10-02")

    def test_the_model_can_pass_the_day_separately(self) -> None:
        with self.on(WED):
            self.assertIn("due Monday, 5 October", task_commands.add_task("Pay rent", due="the 5th"))
            # A day that can't be read stays with the task, not dropped.
            self.assertEqual(task_commands.add_task("Clean desk", due="someday"),
                             "Added to your list: Clean desk.")
        desk = task_ledger.find_tasks("desk")[0]
        self.assertIsNone(desk["due_on"])
        self.assertIn("someday", desk["notes"])

    def test_agenda_today_tomorrow_overdue_undated(self) -> None:
        with self.on(WED):
            task_commands.add_task("submit the assignment tomorrow")
            task_commands.add_task("return the library book today")
            task_commands.add_task("water the plants")
        thu = WED + timedelta(days=1)
        with self.on(WED):
            self.assertEqual(task_commands.agenda("tomorrow"),
                             "Due tomorrow: Submit the assignment. No date on: Water the plants.")
        with self.on(thu):
            self.assertEqual(task_commands.agenda("today"),
                             "Due today: Submit the assignment. Overdue: Return the library book "
                             "(was due yesterday). No date on: Water the plants.")
        task_ledger.close_task(task_ledger.find_tasks("assignment")[0]["id"])
        with self.on(thu):
            self.assertNotIn("assignment", task_commands.agenda("today"))

    def test_agenda_includes_calendar_and_reminders(self) -> None:
        self.calendar.return_value = ["Standup at 10:00 AM"]
        with self.on(date.today()):
            with mock.patch.object(scheduler, "listing", return_value=[scheduler.Schedule(
                    id="s1", spec="at 5pm", what="remind: call mom",
                    next_run=_at(date.today(), 17))]):
                said = task_commands.agenda("today")
        self.assertEqual(said, "On your calendar: Standup at 10:00 AM. Reminders: 5 pm: call mom.")

    def test_nothing_due(self) -> None:
        with self.on(WED):
            self.assertEqual(task_commands.agenda(), "Nothing due today.")

    def test_calendar_that_hangs_or_fails_is_left_out(self) -> None:
        self.calendar.stop()
        with mock.patch("nora.commands.google_services._calendar_service",
                        side_effect=RuntimeError("Google isn't set up")):
            self.assertEqual(task_commands._calendar_on(WED), [])
        self.calendar.start()

    def test_list_tasks_shows_due_days_first(self) -> None:
        with self.on(WED):
            task_commands.add_task("water the plants")
            task_commands.add_task("submit the assignment tomorrow")
        with mock.patch("nora.commands.task_commands.date", _fake_date(WED)):
            said = task_commands.list_tasks()
        self.assertTrue(said.startswith("You have 2 open tasks. Submit the assignment (due tomorrow)."), said)


def _at(day: date, hour: int) -> float:
    from datetime import datetime
    return datetime(day.year, day.month, day.day, hour).timestamp()


class FastPathTest(unittest.TestCase):
    def test_remembering_something_to_do_is_a_task(self) -> None:
        cases = {
            "Remember I have to submit the assignment tomorrow": "submit the assignment tomorrow",
            "remember to call mom on friday": "call mom on friday",
            "don't let me forget I need to pay rent by the 5th": "pay rent by the 5th",
            "NORA, remember that I've got to renew my passport": "renew my passport",
            "remember I’ve got to renew it": "renew it",   # a phone keyboard's apostrophe
            "add a task: buy milk": "buy milk",
            "add buy milk to my to-do list": "buy milk",
        }
        for text, title in cases.items():
            with self.subTest(text):
                r = fast_path.resolve(text)
                self.assertEqual((r.steps[0].action, r.steps[0].parameters), ("add_task", {"title": title}))

    def test_what_to_do_is_the_agenda(self) -> None:
        cases = {
            "what do I need to do today?": "today",
            "What do I need to do": "today",
            "what do I have to do tomorrow": "tomorrow",
            "what do I need to do tommorow": "tommorow",
            "what's on my plate today": "today",
            "what's on my agenda": "today",
            "what is due tomorrow": "tomorrow",
            "anything due today": "today",
            "what have I got on friday": "friday",
            "what are my tasks for today": "today",
        }
        for text, day in cases.items():
            with self.subTest(text):
                r = fast_path.resolve(text)
                self.assertEqual((r.steps[0].action, r.steps[0].parameters), ("agenda", {"day": day}))

    def test_left_alone(self) -> None:
        # Facts, advice and places are not tasks.
        self.assertIsNone(fast_path.resolve("remember I have a dog"))
        self.assertIsNone(fast_path.resolve("what should I do"))
        self.assertEqual(fast_path.resolve("remember my home address is 12 Baker Street").steps[0].action,
                         "save_place")


class ReminderKindTest(unittest.TestCase):
    def setUp(self) -> None:
        delivery.reset_for_tests()
        self.addCleanup(delivery.reset_for_tests)
        self.got: list[tuple[str, str, str]] = []
        for dev in ("phone", "tablet"):
            delivery.register(dev, lambda text, kind, dev=dev: self.got.append((dev, text, kind)) or True)

    def test_a_local_reminder_is_said_and_sent_to_every_device(self) -> None:
        spoken: list[str] = []
        job = jobs.Job(id="j1", title="remind: call mom", kind="reminder",
                       status=jobs.STATUS_DONE, result="Reminder, sir: call mom")
        with mock.patch.object(jobs, "_speak", spoken.append), mock.patch.object(jobs, "_update"):
            jobs._deliver(job)
        self.assertEqual(spoken, ["Reminder, sir: call mom"])
        self.assertEqual(sorted(self.got), [("phone", "Reminder, sir: call mom", "reminder"),
                                            ("tablet", "Reminder, sir: call mom", "reminder")])

    def test_it_can_be_switched_off(self) -> None:
        with mock.patch("nora.config.get_config", return_value={"hub": {"reminders_to_devices": False}}):
            self.assertEqual(delivery.broadcast("x"), [])
        self.assertEqual(self.got, [])

    def test_other_local_jobs_stay_local(self) -> None:
        job = jobs.Job(id="j2", title="the tides", kind="answer", status=jobs.STATUS_DONE, result="Six.")
        with mock.patch.object(jobs, "_speak", lambda _t: None), mock.patch.object(jobs, "_update"):
            jobs._deliver(job)
        self.assertEqual(self.got, [])

    def test_a_fired_reminder_is_a_reminder_job(self) -> None:
        submitted = {}
        with mock.patch("nora.jobs.submit", lambda title, work, **kw: submitted.update(kw)), \
                mock.patch.object(scheduler, "_write"):
            scheduler._fire(scheduler.Schedule(id="s", spec="in 5 minutes", what="remind: stretch",
                                               next_run=0))
            self.assertEqual(submitted["kind"], "reminder")
            scheduler._fire(scheduler.Schedule(id="s2", spec="every day at 7", what="daily brief",
                                               next_run=0))
            self.assertEqual(submitted["kind"], "cron")
        self.assertEqual(jobs._phrase_answer(jobs.Job(id="j", title="t", kind="reminder",
                                                      result="Reminder, sir: stretch")),
                         "Reminder, sir: stretch")


class ExitCriterionTest(HubTestCase):
    """Typed on the phone on Wednesday; asked on the laptop on Thursday."""

    async def test_remember_on_phone_then_ask_the_laptop(self) -> None:
        import contextlib

        from nora import pipeline, wiring
        from nora.frustration import FrustrationTracker

        dev = await self.paired(name="Pixel")
        with contextlib.ExitStack() as stack:
            for target, value in SIDE_EFFECTS:
                if target != "nora.command_engine._log_audit":
                    stack.enter_context(mock.patch(target, autospec=True, return_value=value))
            # The fast path answers both; the model must never be asked.
            stack.enter_context(mock.patch("nora.intent_parser.parse_intent",
                                           side_effect=AssertionError("the model was called")))
            stack.enter_context(mock.patch.object(task_commands, "_calendar_on", return_value=[]))

            with mock.patch.object(task_commands, "date", _fake_date(WED)):
                said = await dev.say("Remember I have to submit the assignment tomorrow")
            self.assertIn("Got it: Submit the assignment, due tomorrow.", said)
            [task] = task_ledger.get_open_tasks()
            self.assertEqual(task["due_on"], "2026-10-01")

            spoken: list[str] = []

            async def no_confirm():
                raise AssertionError("reading the agenda must not ask")

            deps = wiring.build(listener=None, frustration=FrustrationTracker(),
                                speak=lambda t, **k: spoken.append(t), confirm=no_confirm)
            with mock.patch.object(task_commands, "date", _fake_date(WED + timedelta(days=1))):
                outcome = await pipeline.handle_turn("what do I need to do today?", deps)
        self.assertEqual(outcome.kind, "executed")
        self.assertIn("Due today: Submit the assignment.", " ".join(spoken))

    async def test_reminder_reaches_the_phone_titled_as_one(self) -> None:
        dev = await self.paired()
        self.assertEqual(delivery.broadcast("Reminder, sir: stretch"), [dev.device_id])
        await self.until(lambda: dev.notified)
        self.assertEqual((dev.notified[0]["title"], dev.notified[0]["kind"]), ("Reminder", "reminder"))


if __name__ == "__main__":
    unittest.main()

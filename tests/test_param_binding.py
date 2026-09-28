"""Parameter binding between the intent parser's words and the handlers'.

The parser picks a keyword name from the same vocabulary a person would, and
it does not always land on the one in the registered signature. Before
`bind_params` that was fatal: `handler(**params)` raised TypeError, and the
pipeline spoke the exception out loud — "Failed: add_calendar_event() got an
unexpected keyword argument 'title'" — for a request it had understood
perfectly. These tests pin the synonyms that were actually observed failing.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from nora.command_engine import bind_params
from nora.commands.google_services import _parse_date, add_calendar_event


def _handler(summary: str = "", date: str = "today", time: str = "") -> str:
    return f"{summary}|{date}|{time}"


class BindParamsTest(unittest.TestCase):
    def test_exact_names_pass_through(self):
        bound, unplaced = bind_params(_handler, {"summary": "Standup", "date": "friday"})
        self.assertEqual(bound, {"summary": "Standup", "date": "friday"})
        self.assertEqual(unplaced, [])

    def test_observed_synonyms_are_bound(self):
        """The three that failed in real use, in the words the parser used."""
        for alias, target in (("title", "summary"), ("when", "date"), ("datetime", "date")):
            with self.subTest(alias=alias):
                bound, unplaced = bind_params(_handler, {alias: "x"})
                self.assertEqual(bound, {target: "x"})
                self.assertEqual(unplaced, [])

    def test_a_real_parameter_is_never_displaced_by_an_alias(self):
        """`time` is a parameter here, so `at` must not overwrite it."""
        bound, _ = bind_params(_handler, {"time": "4pm", "at": "9am"})
        self.assertEqual(bound["time"], "4pm")

    def test_explicit_name_wins_over_its_synonym(self):
        bound, _ = bind_params(_handler, {"summary": "real", "title": "alias"})
        self.assertEqual(bound["summary"], "real")

    def test_unknown_keyword_is_dropped_not_raised(self):
        bound, unplaced = bind_params(_handler, {"summary": "x", "colour": "red"})
        self.assertEqual(bound, {"summary": "x"})
        self.assertEqual(unplaced, ["colour"])
        _handler(**bound)  # the point of dropping: the call still works

    def test_var_keyword_handler_receives_everything(self):
        def greedy(**kwargs):
            return kwargs

        params = {"anything": 1, "at": 2}
        bound, unplaced = bind_params(greedy, params)
        self.assertEqual(bound, params)
        self.assertEqual(unplaced, [])


class ParseDateTest(unittest.TestCase):
    def test_bare_relative_days(self):
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.assertEqual(_parse_date("today").date(), today.date())
        self.assertEqual(_parse_date("tomorrow").date(), (today + timedelta(days=1)).date())
        self.assertEqual(_parse_date("").date(), today.date())

    def test_relative_day_carrying_a_time(self):
        """The regression: dateutil cannot read "tomorrow", so this used to
        fall through to `now` and book the event today."""
        dt = _parse_date("tomorrow at 4 pm")
        self.assertEqual(dt.date(), (datetime.now() + timedelta(days=1)).date())
        self.assertEqual((dt.hour, dt.minute), (16, 0))

    def test_relative_day_with_comma(self):
        dt = _parse_date("tomorrow, 9:30am")
        self.assertEqual(dt.date(), (datetime.now() + timedelta(days=1)).date())
        self.assertEqual((dt.hour, dt.minute), (9, 30))

    def test_no_time_of_day_is_midnight(self):
        self.assertEqual((_parse_date("tomorrow").hour, _parse_date("tomorrow").minute), (0, 0))

    def test_unparseable_string_does_not_raise(self):
        self.assertIsInstance(_parse_date("sometime whenever"), datetime)


class AddCalendarEventTest(unittest.TestCase):
    """Only the paths that return before touching Google."""

    def test_missing_name_asks_instead_of_raising(self):
        self.assertEqual(add_calendar_event(), "What should I call that event?")

    def test_the_two_failing_utterances_now_bind(self):
        """Both turns from the bug report, as the parser emitted them."""
        for params in (
            {"title": "CV Project Review", "when": "tomorrow"},
            {"title": "CV Project Review", "datetime": "tomorrow at 4 pm"},
        ):
            with self.subTest(params=params):
                bound, unplaced = bind_params(add_calendar_event, params)
                self.assertEqual(unplaced, [])
                self.assertEqual(bound["summary"], "CV Project Review")
                self.assertIn("tomorrow", bound["date"])


if __name__ == "__main__":
    unittest.main()

"""People talk over NORA by stopping her and asking in one breath.

"Stop, what day is it" used to stop everything and drop the question. A stop
followed by a question keeps the question as the turn; a stop followed by
anything else ("stop the music") is still just a stop.
"""
import pytest

from nora.pipeline import _question_after_stop


@pytest.mark.parametrize("text, question", [
    ("Stop, what day is it?", "what day is it?"),
    ("stop what time is it", "what time is it"),
    ("cancel that, what's the weather", "what's the weather"),
    ("Shut up. Who won the match", "Who won the match"),
    ("quiet - is my phone charging", "is my phone charging"),
])
def test_question_after_stop_is_kept(text, question):
    assert _question_after_stop(text) == question


@pytest.mark.parametrize("text", [
    "stop",
    "stop the music",
    "cancel the timer",
    "pause everything",
    "what day is it",
    "stopwatch for five minutes",
])
def test_plain_stop_or_no_stop_gives_none(text):
    assert _question_after_stop(text) is None

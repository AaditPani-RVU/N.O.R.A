"""When NORA decides the user is stuck: not for asking about three things in a row."""
from __future__ import annotations

from nora.frustration import FrustrationTracker


def test_the_same_question_answered_each_time_is_not_frustration():
    # Seen live: three "what is this" photos, each answered, and NORA offered help.
    t = FrustrationTracker()
    assert not any(t.record("what is this") for _ in range(3))
    t = FrustrationTracker()
    assert not any(t.record("next track") for _ in range(3))


def test_question_words_and_app_names_are_not_frustration_words():
    t = FrustrationTracker()
    assert not any(t.record(x) for x in ["what time is it", "open whatsapp", "why is the sky blue",
                                         "what is this"])


def test_repeating_something_that_keeps_failing_still_counts():
    t = FrustrationTracker()
    hits = [t.record("call mama", success=False) for _ in range(3)]
    assert hits[-1]


def test_a_frustration_word_with_a_failure_counts():
    t = FrustrationTracker()
    t.record("play my playlist", success=False)
    t.record("play my playlist", success=False)
    assert t.record("ugh it's still not working", success=False)

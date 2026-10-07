"""A wake phrase is a whole utterance, not a word inside a request.

"Set an alarm to wake up at 6" contained "wake up", got "I'm already awake",
and the alarm was never set.
"""
import pytest

from nora.pipeline import is_wake_phrase


@pytest.mark.parametrize("text", [
    "wake up", "Wake up, Nora.", "Hey Nora, wake up", "Daddy's home", "daddys home",
    "wake up, daddy's home",
])
def test_whole_wake_phrase_wakes(text):
    assert is_wake_phrase(text)


@pytest.mark.parametrize("text", [
    "Set an alarm to wake up at 6", "remind me to wake up", "daddy's home and play music",
    "what time is it",
])
def test_wake_words_inside_a_request_do_not(text):
    assert not is_wake_phrase(text)

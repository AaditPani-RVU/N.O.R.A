"""Personal vocabulary (NORA_ASSIST_PLAN A)."""
from __future__ import annotations

import pytest

from nora import fast_path, store, vocabulary


@pytest.fixture(autouse=True)
def _clean():
    with store.transaction() as conn:
        conn.execute("DELETE FROM vocabulary")
    vocabulary.reset_for_tests()
    yield
    vocabulary.reset_for_tests()


def test_taught_mishearing_is_repaired_as_whole_words():
    vocabulary.learn("Hormuz", "vermouth")
    assert vocabulary.repair("what's the state of vermouth now") == "what's the state of Hormuz now"
    assert vocabulary.repair("Vermouth, is it open?") == "Hormuz, is it open?"
    # Not inside another word.
    assert vocabulary.repair("vermouthy") == "vermouthy"


def test_longer_phrases_replaced_first():
    vocabulary.learn("Deftones", "deaf tools")
    vocabulary.learn("Def", "deaf")
    assert vocabulary.repair("play risk by deaf tools") == "play risk by Deftones"


def test_taught_words_lead_the_prompt(monkeypatch):
    monkeypatch.setattr(vocabulary, "_artists", lambda: ["Daft Punk", "Deftones"])
    vocabulary.learn("Milo")
    vocabulary.learn("Deftones")
    names = vocabulary.words()
    assert names[0] in ("Milo", "Deftones")
    assert names.count("Deftones") == 1          # taught and an artist: once
    assert "Daft Punk" in vocabulary.whisper_prompt()


def test_forget():
    vocabulary.learn("Hormuz", "vermouth")
    assert "Forgot" in vocabulary.forget("Hormuz")
    assert vocabulary.repair("vermouth") == "vermouth"


@pytest.mark.parametrize("said,word,heard", [
    ("You misheard, I said Hormuz, not vermouth", "Hormuz", "vermouth"),
    ("I said Deftones not deaf tools", "Deftones", "deaf tools"),
    ("when I say pink fluid I mean Pink Floyd", "Pink Floyd", "pink fluid"),
])
def test_fast_path_teaches(said, word, heard):
    r = fast_path.resolve(said)
    assert r.steps[0].action == "learn_word"
    assert r.steps[0].parameters == {"word": word, "heard": heard}


def test_whisper_prompt_is_bounded(monkeypatch):
    monkeypatch.setattr(vocabulary, "_artists", lambda: [f"Artist {i}" for i in range(500)])
    assert len(vocabulary.whisper_prompt().split(", ")) <= vocabulary.PROMPT_WORDS

"""Story watch (NORA_ASSIST_PLAN D). The network and the model are faked."""
from __future__ import annotations

import pytest

from nora import assist, calls, model_router, store, story_watch


@pytest.fixture
def news(monkeypatch):
    feed: list[str] = []
    monkeypatch.setattr(story_watch, "_fetch", lambda topic: [{"title": t} for t in feed])
    asked: list[list[dict]] = []

    def fake_complete(role, messages, **kw):
        asked.append(messages)
        assert role == "background"
        return "Shipping resumed through the strait after talks.", "fake"
    monkeypatch.setattr(model_router, "complete", fake_complete)
    told: list[str] = []
    monkeypatch.setattr(assist, "tell", lambda text, **kw: told.append(text) or True)
    monkeypatch.setattr(calls, "quiet_now", lambda now=None: False)
    with store.transaction() as conn:
        conn.execute("DELETE FROM story_watch")
    return feed, asked, told


def _age_checks():
    with store.transaction() as conn:
        conn.execute("UPDATE story_watch SET last_check = 0")


def test_watch_seeds_with_what_is_out_now(news):
    feed, asked, _ = news
    feed[:] = ["Tanker held near Hormuz", "Iran warns US"]
    assert "keep an eye on the Strait of Hormuz" in story_watch.watch("the Strait of Hormuz")
    _age_checks()
    story_watch.check()
    assert asked == []                                  # nothing new yet
    assert story_watch.briefing_line() is None


def test_new_headlines_become_a_pending_change_for_the_briefing(news):
    feed, asked, told = news
    feed[:] = ["Old headline"]
    story_watch.watch("Hormuz")
    feed[:] = ["Old headline", "Strait reopens to shipping", "Oil falls as tankers move"]
    _age_checks()
    story_watch.check()
    assert len(asked) == 1 and told == []               # 2 new: not a burst
    assert "<headlines>" in asked[0][1]["content"]
    line = story_watch.briefing_line()
    assert line == "On Hormuz: Shipping resumed through the strait after talks."
    assert story_watch.briefing_line() is None          # said once


def test_burst_goes_to_the_phone(news):
    feed, _, told = news
    story_watch.watch("Hormuz")
    feed[:] = [f"Headline {i}" for i in range(30)]
    _age_checks()
    story_watch.check()
    assert told and told[0].startswith("Update on Hormuz:")


def test_update_by_partial_name_and_nothing_new(news):
    feed, _, _ = news
    feed[:] = ["A"]
    story_watch.watch("the Strait of Hormuz")
    assert story_watch.update("Hormuz").startswith("Nothing new on the Strait of Hormuz")
    feed[:] = ["A", "B"]
    assert "Shipping resumed" in story_watch.update("Hormuz")


def test_unwatch_and_limit(news):
    story_watch.watch("Hormuz")
    assert story_watch.unwatch("hormuz") == "Stopped watching Hormuz."
    assert "not watching" in story_watch.listing()


def test_model_failure_falls_back_to_headline(news, monkeypatch):
    feed, _, _ = news
    story_watch.watch("Hormuz")
    feed[:] = ["Strait reopens", "Oil falls"]

    def boom(*a, **k):
        raise model_router.AllCandidatesFailed("down")
    monkeypatch.setattr(model_router, "complete", boom)
    _age_checks()
    story_watch.check()
    assert story_watch.briefing_line() == "On Hormuz: Strait reopens."

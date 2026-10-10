"""Tool retrieval: the intent model sees the actions an utterance could need
(Sharp Phase C). The private eval set scores it on real utterances
(tests/test_evals.py); these cover the mechanics on public phrasings."""
from __future__ import annotations

import logging
from unittest import mock

import pytest

from nora import command_engine, tool_retrieval


@pytest.fixture(scope="module", autouse=True)
def commands():
    logging.disable(logging.CRITICAL)
    command_engine.discover_commands()
    logging.disable(logging.NOTSET)


@pytest.fixture
def words_only():
    """The word score alone: what NORA falls back to without the embedder."""
    with mock.patch.object(tool_retrieval, "_embed_ok", False):
        yield


@pytest.mark.parametrize("text, action", [
    ("connect to my bluetooth headphones", "bluetooth_connect"),
    ("what time is it in my timezone", "get_time"),
    ("lock the laptop", "lock_screen"),
    ("remind me in ten minutes to stretch", "remind_me"),
    ("every morning at 7 read me the news", "schedule_task"),
    ("put on some radiohead", "play_music"),
    ("delete everything in the downloads folder", "delete_file"),
    ("explain this traceback", "explain_error"),
    ("what's my dog's name", "recall"),
])
def test_words_alone_find_the_action(words_only, text, action):
    assert action in tool_retrieval.select(text)


def test_core_actions_are_always_offered(words_only):
    picked = tool_retrieval.select("connect to my bluetooth headphones")
    for action in tool_retrieval.CORE:
        assert action in picked
    assert len(picked) <= len(tool_retrieval.CORE) + tool_retrieval.K


def test_a_follow_up_keeps_the_turn_before(words_only):
    # "but I want it in 1 minute not 5" says nothing about reminders.
    text = "but I want it in 1 minute not 5"
    assert "remind_me" in tool_retrieval.select(text, previous=["remind_me"])
    assert "remind_me" in tool_retrieval.select(text, previous_text="remind me in five minutes to stretch")


def test_mcp_tools_are_never_offered(words_only):
    with mock.patch.dict(command_engine._registry, {"mcp_github_search": lambda: None}):
        assert "mcp_github_search" not in tool_retrieval.scores("search github")


def test_a_device_connecting_is_indexed(words_only):
    assert "phone.flashlight" not in tool_retrieval.scores("turn on the flashlight")
    command_engine.register_device_capability(
        "phone.flashlight", lambda **_: None, device="d_test", sig="phone.flashlight(on: bool)",
        description="The phone's torch", risk="low")
    try:
        assert "phone.flashlight" in tool_retrieval.select("turn on the flashlight on my phone")
    finally:
        command_engine.unregister_device("d_test")
    assert "phone.flashlight" not in tool_retrieval.scores("turn on the flashlight")


def test_retrieval_is_fast_without_the_embedder(words_only):
    import time
    tool_retrieval.select("warm")
    started = time.monotonic()
    for _ in range(20):
        tool_retrieval.select("set the volume to forty percent")
    assert (time.monotonic() - started) / 20 < 0.02

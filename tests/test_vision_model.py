"""The Groq vision model behind the camera and read_screen: when Groq retires
it (twice so far), NORA moves to the next version instead of failing every look."""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from nora.commands import screen_intelligence as si


class Gone(Exception):
    status_code = 404

    def __init__(self, model: str):
        super().__init__(f"Error code: 404 - The model `{model}` does not exist "
                         "or you do not have access to it. 'code': 'model_not_found'")


def fake_client(listed: list[str], alive: set[str]):
    calls: list[str] = []

    def create(model, **_):
        calls.append(model)
        if model not in alive:
            raise Gone(model)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="<think>hmm</think> A red circle."),
            finish_reason="stop")])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        models=SimpleNamespace(list=lambda: SimpleNamespace(
            data=[SimpleNamespace(id=m) for m in listed])))
    return client, calls


@pytest.fixture(autouse=True)
def fresh():
    si._successor.clear()
    with mock.patch.object(si, "get_config", return_value={
            "screen_intelligence": {"vision_model": "qwen/qwen3.6-27b"}}):
        yield
    si._successor.clear()


def test_a_retired_model_moves_to_its_newest_version_and_stays_there():
    listed = ["openai/gpt-oss-120b", "qwen/qwen3-32b", "qwen/qwen3.8-27b", "qwen/qwen3.10-27b"]
    client, calls = fake_client(listed, alive={"qwen/qwen3.8-27b", "qwen/qwen3.10-27b"})
    with mock.patch.object(si, "_get_client", return_value=client):
        assert si._vision("aGk=", "what is this") == "A red circle."
        assert si._vision("aGk=", "and now") == "A red circle."
    # 3.10 beats 3.8, and the text-only qwen3-32b is never a candidate.
    assert calls == ["qwen/qwen3.6-27b", "qwen/qwen3.10-27b", "qwen/qwen3.10-27b"]


def test_with_no_successor_the_original_error_stands():
    client, _ = fake_client(["openai/gpt-oss-120b", "qwen/qwen3-32b"], alive=set())
    with mock.patch.object(si, "_get_client", return_value=client), pytest.raises(Gone):
        si._vision("aGk=", "what is this")


def test_other_errors_are_not_mistaken_for_a_retired_model():
    client, calls = fake_client(["qwen/qwen3.8-27b"], alive={"qwen/qwen3.8-27b"})

    def boom(**_):
        raise RuntimeError("rate limited")
    client.chat.completions.create = boom
    with mock.patch.object(si, "_get_client", return_value=client), pytest.raises(RuntimeError):
        si._vision("aGk=", "what is this")
    assert si._successor == {}

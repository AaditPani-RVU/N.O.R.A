"""nora.doctor: finds what stopped working, and says so once.

No network here: provider model lists and the other checks are stubbed.
"""
from __future__ import annotations

from unittest import mock

import pytest

from nora import delivery, doctor
from nora.doctor import Finding

ROLES = {"llm_router": {"roles": {
    "intent": [{"name": "groq_big", "provider": "groq", "base_url": "https://g/v1",
                "api_key_env": "G_KEY", "model": "openai/gpt-oss-120b"}],
    "live_search": [{"name": "groq_compound_mini", "provider": "groq", "base_url": "https://g/v1",
                     "api_key_env": "G_KEY", "model": "groq/compound-mini"},
                    {"name": "nv", "provider": "nvidia", "base_url": "https://n/v1",
                     "api_key_env": "N_KEY", "model": "nvidia/x"}],
}}}


def test_a_withdrawn_model_is_found_with_its_fix():
    lists = {("https://g/v1", "G_KEY"): {"openai/gpt-oss-120b"},
             ("https://n/v1", "N_KEY"): "N_KEY is not set"}
    with mock.patch.object(doctor, "get_config", return_value=ROLES), \
            mock.patch.object(doctor, "_model_lists", return_value=lists):
        found = {f.check: f for f in doctor.check_models()}
    assert found["model:intent:groq_big"].ok
    gone = found["model:live_search:groq_compound_mini"]
    assert not gone.ok and "no longer offered" in gone.detail and "tries it first" in gone.detail
    assert "llm_router.roles.live_search" in gone.fix
    nokey = found["model:live_search:nv"]
    assert not nokey.ok and "N_KEY" in nokey.fix


def test_a_failure_is_news_once_until_it_recovers():
    bad = [Finding("google", False, "Google sign-in expired", "python -m nora.doctor google-login"),
           Finding("hub", True, "ok")]
    fresh, state = doctor.news(bad, {})
    assert [f.check for f in fresh] == ["google"]

    fresh, state = doctor.news(bad, state)                 # the next day: still failing
    assert fresh == [] and "google" in state

    fresh, state = doctor.news([Finding("google", True, "ok")], state)   # fixed
    assert fresh == [] and state == {}

    fresh, _ = doctor.news(bad, state)                     # broken again: news again
    assert [f.check for f in fresh] == ["google"]


def test_tell_goes_to_the_phone_and_falls_back_to_the_speaker():
    f = [Finding("google", False, "Google sign-in expired", "python -m nora.doctor google-login")]
    spoken: list[str] = []
    with mock.patch.object(delivery, "broadcast", return_value=["d_phone"]) as sent:
        text = doctor.tell(f, lambda t, **_: spoken.append(t))
    assert sent.called and spoken == []
    assert "Google sign-in expired" in text and "google-login" in text

    with mock.patch.object(delivery, "broadcast", return_value=[]):
        doctor.tell(f, lambda t, **_: spoken.append(t))
    assert spoken and "Google sign-in expired" in spoken[0]


def test_check_and_tell_keeps_state_between_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "STATE_PATH", tmp_path / "state.json")
    findings = [Finding("google", False, "Google sign-in expired")]
    monkeypatch.setattr(doctor, "run_checks", lambda: findings)
    told: list[str] = []
    monkeypatch.setattr(doctor, "tell", lambda fresh, speak: told.extend(f.check for f in fresh))
    doctor.check_and_tell()
    doctor.check_and_tell()
    assert told == ["google"]


def test_a_check_that_raises_is_a_finding_not_a_crash(monkeypatch):
    def boom():
        raise RuntimeError("no route to host")
    monkeypatch.setattr(doctor, "CHECKS", [("google", boom), ("hub", lambda: [Finding("hub", True, "ok")])])
    found = doctor.run_checks()
    assert [f.ok for f in found] == [False, True]
    assert "no route to host" in found[0].detail


def test_failed_command_modules_are_reported(monkeypatch):
    from nora import command_engine
    monkeypatch.setattr(command_engine, "_load_failures",
                        {"nora.commands.system_control": "KeyError: 'DISPLAY'"})
    found = doctor.check_commands()
    assert len(found) == 1 and not found[0].ok
    assert "system_control" in found[0].detail


@pytest.mark.parametrize("age, ok", [(3600, True), (3 * 86400, False)])
def test_a_device_not_seen_for_days_is_reported(monkeypatch, age, ok):
    import time
    from nora.hub import registry
    dev = mock.Mock(id="d_1", approved_at=1.0, revoked_at=None, last_seen=time.time() - age)
    dev.name = "Pixel 10"
    monkeypatch.setattr(registry, "listing", lambda: [dev])
    (f,) = doctor.check_phone()
    assert f.ok is ok
    if not ok:
        assert "revoke d_1" in f.fix

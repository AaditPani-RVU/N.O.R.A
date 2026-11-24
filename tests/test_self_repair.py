"""Self-repair overnight (NORA_ASSIST_PLAN G). No real Claude run, no real merge."""
from __future__ import annotations

import json
import time
from datetime import datetime

import pytest

from nora import audit_log, fast_path, self_repair


@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit_log, "_LOG_PATH", path)
    monkeypatch.setattr(self_repair, "STATE_PATH", tmp_path / "state.json")

    def write(*entries):
        with path.open("a") as f:
            for e in entries:
                f.write(json.dumps({"ts": time.time(), "ts_human": "now", **e}) + "\n")
    return write


def test_counts_hidden_failures_but_not_the_world(log):
    log({"action": "add_calendar_event", "success": True, "result": "I couldn't add that event to your calendar."},
        {"action": "add_calendar_event", "success": False, "result": "boom"},
        {"action": "phone.call_contact", "success": True, "result": "There's no contact called Nora on the phone."},
        {"action": "look_with_phone", "success": False, "result": "Your phone isn't connected"},
        {"action": "get_time", "success": True, "result": "It's 4 pm."})
    f = self_repair.failures()
    assert list(f) == ["add_calendar_event"] and len(f["add_calendar_event"]) == 2


def test_pick_needs_enough_failures_and_waits_between_tries(log):
    log({"action": "read_screen", "success": False, "result": "x"})
    assert self_repair.pick() is None
    log({"action": "read_screen", "success": False, "result": "y"})
    assert self_repair.pick()[0] == "read_screen"
    self_repair._save({"attempts": {"read_screen": time.time()}, "last": None})
    assert self_repair.pick() is None


def test_prompt_carries_the_rules(log):
    p = self_repair.prompt_for("check_calendar", [{"said": "what's on", "params": {}, "result": "I couldn't",
                                                   "at": "t"}])
    assert "Do NOT add Co-Authored-By" in p and "Never push" in p and '"check_calendar"' in p


def test_briefing_tells_once_and_merge_refuses_failing_tests(log):
    self_repair._save({"attempts": {}, "last": {
        "action": "add_calendar_event", "failures": 6, "branch": "repair/x", "changed": True,
        "tests_ok": False, "told": False, "merged": False, "stat": "1 file changed", "summary": "s"}})
    line = self_repair.briefing_line()
    assert "fix on repair/x" in line and "tests fail" in line
    assert self_repair.briefing_line() is None
    assert "won't merge" in self_repair.merge()


def test_check_runs_once_a_night_inside_its_window(log, monkeypatch):
    from nora import follow_through, store
    with store.transaction() as conn:
        conn.execute("DELETE FROM nudges")
    ran: list[int] = []
    monkeypatch.setattr(self_repair, "attempt", lambda now=None: ran.append(1))
    self_repair.check(datetime(2026, 11, 25, 12, 0))
    self_repair.check(datetime(2026, 11, 25, 1, 40))
    for _ in range(50):
        if ran:
            break
        time.sleep(0.02)
    self_repair.check(datetime(2026, 11, 25, 2, 0))
    time.sleep(0.05)
    assert ran == [1]


def test_trailers_are_stripped(tmp_path):
    import subprocess
    repo = tmp_path / "r"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True)  # noqa: E731
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (repo / "f").write_text("a")
    run("add", "f")
    run("commit", "-qm", "base")
    base = run("rev-parse", "HEAD").stdout.strip()
    (repo / "f").write_text("b")
    run("commit", "-qam", "Fix it\n\nBecause.\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    self_repair._strip_trailers(repo, base)
    msg = run("log", "-1", "--format=%B").stdout
    assert "Co-Authored-By" not in msg and msg.startswith("Fix it")


@pytest.mark.parametrize("said,action", [("show me the fix", "repair_status"),
                                         ("merge the fix", "merge_repair")])
def test_fast_path(said, action):
    assert fast_path.resolve(said).steps[0].action == action

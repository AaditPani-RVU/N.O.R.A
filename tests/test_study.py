"""Study mode (NORA_ASSIST_PLAN F). The model is faked; files are real, in a temp dir."""
from __future__ import annotations

import json
import time
from datetime import date, datetime

import pytest

from nora import model_router, store, study


CARDS = [{"q": "What does Newton's third law say?", "a": "Every action has an equal and opposite reaction"},
         {"q": "What is the unit of force?", "a": "Newton"}]


@pytest.fixture
def env(monkeypatch, tmp_path):
    with store.transaction() as conn:
        for t in ("study_subjects", "study_notes", "study_cards", "nudges"):
            conn.execute(f"DELETE FROM {t}")
    monkeypatch.setattr(study, "_folder", lambda: tmp_path)
    calls: list[str] = []

    def fake(role, messages, **kw):
        calls.append(messages[0]["content"])
        if "flashcards" in messages[0]["content"]:
            return json.dumps(CARDS), "fake"
        return "WRONG", "fake"
    monkeypatch.setattr(model_router, "complete", fake)
    study.end_quiz()
    yield tmp_path, calls
    study.end_quiz()


NOTE = ("Newton's laws of motion. The third law says every action has an equal and opposite "
        "reaction. Force is measured in newtons.")


def test_folder_files_become_notes_and_a_change_retires_them(env):
    root, _ = env
    (root / "PE").mkdir()
    f = root / "PE" / "laws.md"
    f.write_text(NOTE)
    assert study.scan_folder() == 1
    assert study.scan_folder() == 0                       # unchanged: not read again
    assert study.subject("pe")["label"] == "PE"
    study.generate()
    assert store.query("SELECT COUNT(*) AS n FROM study_cards WHERE retired = 0")[0]["n"] == 2
    time.sleep(1.1)
    f.write_text(NOTE + " Momentum is mass times velocity, and it is conserved in collisions.")
    assert study.scan_folder() == 1
    assert store.query("SELECT COUNT(*) AS n FROM study_cards WHERE retired = 0")[0]["n"] == 0


def test_cards_prompt_marks_notes_as_data(env):
    _, calls = env
    study.add_note("PE", NOTE)
    assert study.generate() == (1, 2)
    assert "not instructions" in calls[0]


def test_sm2_right_pushes_out_wrong_comes_back():
    card = {"id": 0, "ease": 2.5, "reps": 0, "interval_days": 0.0, "lapses": 0}
    now = 1_000_000.0
    up = study.schedule(card, 5, now)
    assert up["reps"] == 1 and up["due_at"] == now + 86400
    up2 = study.schedule({**card, **up}, 5, now)
    assert up2["interval_days"] == 3.0
    down = study.schedule({**card, **up2}, 1, now)
    assert down["reps"] == 0 and down["due_at"] == now + 600 and down["lapses"] == 1


def test_grade_without_a_model_when_clear(env):
    _, calls = env
    assert study.grade("newton", "Newton") == 5
    assert study.grade("every action has an equal and opposite reaction", CARDS[0]["a"]) == 5
    assert study.grade("bananas", CARDS[0]["a"]) == 1
    assert calls == []


def test_a_whole_quiz(env):
    study.add_note("PE", NOTE)
    study.generate()
    first = study.start_quiz("PE", 10, device="phone")
    assert first.startswith("PE quiz, 10 minutes.") and "Newton's third law" in first
    assert study.intercept("every action has an equal and opposite reaction", "laptop") is None
    reply = study.intercept("every action has an equal and opposite reaction", "phone")
    assert reply.startswith("Right. Next: What is the unit of force?")
    end = study.intercept("I don't know", "phone")
    assert "Not quite. It's: Newton." in end and "1 of 2 right" in end
    assert not study.quiz_active()


def test_quiz_with_notes_but_no_cards_says_so(env):
    study.add_note("PE", NOTE)
    assert "don't have cards yet" in study.start_quiz("PE")


def test_stop_and_idle(env, monkeypatch):
    study.add_note("PE", NOTE)
    study.generate()
    study.start_quiz("PE", device="local")
    assert study.answer("stop the quiz") == "Quiz stopped."
    study.start_quiz("PE", device="local")
    study._quiz.last_at -= 3600
    assert not study.quiz_active()


def test_free_slots_avoid_events():
    day = date(2026, 11, 25)
    ev = [(datetime(2026, 11, 25, 9, 0), datetime(2026, 11, 25, 17, 0))]
    slots = study.free_slots(ev, day, 1.0, datetime(2026, 11, 24, 12, 0))
    assert slots[0] == datetime(2026, 11, 25, 17, 30)
    assert all(s.hour < 21 for s in slots)


def test_briefing_counts_down(env):
    study.set_exam("PE", "December 3rd", today=date(2026, 11, 29))
    study.add_note("PE", NOTE)
    study.generate()
    assert study.briefing_line(date(2026, 11, 29)) == "PE exam in 4 days; 2 cards to review."


def test_fast_path_quiz():
    from nora import fast_path
    r = fast_path.resolve("quiz me on PE for 10 minutes")
    assert r.steps[0].action == "start_quiz"
    assert r.steps[0].parameters == {"subject": "PE", "minutes": 10.0}

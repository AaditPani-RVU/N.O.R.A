"""Study mode by voice (NORA_ASSIST_PLAN F)."""
from __future__ import annotations

import asyncio
import logging

from nora.command_engine import register

logger = logging.getLogger("nora.commands.study")


@register(
    "start_quiz",
    sig="start_quiz(subject: str = '', minutes: float = 10)",
    description="Quiz the user out loud from their study flashcards: 'quiz me on PE for 10 "
                "minutes', 'let's revise CV'. The next replies are taken as answers",
    category="study",
)
def start_quiz(subject: str = "", minutes: float = 10) -> str:
    from nora import channel, study
    ch = channel.current()
    return study.start_quiz(subject, minutes, device=ch.device_id if ch else "local")


@register(
    "add_exam",
    sig="add_exam(subject: str, date: str)",
    description="Remember an exam date: 'my PE exam is on December 3rd' → add_exam('PE', 'December 3rd')",
    category="study",
)
def add_exam(subject: str, date: str) -> str:
    from nora import study
    return study.set_exam(subject, date)


@register(
    "add_study_note",
    sig="add_study_note(subject: str, text: str)",
    description="Add something said to a subject's study notes: 'add a PE note: Newton's third law ...'",
    category="study",
)
def add_study_note(subject: str, text: str) -> str:
    from nora import study
    return study.add_note(subject, text)


_TRANSCRIBE = ("Transcribe all the text in this image exactly, keeping headings and lists. "
               "Then describe any diagram or equation in one line. Nothing else.")


@register(
    "save_photo_to_notes",
    sig="save_photo_to_notes(subject: str)",
    description="Save the photo just taken with the phone to a subject's study notes: "
                "'save this to my PE notes'",
    category="study",
)
async def save_photo_to_notes(subject: str) -> str:
    from nora import photos, study
    photo = photos.latest()
    if photo is None:
        return "There's no recent photo. Say 'what is this' with the page in front of the camera first."
    try:
        text = await asyncio.get_running_loop().run_in_executor(None, photos.describe, photo, _TRANSCRIBE)
    except Exception as e:
        logger.warning("transcribing a photo for notes failed: %s", e)
        return "I couldn't read the photo; the vision model didn't answer."
    return study.add_note(subject, text, source="photo")


@register(
    "make_study_cards",
    sig="make_study_cards(subject: str = '')",
    description="Make flashcards from a subject's notes now, instead of overnight",
    category="study",
)
def make_study_cards(subject: str = "") -> str:
    from nora import study
    study.scan_folder()
    if subject and study.subject(subject) is None:
        return f"I don't have any {subject} notes yet."
    pending = store_more(subject)
    done, made = study.generate(subject, limit=3)
    if not done:
        return "I couldn't make cards right now; I'll try again tonight." if pending else \
            "No new notes to make cards from."
    more = store_more(subject)
    return f"Made {made} cards from {done} note{'s' if done != 1 else ''}.{more}"


def store_more(subject: str) -> str:
    from nora import store, study
    s = study.subject(subject) if subject else None
    sql = "SELECT COUNT(*) AS n FROM study_notes WHERE carded = 0 AND retired = 0"
    rows = store.query(sql + (" AND subject = ?" if s else ""), ((s["key"],) if s else ()))
    n = rows[0]["n"]
    return f" {n} more note{'s' if n != 1 else ''} will be done tonight." if n else ""


@register(
    "plan_study",
    sig="plan_study(subject: str, hours: float = 1, days: int = 3)",
    description="Find free time in the calendar over the next days and book study blocks "
                "for a subject: 'plan study time for PE'",
    category="study",
    requires_confirmation=True,
)
def plan_study(subject: str, hours: float = 1, days: int = 3) -> str:
    from nora import study
    try:
        starts = study.plan_blocks(subject, float(hours or 1), int(days or 3))
    except Exception as e:
        logger.warning("plan_study: calendar unreadable: %s", e)
        return "I couldn't read your calendar to find free time."
    if not starts:
        return "I couldn't find a free block in the next few days."
    study.book_blocks(subject, starts, float(hours or 1))
    said = ", ".join(s.strftime("%A %-I:%M %p").replace(":00 ", " ") for s in starts)
    return f"Booked {len(starts)} study block{'s' if len(starts) != 1 else ''}: {said}."


@register(
    "study_status",
    sig="study_status()",
    description="What the user is studying: subjects, exams, cards due",
    category="study",
)
def study_status() -> str:
    from nora import study
    subs = study.subjects()
    if not subs:
        return f"No study subjects yet. Put notes in {study._folder()}/<Subject>, or say: add a note."
    return study.briefing_line() or "Subjects: " + ", ".join(s["label"] for s in subs) + ". Nothing due."

"""NORA plugin: quick notes and daily briefing.

Voice commands:
  "quick note remember to check loss curves"   → quick_note(content="...")
  "note titled meeting remember agenda"        → quick_note(content="...", title="meeting")
  "daily briefing"                             → daily_briefing()
  "good morning briefing"                      → daily_briefing()
"""
from __future__ import annotations

import datetime
from pathlib import Path

from nora.command_engine import register

_NOTES_DIR = Path.home() / "Documents" / "NORA Notes"


def _ensure_notes_dir() -> Path:
    _NOTES_DIR.mkdir(parents=True, exist_ok=True)
    return _NOTES_DIR


@register("quick_note")
def quick_note(content: str, title: str = "") -> str:
    notes_dir = _ensure_notes_dir()
    now = datetime.datetime.now()
    safe_title = (title or "Notes").replace("/", "-").replace("\\", "-")
    filename = f"{now.strftime('%Y-%m-%d')} {safe_title}.md"
    note_path = notes_dir / filename
    timestamp = now.strftime("%H:%M")

    if note_path.exists():
        with open(note_path, "a", encoding="utf-8") as f:
            f.write(f"\n**{timestamp}** — {content}\n")
        return f"Added to today's note: {content[:60]}{'…' if len(content) > 60 else ''}."
    else:
        heading = title or now.strftime("%B %d, %Y")
        with open(note_path, "w", encoding="utf-8") as f:
            f.write(f"# {heading}\n\n**{timestamp}** — {content}\n")
        return f"Note created: {content[:60]}{'…' if len(content) > 60 else ''}."


@register("daily_briefing")
def daily_briefing() -> str:
    now = datetime.datetime.now()
    hour = now.hour
    if hour < 12:
        greeting = "Good morning"
    elif hour < 18:
        greeting = "Good afternoon"
    else:
        greeting = "Good evening"

    date_str = now.strftime("%A, %B %d")
    time_str = now.strftime("%I:%M %p").lstrip("0")
    parts = [f"{greeting}. It's {time_str} on {date_str}."]

    try:
        import psutil
        battery = psutil.sensors_battery()
        if battery:
            status = "charging" if battery.power_plugged else "on battery"
            parts.append(f"Battery at {int(battery.percent)}%, {status}.")
        mem = psutil.virtual_memory()
        if mem.percent > 80:
            parts.append(f"Memory is at {mem.percent:.0f}% — you might want to close some things.")
    except Exception:
        pass

    notes_dir = _NOTES_DIR
    if notes_dir.exists():
        today = now.strftime("%Y-%m-%d")
        count = len(list(notes_dir.glob(f"{today}*.md")))
        if count:
            parts.append(f"You have {count} note{'s' if count != 1 else ''} from today.")

    return " ".join(parts)

"""Desktop notifications, timed reminders, and WhatsApp messaging.

notify_me   â†' Windows toast notification (no extra deps)
remind_me   â†' Timed voice + toast reminder (background thread)
send_whatsapp â†' pywhatkit (requires WhatsApp Web open in default browser)
              Contacts can be phone numbers (+countrycode) or names
              mapped in config.yaml under contacts:
"""
from __future__ import annotations

import re

import logging
import subprocess
import threading
import time

from nora import scheduler
from nora.command_engine import register
from nora.config import get_config

logger = logging.getLogger("nora.commands.notifications")


def _toast(title: str, message: str) -> None:
    """Send a Windows toast notification via PowerShell (no extra packages)."""
    safe_title = title.replace('"', "'")
    safe_msg = message.replace('"', "'")
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null; "
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime] | Out-Null; "
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
        f'$t.SelectSingleNode("//text[@id=\'1\']").InnerText = "{safe_title}"; '
        f'$t.SelectSingleNode("//text[@id=\'2\']").InnerText = "{safe_msg}"; '
        "$n = [Windows.UI.Notifications.ToastNotification]::new($t); "
        '[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("NORA").Show($n)'
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            timeout=10,
        )
    except Exception as e:
        logger.debug("Toast notification failed (non-critical): %s", e)


@register("notify_me", sig="notify_me(message: str)",
           description="Send a Windows desktop notification", category="notification")
def notify_me(message: str) -> str:
    """Send a Windows desktop notification with the given message."""
    _toast("NORA", message)
    return f"Notification sent: {message}"


_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                 "seven": 7, "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20,
                 "thirty": 30, "forty": 40, "forty-five": 45, "sixty": 60}
_DURATION_RE = re.compile(
    r"^\s*(?:(?P<half>half\s+an?\s+hour)|(?P<n>\d+(?:\.\d+)?|[a-z-]+)\s*"
    r"(?P<unit>s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?))\s*$", re.I)


def duration_minutes(text: str | float | int | None) -> float | None:
    """"1 minute", "one minute", "90 seconds", "half an hour", 10 → minutes. None if unreadable."""
    if isinstance(text, (int, float)):
        return float(text)
    m = _DURATION_RE.match(str(text or ""))
    if not m:
        try:
            return float(str(text).strip())
        except ValueError:
            return None
    if m.group("half"):
        return 30.0
    raw = m.group("n").lower()
    n = float(raw) if raw[0].isdigit() else _NUMBER_WORDS.get(raw)
    if n is None:
        return None
    unit = m.group("unit").lower()
    return n / 60 if unit.startswith("s") else n * 60 if unit.startswith("h") else n


@register("remind_me", sig="remind_me(message: str, delay_minutes: float = 5.0)",
           description="Timed voice + toast reminder, in N minutes from now", category="notification")
def remind_me(message: str, delay_minutes: float | str = 5.0, duration: str = "") -> str:
    """Set a timed voice and desktop reminder.

    Backed by `nora.scheduler` rather than a sleeping thread. The old version
    was a `time.sleep` in a daemon thread, which meant every pending reminder
    died silently the moment NORA restarted — and a reminder you are not told
    about is worse than one you never set, because you stopped tracking it
    yourself. Schedules are durable, so the reminder survives a restart.

    Absolute and recurring times ("at 6pm", "every morning") go to
    `schedule_task`; this stays the simple relative case.
    """
    # The model has sent {"duration": "1 minute"} instead of delay_minutes;
    # dropping it silently set the reminder for the 5-minute default.
    minutes = duration_minutes(duration if duration else delay_minutes)
    if minutes is None or minutes <= 0:
        return f"I couldn't tell when to remind you about {message}. Say it with a time, like in 10 minutes."
    seconds = max(10, round(minutes * 60))
    # The spoken form is also the spec "what's scheduled" reads back.
    when = _say_delay(seconds)
    sched = scheduler.add(f"in {when}", f"remind: {message}")
    if sched is None:  # unparseable delay — fall back to speaking now
        return f"I couldn't set that reminder. {message}"
    return f"I'll remind you about that in {when}."


def _say_delay(seconds: int) -> str:
    """90 → "90 seconds", 60 → "1 minute", 7200 → "2 hours"."""
    def n(q: int, unit: str) -> str:
        return f"{q} {unit}" + ("" if q == 1 else "s")
    if seconds < 60 or (seconds < 180 and seconds % 60):
        return n(seconds, "second")
    if seconds % 3600 == 0:
        return n(seconds // 3600, "hour")
    return n(round(seconds / 60), "minute")


def _resolve_contact(contact: str) -> str:
    """Return a phone number for the contact (name or raw +number)."""
    if contact.startswith("+"):
        return contact
    contacts: dict[str, str] = get_config().get("contacts", {})
    # Case-insensitive lookup
    contact_lower = contact.lower()
    for name, number in contacts.items():
        if name.lower() == contact_lower:
            return number
    return contact  # pass through and let pywhatkit error naturally


@register("send_whatsapp", sig="send_whatsapp(contact: str, message: str)",
           description="Send WhatsApp message via pywhatkit (needs WhatsApp Web open)", category="notification",
           risk="high", requires_confirmation=True)
def send_whatsapp(contact: str, message: str) -> str:
    """Send a WhatsApp message (requires WhatsApp Web open in default browser).

    contact can be a phone number (+countrycode...) or a name defined
    in config.yaml under contacts:.
    """
    try:
        import pywhatkit as kit  # type: ignore
    except ImportError:
        return "pywhatkit is not installed. Run: pip install pywhatkit"

    phone = _resolve_contact(contact)
    if not phone.startswith("+"):
        return (
            f"Could not resolve contact '{contact}' to a phone number. "
            "Add it to config.yaml under contacts: or use a number starting with +."
        )

    try:
        kit.sendwhatmsg_instantly(phone, message, wait_time=15, tab_close=True, close_time=3)
        return f"WhatsApp message sent to {contact}."
    except Exception as e:
        logger.error("send_whatsapp failed: %s", e)
        return f"Failed to send WhatsApp message: {e}"

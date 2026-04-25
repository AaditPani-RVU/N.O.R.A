"""Desktop notifications, timed reminders, and WhatsApp messaging.

notify_me   â†’ Windows toast notification (no extra deps)
remind_me   â†’ Timed voice + toast reminder (background thread)
send_whatsapp â†’ pywhatkit (requires WhatsApp Web open in default browser)
              Contacts can be phone numbers (+countrycode) or names
              mapped in config.yaml under contacts:
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time

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


@register("notify_me")
def notify_me(message: str) -> str:
    """Send a Windows desktop notification with the given message."""
    _toast("NORA", message)
    return f"Notification sent: {message}"


@register("remind_me")
def remind_me(message: str, delay_minutes: float = 5.0) -> str:
    """Set a timed voice and desktop reminder."""
    delay_sec = float(delay_minutes) * 60

    def _fire() -> None:
        time.sleep(delay_sec)
        from nora import speaker
        reminder_text = f"Reminder, sir: {message}"
        _toast("NORA Reminder", message)
        speaker.speak(reminder_text)

    thread = threading.Thread(target=_fire, daemon=True, name="nora-reminder")
    thread.start()

    mins = int(delay_minutes)
    unit = "minute" if mins == 1 else "minutes"
    return f"I'll remind you about that in {mins} {unit}."


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


@register("send_whatsapp")
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

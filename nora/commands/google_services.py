"""Google Calendar and Gmail integration — direct Google API.

Setup (one-time):
  1. Go to https://console.cloud.google.com → New Project
  2. Enable "Google Calendar API" and "Gmail API"
  3. Create OAuth2 credentials → **Desktop app** → download the JSON
     (a "Web application" client will not work: it cannot complete a local
     consent flow without pre-registered redirect URIs)
  4. Drop it in the project root under the name it downloaded with —
     client_secret_<id>.apps.googleusercontent.com.json is expected
  5. Run NORA and issue any calendar command — browser OAuth flow runs once,
     token is saved to google_token.json for future runs.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from nora.command_engine import register

logger = logging.getLogger("nora.commands.google_services")

# parent x3, not x2: this file is nora/commands/, so two levels reaches the
# `nora` package and three reaches the project root. The setup notes above have
# always said "project root", and the code looked one directory above it — so
# following the instructions produced "Google credentials not found" with the
# file sitting exactly where it was asked for.
_ROOT = Path(__file__).resolve().parent.parent.parent
_TOKEN_FILE = _ROOT / "google_token.json"


def _find_client_secrets() -> Path | None:
    """Locate the OAuth client secrets file, whatever Google called it.

    The console hands you `client_secret_<id>.apps.googleusercontent.com.json`,
    never `credentials.json`, so requiring that exact name means every setup
    starts with an undocumented rename — and failing it produces "credentials
    not found" while the file sits in the directory being searched.

    `credentials.json` still wins if present, so an existing setup keeps
    working. Otherwise the newest matching download is used: re-downloading
    after recreating the client is the common repair, and the newest file is
    the one that repair produced.
    """
    named = _ROOT / "credentials.json"
    if named.exists():
        return named
    found = sorted(
        _ROOT.glob("client_secret*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return found[0] if found else None
_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
]


# ── Auth ───────────────────────────────────────────────────────────────────

def _get_creds():
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
    except ImportError:
        raise RuntimeError(
            "Run: pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
        )

    creds_file = _find_client_secrets()
    if creds_file is None:
        raise RuntimeError(
            "Google credentials not found. In Google Cloud Console create an "
            "OAuth client of type 'Desktop app', download the JSON, and drop it "
            f"in {_ROOT} — the client_secret_*.json name it arrives with is fine."
        )

    # A "Desktop app" client is the one this flow can complete. A "Web
    # application" client parses and then fails at consent with
    # redirect_uri_mismatch, because run_local_server redirects to a
    # localhost port that a web client would have to have registered in
    # advance. Saying so here beats debugging it in a browser.
    try:
        import json
        kind = next(iter(json.loads(creds_file.read_text())))
    except Exception:
        kind = "installed"
    if kind == "web":
        raise RuntimeError(
            f"{creds_file.name} is a 'Web application' OAuth client; this needs a "
            "'Desktop app' one. Create a new client of that type in Google Cloud "
            "Console and download it — the web client cannot complete a local "
            "consent flow without pre-registered redirect URIs."
        )

    creds = None
    if _TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE), _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), _SCOPES)
            creds = flow.run_local_server(port=0)
        _TOKEN_FILE.write_text(creds.to_json())

    return creds


def _calendar_service() -> Any:
    from googleapiclient.discovery import build
    return build("calendar", "v3", credentials=_get_creds())


def _gmail_service() -> Any:
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=_get_creds())


# ── Date helpers ───────────────────────────────────────────────────────────

def _parse_date(date_str: str) -> datetime:
    """Parse a loose date string into a datetime (local timezone)."""
    from dateutil.parser import parse as _parse
    now = datetime.now()
    if date_str in ("", "today"):
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if date_str == "tomorrow":
        return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        return _parse(date_str, default=now)
    except Exception:
        return now


def _day_window(dt: datetime) -> tuple[str, str]:
    """Return (time_min, time_max) ISO strings covering the day of dt."""
    local_tz = datetime.now().astimezone().tzinfo
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(local_tz)
    end = dt.replace(hour=23, minute=59, second=59, microsecond=0).astimezone(local_tz)
    return start.isoformat(), end.isoformat()


def _fmt_event_time(ev: dict) -> str:
    start = ev["start"].get("dateTime", ev["start"].get("date", ""))
    try:
        return datetime.fromisoformat(start).strftime("%-I:%M %p")
    except Exception:
        return start


# ── Calendar commands ──────────────────────────────────────────────────────

@register("check_calendar", sig='check_calendar(when: str = "today")',
          description="Check Google Calendar for upcoming events", category="notification")
def check_calendar(when: str = "today") -> str:
    try:
        svc = _calendar_service()
        dt = _parse_date(when)
        time_min, time_max = _day_window(dt)

        result = svc.events().list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            maxResults=15,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = result.get("items", [])

        if not events:
            return f"Nothing on your calendar for {when}."

        parts = [f"{ev.get('summary', 'Untitled')} at {_fmt_event_time(ev)}" for ev in events]
        label = "event" if len(events) == 1 else "events"
        return f"You have {len(events)} {label} {when}: {', '.join(parts)}."
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        logger.error("check_calendar failed: %s", e)
        return "I couldn't check your calendar right now."


@register("add_calendar_event",
          sig='add_calendar_event(summary: str, date: str = "today", time: str = "")',
          description="Add an event to Google Calendar", category="notification")
def add_calendar_event(summary: str, date: str = "today", time: str = "") -> str:
    try:
        svc = _calendar_service()
        dt = _parse_date(date)

        if time:
            from dateutil.parser import parse as _parse
            dt = _parse(f"{dt.date()} {time}", default=dt)
            dt_end = dt + timedelta(hours=1)
            local_tz = datetime.now().astimezone().tzinfo
            event_body = {
                "summary": summary,
                "start": {"dateTime": dt.astimezone(local_tz).isoformat()},
                "end": {"dateTime": dt_end.astimezone(local_tz).isoformat()},
            }
        else:
            event_body = {
                "summary": summary,
                "start": {"date": str(dt.date())},
                "end": {"date": str(dt.date())},
            }

        svc.events().insert(calendarId="primary", body=event_body).execute()
        date_label = str(dt.date())
        time_label = f" at {dt.strftime('%-I:%M %p')}" if time else ""
        return f"Added '{summary}' to your calendar on {date_label}{time_label}."
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        logger.error("add_calendar_event failed: %s", e)
        return "I couldn't add that event to your calendar."


@register("delete_calendar_event",
          sig="delete_calendar_event(summary: str, date: str = \"today\")",
          description="Delete an event from Google Calendar by name", category="notification")
def delete_calendar_event(summary: str, date: str = "today") -> str:
    try:
        svc = _calendar_service()
        dt = _parse_date(date)
        time_min, time_max = _day_window(dt)

        result = svc.events().list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            maxResults=20,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = result.get("items", [])

        match = next(
            (ev for ev in events if summary.lower() in ev.get("summary", "").lower()),
            None,
        )
        if not match:
            return f"I couldn't find '{summary}' on {date}."

        svc.events().delete(calendarId="primary", eventId=match["id"]).execute()
        return f"Deleted '{match.get('summary')}' from your calendar."
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        logger.error("delete_calendar_event failed: %s", e)
        return "I couldn't delete that event."


# ── Gmail commands ─────────────────────────────────────────────────────────

@register("check_email", sig='check_email(filter: str = "unread")',
          description="Check Gmail for recent messages", category="notification")
def check_email(filter: str = "unread") -> str:
    try:
        svc = _gmail_service()
        query = "is:unread" if filter == "unread" else filter
        result = svc.users().messages().list(
            userId="me", q=query, maxResults=5
        ).execute()
        messages = result.get("messages", [])

        if not messages:
            return f"No {filter} emails found."

        summaries = []
        for msg in messages[:5]:
            detail = svc.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()
            headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
            sender = headers.get("From", "Unknown").split("<")[0].strip()
            subject = headers.get("Subject", "No subject")
            summaries.append(f"{sender}: {subject}")

        return f"You have {len(messages)} {filter} email{'s' if len(messages) != 1 else ''}. " + \
               " | ".join(summaries[:3]) + ("..." if len(summaries) > 3 else "")
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        logger.error("check_email failed: %s", e)
        return "I couldn't check your email right now."


@register("send_email", sig="send_email(to: str, subject: str, body: str)",
          description="Compose and send an email via Gmail", category="notification")
def send_email(to: str, subject: str, body: str) -> str:
    try:
        import base64
        from email.mime.text import MIMEText
        svc = _gmail_service()

        msg = MIMEText(body)
        msg["to"] = to
        msg["subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        return f"Email sent to {to}."
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        logger.error("send_email failed: %s", e)
        return "I couldn't send that email."

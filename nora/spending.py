"""Spending, from the payment notifications the phone already sees (NORA_ASSIST_PLAN E).

The phone forwards a notification as `money.notification` only when it looks
like a payment (`Payments.kt`): a rupee amount plus "paid"/"debited"/… from a
payments or banking app, or bank phrasing from anywhere else. Here it's parsed
into amount, direction and the other party, and **only those fields are kept**;
the text itself is never stored. A notification that is updated or re-posted
is one payment, not two: the digest of its text within a 10-minute bucket is
unique.

  "Paid to Swiggy" / "You paid ₹250 to Swiggy"          → debit 250, Swiggy
  "Rs.1,200.00 debited from a/c XX1234 to VPA swiggy@icici. Avl Bal Rs 5,000"
                                                         → debit 1200, Swiggy
  "INR 500 credited to your account via UPI from Rahul"  → credit 500, Rahul

"What did I spend this week?" / "how much on Swiggy this month?" → `summary`.
Table `spend` (store migration 13). Off with `spending.enabled: false`.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import date, datetime, timedelta

from nora import store
from nora.config import get_config

logger = logging.getLogger("nora.spending")

_AMOUNT = re.compile(r"(?:₹|\bRs\.?|\bINR)\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I)
_BALANCE = re.compile(r"\b(?:avl\.?|available|avbl\.?|total)\s*(?:bal(?:ance)?|limit)\b.*$", re.I)
_CREDIT = re.compile(r"\b(?:credited|received|refund(?:ed)?)\b", re.I)
_DEBIT = re.compile(r"\b(?:paid|debited|sent|spent|withdrawn|transferred|purchase)\b", re.I)
_PARTY = [
    re.compile(r"\bVPA\s+([\w.\-]+@[\w.\-]+)", re.I),
    re.compile(r"\b(?:paid|sent|transferred)\s+(?:(?:₹|Rs\.?|INR)\s?[\d,.]+\s+)?to\s+([A-Za-z][\w&.' \-]{1,40}?)"
               r"(?=\s+(?:on|via|from|for|using|ref|upi|txn)\b|[.,!]|$)", re.I),
    re.compile(r"\bto\s+([A-Za-z][\w&.' \-]{1,40}?)(?=\s+(?:on|via|from|for|using|ref|upi|txn)\b|[.,!]|$)", re.I),
    re.compile(r"\bat\s+([A-Za-z][\w&.' \-]{1,30}?)(?=\s+(?:on|via|for|ref|txn)\b|[.,!]|$)", re.I),
    re.compile(r"\bfrom\s+(?!(?:your\s+)?a/?c\b|account\b|xx)([A-Za-z][\w&.' \-]{1,40}?)"
               r"(?=\s+(?:on|via|to|for|ref|upi|txn)\b|[.,!]|$)", re.I),
]
_NOT_PARTY = re.compile(r"^(?:your|you|a/?c|account|xx\d+|the|card|bank|upi|wallet)\b", re.I)


def _cfg() -> dict:
    return get_config().get("spending", {}) or {}


def _party(raw: str) -> str:
    p = raw.strip(" .,-")
    if "@" in p:                                    # swiggy@icici, 9876543210@ybl
        p = p.split("@", 1)[0]
        if p.isdigit():
            return ""
        p = re.sub(r"[._\-]+", " ", p)
    p = re.sub(r"\s+", " ", p).strip()
    if not p or _NOT_PARTY.match(p):
        return ""
    return p if any(c.isupper() for c in p[1:]) else p.title()


def parse(title: str, text: str) -> dict | None:
    """{amount, direction, party} from a payment notification, or None."""
    body = f"{title}. {text}".strip()
    head = _BALANCE.sub("", body)                   # the balance is not the payment
    m = _AMOUNT.search(head)
    if not m:
        return None
    try:
        amount = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if amount <= 0:
        return None
    credit = bool(_CREDIT.search(head)) and not re.search(r"\bdebited\b", head, re.I)
    if not credit and not _DEBIT.search(head):
        return None
    party = ""
    order = _PARTY if not credit else [_PARTY[0], _PARTY[4], *_PARTY[1:4]]
    for pat in order:
        pm = pat.search(head)
        if pm:
            party = _party(pm.group(1))
            if party:
                break
    return {"amount": round(amount, 2), "direction": "credit" if credit else "debit", "party": party}


def record(device: str, data: dict, at: float | None = None) -> dict | None:
    """The hub's `money.notification` event. Keeps the parsed fields only."""
    if not _cfg().get("enabled", True):
        return None
    title, text = str(data.get("title", ""))[:200], str(data.get("text", ""))[:600]
    parsed = parse(title, text)
    if parsed is None:
        logger.info("spending: a payment-looking notification from %s didn't parse",
                    str(data.get("app", ""))[:30])
        return None
    at = at or time.time()
    digest = hashlib.sha256(f"{title}\n{text}\n{int(at // 600)}".encode()).hexdigest()[:20]
    with store.transaction() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO spend (at, amount, direction, party, app, digest) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (at, parsed["amount"], parsed["direction"], parsed["party"],
             str(data.get("app", ""))[:40], digest))
        if cur.rowcount == 0:
            return None
    logger.info("spending: %s ₹%.2f %s", parsed["direction"], parsed["amount"], parsed["party"] or "?")
    return parsed


# ── asking ───────────────────────────────────────────────────────────────────

def window(period: str, today: date | None = None) -> tuple[datetime, datetime, str]:
    today = today or date.today()
    p = (period or "week").lower().strip()
    start_of = lambda d: datetime.combine(d, datetime.min.time())   # noqa: E731
    if p in ("today",):
        return start_of(today), start_of(today + timedelta(days=1)), "today"
    if p in ("yesterday",):
        return start_of(today - timedelta(days=1)), start_of(today), "yesterday"
    if p in ("month", "this month"):
        return start_of(today.replace(day=1)), start_of(today + timedelta(days=1)), "this month"
    if p in ("last month",):
        first = today.replace(day=1)
        prev = (first - timedelta(days=1)).replace(day=1)
        return start_of(prev), start_of(first), "last month"
    if p in ("last week",):
        monday = today - timedelta(days=today.weekday())
        return start_of(monday - timedelta(days=7)), start_of(monday), "last week"
    monday = today - timedelta(days=today.weekday())
    return start_of(monday), start_of(today + timedelta(days=1)), "this week"


def _rupees(x: float) -> str:
    s = f"{x:,.2f}".rstrip("0").rstrip(".")
    return f"₹{s}"


def summary(period: str = "week", party: str = "", today: date | None = None) -> str:
    start, end, label = window(period, today)
    rows = store.query("SELECT amount, direction, party FROM spend WHERE at >= ? AND at < ?",
                       (start.timestamp(), end.timestamp()))
    if party:
        want = party.lower().strip()
        rows = [r for r in rows if want in (r["party"] or "").lower()]
    debits = [r for r in rows if r["direction"] == "debit"]
    credits = [r for r in rows if r["direction"] == "credit"]
    if not rows:
        where = f" on {party}" if party else ""
        return (f"I haven't seen any payments{where} {label}. I only see the ones the phone "
                "shows a notification for.")
    spent = sum(r["amount"] for r in debits)
    if party:
        out = f"You spent {_rupees(spent)} on {party} {label}, in {len(debits)} payment{'s' if len(debits) != 1 else ''}."
    else:
        out = f"You spent {_rupees(spent)} {label}, across {len(debits)} payment{'s' if len(debits) != 1 else ''}."
        by: dict[str, float] = {}
        for r in debits:
            by[r["party"] or "unknown"] = by.get(r["party"] or "unknown", 0) + r["amount"]
        top = [(k, v) for k, v in sorted(by.items(), key=lambda kv: -kv[1]) if k != "unknown"][:3]
        if top:
            out += " Most went to " + ", ".join(f"{k} ({_rupees(v)})" for k, v in top) + "."
    if credits:
        out += f" {_rupees(sum(r['amount'] for r in credits))} came in."
    return out

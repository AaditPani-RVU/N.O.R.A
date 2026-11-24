"""Spending from payment notifications (NORA_ASSIST_PLAN E)."""
from __future__ import annotations

import time
from datetime import date, datetime

import pytest

from nora import fast_path, spending, store, triggers


@pytest.fixture(autouse=True)
def _clean():
    with store.transaction() as conn:
        conn.execute("DELETE FROM spend")


@pytest.mark.parametrize("title,text,expected", [
    ("Paid to Swiggy", "You paid ₹250 to Swiggy", (250.0, "debit", "Swiggy")),
    ("AX-HDFCBK", "Rs.1,200.00 debited from a/c XX1234 on 24-11-26 to VPA swiggy@icici. Avl Bal Rs 5,000",
     (1200.0, "debit", "Swiggy")),
    ("JM-SBIUPI", "INR 500 credited to your account via UPI from Rahul Sharma", (500.0, "credit", "Rahul Sharma")),
    ("PhonePe", "₹99 sent to Ramesh Kumar", (99.0, "debit", "Ramesh Kumar")),
    ("ICICI", "INR 2,340.50 spent on ICICI Card XX9012 at AMAZON PAY on 24-Nov", (2340.5, "debit", "AMAZON PAY")),
])
def test_parse(title, text, expected):
    p = spending.parse(title, text)
    assert (p["amount"], p["direction"], p["party"]) == expected


def test_offers_are_not_payments():
    assert spending.parse("Google Pay", "Get ₹50 cashback on your next recharge") is None


def test_the_balance_is_not_the_payment():
    p = spending.parse("HDFC", "Avl Bal Rs 9,999 after Rs 120 debited to VPA chai@ybl")
    assert p is None or p["amount"] != 9999


def test_text_is_never_stored_and_reposts_count_once():
    data = {"app": "Google Pay", "title": "Paid to Swiggy", "text": "You paid ₹250 to Swiggy"}
    now = time.time()
    assert spending.record("phone", data, now)
    assert spending.record("phone", data, now + 30) is None          # re-posted
    cols = [r[1] for r in store.query("PRAGMA table_info(spend)")]
    assert "text" not in cols and "title" not in cols


def test_summary_by_week_and_party():
    today = date(2026, 11, 26)          # a Thursday
    at = datetime(2026, 11, 24, 13, 0).timestamp()
    spending.record("p", {"title": "Paid", "text": "You paid ₹250 to Swiggy"}, at)
    spending.record("p", {"title": "Paid", "text": "You paid ₹450 to Swiggy for dinner"}, at + 3600)
    spending.record("p", {"title": "Paid", "text": "₹99 sent to Ramesh Kumar"}, at + 7200)
    spending.record("p", {"title": "UPI", "text": "INR 500 credited to your account from Rahul"}, at + 9000)
    out = spending.summary("week", today=today)
    assert out.startswith("You spent ₹799 this week, across 3 payments.")
    assert "Swiggy (₹700)" in out and "₹500 came in" in out
    assert spending.summary("week", "swiggy", today=today).startswith("You spent ₹700 on swiggy")
    assert "haven't seen any payments" in spending.summary("last month", today=today)


def test_event_reaches_the_log():
    triggers.handle(triggers.Event("phone", "money.notification",
                                   {"title": "Paid", "text": "You paid ₹250 to Swiggy"}, time.time()))
    assert store.query("SELECT COUNT(*) AS n FROM spend")[0]["n"] == 1


@pytest.mark.parametrize("said,params", [
    ("what did I spend this week", {"period": "week", "party": ""}),
    ("how much did I spend on Swiggy this month", {"period": "month", "party": "Swiggy"}),
])
def test_fast_path(said, params):
    r = fast_path.resolve(said)
    assert r.steps[0].action == "spending" and r.steps[0].parameters == params

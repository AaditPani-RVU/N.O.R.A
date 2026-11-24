"""Spending by voice (NORA_ASSIST_PLAN E)."""
from __future__ import annotations

from nora.command_engine import register


@register(
    "spending",
    sig="spending(period: str = 'week', party: str = '')",
    description="How much the user spent, from the phone's payment notifications. period: "
                "today, yesterday, week, last week, month, last month. party: a payee like "
                "'Swiggy'. 'what did I spend this week', 'how much on Swiggy this month'",
    category="money",
)
def spending(period: str = "week", party: str = "") -> str:
    from nora import spending as sp
    return sp.summary(period, party)

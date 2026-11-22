"""'How fast is my phone's battery draining?' (Phase 10, nora.battery_stats)."""
from __future__ import annotations

from nora import battery_stats
from nora.command_engine import register


@register(
    "phone_battery_drain",
    sig="phone_battery_drain(days=7)",
    description="How fast the phone's battery drains while idle, measured over the last few days",
    category="system",
)
def phone_battery_drain(days: float = 7) -> str:
    try:
        days = max(1.0, min(30.0, float(days)))
    except (TypeError, ValueError):
        days = 7.0
    return battery_stats.spoken(battery_stats.drain(days=days), days)

"""The overnight self-repair, by voice (NORA_ASSIST_PLAN G)."""
from __future__ import annotations

from nora.command_engine import register


@register(
    "repair_status",
    sig="repair_status()",
    description="What NORA's overnight self-repair found and changed: 'show me the fix', "
                "'what did you fix last night'",
    category="system",
)
def repair_status() -> str:
    from nora import self_repair
    return self_repair.status()


@register(
    "merge_repair",
    sig="merge_repair()",
    description="Merge the overnight self-repair branch into NORA's live code and restart her. "
                "Only when its tests passed. 'merge the fix'",
    category="system",
    risk="high",
    requires_confirmation=True,
)
def merge_repair() -> str:
    from nora import self_repair
    return self_repair.merge()

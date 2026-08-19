"""Silent hours — Codex integration 5.6 (see CODEX_INTEGRATION.md).

Learns per-day-of-week quiet windows from the existing activity heatmap
(nora.cognitive_memory) — time bins with near-zero command volume across
the tracked history are treated as "the user doesn't want to be
interrupted then." Feeds focus.allows_proactive_speech() as one more
gate, alongside CALL/MEDIA/AWAY: during a learned silent hour, proactive
speech is deferred exactly like it is during a call.

Needs a minimum amount of history before it trusts the pattern — early
on (or with proactive/cognitive_memory disabled) it reports nothing
quiet and defers entirely to the other focus gates.
"""
from __future__ import annotations

from datetime import datetime

from nora.config import get_config


def _bin_totals() -> dict[tuple[str, str], int]:
    from nora.cognitive_memory import _load_user_model

    m = _load_user_model()
    totals: dict[tuple[str, str], int] = {}
    for tb, days in m.get("activity_heatmap", {}).items():
        for dow, actions in days.items():
            totals[(tb, dow)] = len(actions)
    return totals


def is_silent_now(now: datetime | None = None) -> bool:
    """True if the current time bin/day-of-week is a learned quiet window."""
    cfg = get_config().get("silent_hours", {})
    if not cfg.get("enabled", True):
        return False

    from nora.cognitive_memory import _time_bin

    totals = _bin_totals()
    total_activity = sum(totals.values())
    min_history = int(cfg.get("min_history_bins", 20))
    if total_activity < min_history:
        return False  # not enough history to trust a quiet pattern yet

    now = now or datetime.now()
    tb, dow = _time_bin(now), str(now.weekday())
    threshold = float(cfg.get("quiet_threshold_frac", 0.03))
    return totals.get((tb, dow), 0) <= total_activity * threshold


def status() -> str:
    """Natural-language state for the voice interface."""
    if is_silent_now():
        return "This is a learned quiet window for you — I'm holding proactive speech."
    return "Not currently in a learned quiet window."

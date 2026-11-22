"""How fast the phone's battery drains while idle (distributed plan Phase 10).

The exit criterion for hardening is "battery drain < 3 % a day idle". The
phone reports a `battery.sample` on every level change, charger change and
screen on/off (BatterySampler.kt); this keeps them and does the sums.

Idle is screen off and unplugged. Between two samples the phone was in the
state the first one reports, so an interval that starts screen-off and
unplugged, and ends unplugged, is idle time, and the level it lost is idle
drain. Gaps over `MAX_GAP_H` (the phone was off, or samples were lost) and
intervals where the level went up are left out.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from nora import store

MAX_GAP_H = 36.0
KEEP_DAYS = 30
MIN_IDLE_H = 6.0


def record(device: str, data: dict, at: float) -> bool:
    try:
        level = int(data["level"])
    except (KeyError, TypeError, ValueError):
        return False
    if not 0 <= level <= 100:
        return False
    with store.transaction() as conn:
        conn.execute("INSERT INTO battery_samples (device_id, at, level, charging, screen_on)"
                     " VALUES (?,?,?,?,?)",
                     (device, at, level, int(bool(data.get("charging"))), int(bool(data.get("screen_on")))))
        conn.execute("DELETE FROM battery_samples WHERE at < ?", (time.time() - KEEP_DAYS * 86400,))
    return True


@dataclass(frozen=True)
class Drain:
    idle_hours: float
    idle_pct_per_day: float | None      # None: not enough idle time to say
    unplugged_hours: float
    unplugged_pct_per_day: float | None
    samples: int


def drain(device: str | None = None, days: float = 7.0, now: float | None = None) -> Drain:
    now = now or time.time()
    args: tuple = (now - days * 86400,)
    where = "at >= ?"
    if device:
        where += " AND device_id = ?"
        args += (device,)
    rows = store.query(f"SELECT device_id, at, level, charging, screen_on FROM battery_samples"
                       f" WHERE {where} ORDER BY device_id, at", args)
    idle_s = idle_drop = plug_s = plug_drop = 0.0
    for a, b in zip(rows, rows[1:]):
        if a["device_id"] != b["device_id"]:
            continue
        span = b["at"] - a["at"]
        if span <= 0 or span > MAX_GAP_H * 3600 or b["level"] > a["level"]:
            continue
        if a["charging"] or b["charging"]:
            continue
        drop = a["level"] - b["level"]
        plug_s += span
        plug_drop += drop
        if not a["screen_on"]:
            idle_s += span
            idle_drop += drop

    def per_day(drop: float, secs: float) -> float | None:
        return round(drop / secs * 86400, 1) if secs >= MIN_IDLE_H * 3600 else None

    return Drain(round(idle_s / 3600, 1), per_day(idle_drop, idle_s),
                 round(plug_s / 3600, 1), per_day(plug_drop, plug_s), len(rows))


def spoken(d: Drain, days: float = 7.0) -> str:
    if d.samples < 2:
        return "I don't have battery readings from the phone yet; they come in as the level changes."
    span = f"Over the last {days:g} day{'s' if days != 1 else ''}"
    if d.idle_pct_per_day is None:
        return (f"{span} I've only seen {d.idle_hours:g} hours with the screen off and unplugged, "
                f"not enough to say how fast it drains idle.")
    verdict = "within" if d.idle_pct_per_day < 3 else "over"
    out = (f"{span} the phone lost about {d.idle_pct_per_day:g}% a day while idle "
           f"(screen off, unplugged, {d.idle_hours:g} hours measured), {verdict} the 3% target.")
    if d.unplugged_pct_per_day is not None:
        out += f" Unplugged overall, in use or not, it was {d.unplugged_pct_per_day:g}% a day."
    return out

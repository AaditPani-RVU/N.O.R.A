"""Voice command for the end-to-end health check (nora/health.py)."""
from __future__ import annotations

import logging

from nora import health
from nora.command_engine import register

logger = logging.getLogger("nora.commands.health")


@register(
    "health_check",
    sig="health_check()",
    description="Check every subsystem end-to-end (mic, STT, TTS, LLM, memory, Linux "
                "integrations) and whether models, Google, Spotify and the phone still "
                "work; for 'what's broken', 'diagnose the calendar'",
    category="system",
    risk="low",
)
def health_check() -> str:
    from nora import doctor
    bad = [f for f in doctor.run_checks() if not f.ok]
    if not bad:
        return health.report() + " Models, sign-ins and devices all check out."
    found = " ".join(f"{f.detail}." + (f" Fix: {f.fix}." if f.fix else "") for f in bad)
    return f"{health.report()} But: {found}"

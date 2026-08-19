"""Voice command for the end-to-end health check (nora/health.py)."""
from __future__ import annotations

import logging

from nora import health
from nora.command_engine import register

logger = logging.getLogger("nora.commands.health")


@register(
    "health_check",
    sig="health_check()",
    description="Check every subsystem end-to-end (mic, STT, TTS, LLM, memory, Linux integrations) and report anything degraded",
    category="system",
    risk="low",
)
def health_check() -> str:
    return health.report()

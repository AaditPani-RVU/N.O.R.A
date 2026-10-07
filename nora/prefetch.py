"""Keep the answers to everyday questions warm (Sharp B).

The fast path routes "what's the weather" and "what's on my calendar" in
milliseconds; the fetch behind them took 0.5–3 s. This thread refreshes them
in the background so the answer is usually already here: the local forecast
every 10 minutes, today's and tomorrow's calendar every 3.

Each refresh is the command module's own `prefetch` function, which fills the
same cache the command reads; a failed refresh just leaves the command to
fetch on demand, as before.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from nora.config import get_config

logger = logging.getLogger("nora.prefetch")


def _jobs() -> list[tuple[str, float, Callable[[], None]]]:
    from nora.commands import google_services, weather
    return [
        ("weather", 600.0, weather.prefetch),
        ("calendar", 170.0, google_services.prefetch_calendar),
    ]


_thread: threading.Thread | None = None


def start() -> None:
    global _thread
    if not (get_config().get("prefetch", {}) or {}).get("enabled", True) or _thread is not None:
        return

    def loop() -> None:
        due: dict[str, float] = {}
        while True:
            now = time.time()
            for name, every, fn in _jobs():
                if now >= due.get(name, 0):
                    try:
                        fn()
                    except Exception as e:
                        logger.debug("prefetch %s failed: %s", name, e)
                    due[name] = now + every
            time.sleep(15)

    _thread = threading.Thread(target=loop, daemon=True, name="nora-prefetch")
    _thread.start()

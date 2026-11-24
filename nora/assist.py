"""One background tick for the NORA Assist features (NORA_ASSIST_PLAN.md).

Follow-through, routines, story watch, study cards and self-repair each need
"look again every few minutes"; one thread runs them all on their own
intervals rather than five threads sleeping side by side. A job that raises is
logged and tried again on its next interval; it never stops the others.

`tell()` is how they all reach the user: to every connected phone, said on the
laptop too when the user is home (and it isn't quiet hours or a call), and the
laptop's desktop notification when nothing else took it. Same rule as the
calendar's leave-by alerts (`calendar_rules._tell`).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Callable

from nora.config import get_config

logger = logging.getLogger("nora.assist")

_speak: Callable[[str], None] | None = None
_thread: threading.Thread | None = None
_stop = threading.Event()
_TICK_SEC = 30.0


def _jobs() -> list[tuple[str, float, Callable[[datetime], object]]]:
    from nora import follow_through
    jobs: list[tuple[str, float, Callable[[datetime], object]]] = [
        ("follow-through", 300.0, follow_through.check),
    ]
    for mod, every in (("routines", 120.0), ("story_watch", 600.0), ("study", 900.0),
                       ("self_repair", 900.0)):
        try:
            module = __import__(f"nora.{mod}", fromlist=["check"])
        except ImportError:
            continue
        jobs.append((mod.replace("_", " "), every, module.check))
    return jobs


def home_place() -> str:
    return str(((get_config().get("triggers", {}) or {}).get("home", {}) or {}).get("place", "home"))


def at_home() -> bool:
    from nora import triggers
    return triggers.where(home_place()) != triggers.OUTSIDE


def tell(text: str, *, kind: str = "reminder", say_at_home: bool = False) -> bool:
    """Reach the user with `text`. True when something took it."""
    from nora import calls, delivery, focus
    logger.info("%s: %s", kind, text)
    sent = [d for d in delivery.connected() if delivery.deliver(text, device=d, kind=kind)]
    spoke = False
    if (say_at_home and _speak is not None and at_home() and not calls.quiet_now()
            and focus.current() != focus.FocusState.CALL):
        try:
            _speak(text)
            spoke = True
        except Exception as e:
            logger.debug("saying %s failed: %s", kind, e)
    if not sent and not spoke:
        calls.notify_laptop("NORA", text)
    return True


def tick(now: datetime | None = None, _due: dict[str, float] | None = None) -> None:
    due = _due if _due is not None else {}
    stamp = time.time()
    for name, every, fn in _jobs():
        if stamp < due.get(name, 0):
            continue
        due[name] = stamp + every
        try:
            fn(now or datetime.now())
        except Exception as e:
            logger.warning("assist %s failed: %s", name, e)


def start(speak: Callable[[str], None] | None = None) -> None:
    global _thread, _speak
    _speak = speak
    if _thread is not None or not (get_config().get("assist", {}) or {}).get("enabled", True):
        return
    due: dict[str, float] = {}

    def loop() -> None:
        # A minute's grace so startup isn't also a burst of checks.
        if _stop.wait(60):
            return
        while True:
            tick(_due=due)
            if _stop.wait(_TICK_SEC):
                return

    _stop.clear()
    _thread = threading.Thread(target=loop, daemon=True, name="nora-assist")
    _thread.start()


def stop() -> None:
    global _thread
    _stop.set()
    _thread = None

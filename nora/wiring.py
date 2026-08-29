"""Composition root — the one place NORA's moving parts get assembled.

Everything in `pipeline.run()` used to be assembled inline: the subsystem
`start()` calls, the audio and LLM edges, the timeouts. That made the
orchestration loop untestable, because reaching a single line of it meant
booting a microphone, a Whisper model, a Chroma index, and a WebSocket server.

So the wiring lives here instead. `build()` returns the callables the turn
handler reaches for — TTS, the intent parser, the conversation engine, the
confirmation prompt, the ReAct planner — with the real implementations as
defaults. A test overrides the interactive edges and leaves the rest alone,
which is the point: the fast path, both guards, the security check, the
autonomy ladder and the command dispatcher all stay real.

`start_subsystems()` / `stop_subsystems()` hold the background threads. The
turn handler never touches them, so tests never start them.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from nora.config import get_config
from nora.schemas import IntentResponse

logger = logging.getLogger("nora.wiring")


@dataclass
class TurnDeps:
    """The edges of a single turn: audio in, speech out, LLM, confirmation.

    Everything the turn handler does *not* take from here — fast path,
    NeuroSym, security, risk/autonomy/confidence, command dispatch, memory —
    it calls directly, and therefore runs for real under test.
    """

    speak: Callable[..., None]
    parse_intent: Callable[..., IntentResponse]
    respond: Callable[..., str]
    confirm: Callable[[], Awaitable[bool]]
    run_plan: Callable[..., Awaitable[list]]
    frustration: Any
    listener: Any = None
    llm_timeout: float = 20.0
    transcribe_timeout: float = 30.0


def build(
    listener: Any = None,
    frustration: Any = None,
    *,
    speak: Callable[..., None] | None = None,
    parse_intent: Callable[..., IntentResponse] | None = None,
    respond: Callable[..., str] | None = None,
    confirm: Callable[[], Awaitable[bool]] | None = None,
    run_plan: Callable[..., Awaitable[list]] | None = None,
) -> TurnDeps:
    """Assemble a `TurnDeps`, defaulting every edge to the real implementation.

    Imports are deferred to call time so that overriding an edge does not drag
    in the module it replaces — a test that supplies its own `speak` should not
    pay for pygame.
    """
    from nora import conversation, intent_parser, speaker
    from nora.frustration import FrustrationTracker

    if frustration is None:
        frustration = FrustrationTracker()

    async def _default_confirm() -> bool:
        from nora import pipeline
        return await pipeline.confirmation_flow(listener)

    async def _default_run_plan(text: str, mem_ctx: dict) -> list:
        from nora import planner
        return await planner.run_plan(text, mem_ctx, listener)

    timeouts = get_config().get("timeouts", {})
    return TurnDeps(
        speak=speak or speaker.speak,
        parse_intent=parse_intent or intent_parser.parse_intent,
        respond=respond or conversation.respond,
        confirm=confirm or _default_confirm,
        run_plan=run_plan or _default_run_plan,
        frustration=frustration,
        listener=listener,
        llm_timeout=float(timeouts.get("llm_sec", 20)),
        transcribe_timeout=float(timeouts.get("transcribe_sec", 30)),
    )


def start_subsystems(speak: Callable[..., None]) -> None:
    """Start every background thread NORA runs alongside the command loop.

    Each start is a no-op when its feature is disabled in config, so this stays
    a flat list rather than a tree of conditionals. Order matters only for the
    two that register callbacks (`proactive`, and the Linux hooks) — those need
    their targets importable, which they are by the time this is called.
    """
    from nora import (
        ack as _ack, ambient, anomaly_watchdog, cognitive_memory, consolidation,
        focus, jobs, proactive, scheduler, terminal_monitor, text_input, vision,
    )
    from nora import wakeword as _ww

    ambient.start()
    vision.start()
    cognitive_memory.warm_up()

    # Proactive speech is gated by the focus/attention model
    # (CODEX_INTEGRATION.md 5.5).
    proactive.register_callback(focus.gated(speak))
    proactive.start()

    # Background answers and scheduled runs speak through the same focus-gated
    # channel as proactive suggestions: an answer that lands mid-meeting is
    # held and flushed on the next interaction rather than barging in. Both
    # ride `nora.jobs`, so the queue starts first.
    _gated = focus.gated(speak)
    jobs.start(speak_callback=_gated)
    scheduler.start()

    text_input.start()

    # Pre-synthesize ack tokens in background. Rate is intentionally left to
    # ack.py's own (slower) default — inheriting the speaker's boosted rate is
    # what made acks sound clipped and unnatural.
    cfg_spk = get_config().get("speaker", {})
    threading.Thread(
        target=_ack.preload,
        kwargs={"voice": cfg_spk.get("voice", "en-GB-SoniaNeural")},
        daemon=True,
        name="nora-ack-preload",
    ).start()

    _ww.start()
    consolidation.start()
    terminal_monitor.start(speak_callback=speak)
    anomaly_watchdog.start(speak_callback=speak)

    # Pull the lazy model loads off the first spoken turn and onto startup.
    from nora import pipeline
    threading.Thread(target=pipeline._warm_lazy_singletons, daemon=True,
                     name="nora-warmup").start()

    _start_remote_mic()
    _start_telegram()
    _start_mcp()
    _start_linux_hooks()
    _start_websocket_api()


def stop_subsystems() -> None:
    """Shut the background threads down. Safe to call twice."""
    from nora import (
        ambient, anomaly_watchdog, consolidation, jobs, proactive, scheduler,
        terminal_monitor, vision,
    )
    from nora.gateway import telegram

    for label, stop in (
        ("ambient", ambient.stop), ("vision", vision.stop),
        ("proactive", proactive.stop), ("consolidation", consolidation.stop),
        ("terminal_monitor", terminal_monitor.stop),
        ("anomaly_watchdog", anomaly_watchdog.stop),
        ("scheduler", scheduler.stop), ("jobs", jobs.stop),
        ("telegram", telegram.stop),
    ):
        try:
            stop()
        except Exception as e:
            logger.debug("%s.stop() failed: %s", label, e)


def _start_remote_mic() -> None:
    """Remote mic server — accepts audio from e.g. a MacBook."""
    cfg = get_config().get("remote_mic", {})
    if not cfg.get("enabled", False):
        return
    from nora import remote_mic
    host = cfg.get("host", "0.0.0.0")
    port = int(cfg.get("port", 8767))
    remote_mic.start(host=host, port=port)
    print(f"[NORA] Remote mic server listening on {host}:{port}")


def _start_telegram() -> None:
    """Telegram gateway — NORA reachable when you are not in the room."""
    from nora.gateway import telegram
    if telegram.start():
        print("[NORA] Telegram gateway active")


def _start_mcp() -> None:
    """Load MCP server tools (Sprint 5 — Ecosystem Expansion)."""
    from nora import mcp_bridge
    mcp_bridge.load_all()


def _start_linux_hooks() -> None:
    """Linux flagship modules (F3/F4/F5) — no-op off Linux or without deps."""
    import sys
    if not sys.platform.startswith("linux"):
        return
    for label, mod_name, hook in (
        ("why_engine", "why_engine", "register_with_watchdog"),
        ("time_travel", "time_travel", "register_with_reversible"),
        ("ambient_linux", "ambient_linux", "register_with_wakeword"),
    ):
        try:
            mod = __import__(f"nora.commands.{mod_name}", fromlist=[mod_name])
            getattr(mod, hook)()
        except Exception as e:
            logger.debug("%s hook skipped: %s", label, e)


def _start_websocket_api() -> None:
    """Authenticated WebSocket API (Sprint 5)."""
    cfg = get_config().get("websocket_api", {})
    if not cfg.get("enabled", True):
        return
    import os
    from nora import ui_server
    port = int(cfg.get("port", 8765))
    token = os.environ.get("NORA_API_TOKEN", "")
    url = ui_server.start_ws(port=port, token=token)
    if url:
        auth_note = " (token auth enabled)" if token else " (no auth — localhost only)"
        print(f"[NORA] WebSocket API: {url}{auth_note}")

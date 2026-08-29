from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import numpy as np

from nora import ambient, audit_log, autonomy, cognitive_memory, command_engine, confidence, consent_memory, context, conversation, dialogue, focus, intent_parser, memory, neurosym_guard, phrasing, post_action_cards, proactive, reversible, risk, security, session_briefing, speaker, text_input, tool_trust, transcriber
from nora import ack as _ack
from nora import wiring
from nora.config import get_config
from nora.commands.greetings import daddys_home
from nora.commands.music import iron_man_entrance
from nora.listener import Listener
from nora.schemas import IntentResponse, StepResult
from nora.wiring import TurnDeps

logger = logging.getLogger("nora.pipeline")

WAKE_PHRASES = [
    "daddy's home", "daddys home", "daddy is home", "daddy's home",
    "wake up daddy's home", "wake up daddys home", "wake up daddy is home",
    "wake up, daddy's home", "wake up",
]

STOP_PHRASES = ("stop", "cancel", "cancel that", "pause everything", "shut up", "quiet")


def _warm_lazy_singletons() -> None:
    """Load the models that would otherwise load during the first spoken turn.

    Three subsystems build their heavy objects on first use: ChromaDB's
    embedding model (measured at 12.4s cold against 22ms warm), the Whisper
    model, and Kokoro's ONNX session. Left alone, the whole bill lands on the
    user's opening sentence, which is the worst possible turn to spend twelve
    seconds on.

    Running preflight beforehand does not help here — it is a separate
    process, so it warms the OS page cache but not this one's memory. Hence
    doing it again, in-process, on a daemon thread so startup is not blocked.
    """
    import time as _t

    def _warm(label: str, fn) -> None:
        started = _t.monotonic()
        try:
            fn()
            logger.info("warm-up: %s ready in %.0fms", label, (_t.monotonic() - started) * 1000)
        except Exception as e:
            logger.warning("warm-up: %s failed (%s) — first use will pay the load", label, e)

    _warm("cognitive memory", lambda: cognitive_memory.get_context_for_prompt("warm up", n=1))
    _warm("whisper", lambda: transcriber.transcribe(np.zeros(16000, dtype=np.float32)))

    def _warm_tts() -> None:
        import tempfile
        from pathlib import Path as _P
        from nora import tts_local
        cfg = get_config().get("speaker", {})
        if not tts_local.enabled():
            return
        tts_local.synth_if_enabled(
            "Ready.", cfg.get("rate", "+20%"),
            str(_P(tempfile.gettempdir()) / "nora_warmup.wav"),
        )

    _warm("kokoro", _warm_tts)


def summarize_results(results: list[StepResult]) -> str:
    """Create a spoken summary of execution results."""
    if not results:
        return "No actions were taken."
    messages = []
    for r in results:
        if r.success or r.withheld:
            # A withheld step didn't fail — NORA chose not to run it (guest
            # mode). Speaking "Failed:" over a deliberate refusal misreports it.
            if r.message:
                messages.append(r.message)
        else:
            messages.append(f"Failed: {r.message or 'unknown error'}")
    return ". ".join(messages) if messages else "Done."


def is_wake_phrase(text: str) -> bool:
    """Check if transcribed text matches the wake phrase."""
    text_lower = text.lower().strip().rstrip(".")
    return any(phrase in text_lower for phrase in WAKE_PHRASES)


async def confirmation_flow(listener: Listener) -> bool:
    """Ask for voice confirmation and return True if user says yes."""
    speaker.speak("Should I proceed? Say yes or no.")
    audio = await listener.listen()
    if audio is None:
        return False
    text = transcriber.transcribe(audio).lower()
    return any(word in text for word in ["yes", "yeah", "yep", "sure", "go ahead", "confirm", "do it"])

@dataclass
class TurnOutcome:
    """What one utterance turned into.

    The command loop used to express all of this as control flow — sixteen
    `continue`s and a bare `return`, each one a decision no caller could see.
    Naming the outcomes is what makes the loop assertable: a test can say "this
    utterance took the fast path and dispatched play_music" without inspecting
    what was spoken, which is the brittle part.

    `kind` is one of:

    ``ignored``      too short, or nothing to act on
    ``blocked``      refused — see `stage` for which guard said no
    ``interrupted``  a stop phrase cut the current action short
    ``exit``         shutdown requested; the caller stops the loop
    ``chat``         answered conversationally, no actions dispatched
    ``executed``     actions ran — `results` holds their StepResults
    ``cancelled``    planned, then not run (declined, or cancelled mid-flight)
    ``error``        the intent could not be parsed
    """

    kind: str
    text: str = ""
    intent: str = ""
    actions: list[str] = field(default_factory=list)
    results: list[StepResult] = field(default_factory=list)
    reply: str = ""
    message: str = ""
    stage: str = ""
    source: str = ""
    violations: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the turn did what was asked, whatever route it took."""
        if self.kind in ("blocked", "error", "cancelled"):
            return False
        return all(r.success for r in self.results) if self.results else True


def _actions(intent: IntentResponse) -> list[str]:
    return [s.action for s in intent.steps]


async def handle_turn(text: str, deps: TurnDeps, rms: float = 0.0) -> TurnOutcome:
    """Take one utterance from heard text to finished action.

    This is the whole of NORA's decision-making for a single turn: guards, the
    fast path, conversational routing, intent parsing, the autonomy ladder,
    confirmation, execution, and every memory write that follows. It is
    deliberately free of the audio and process concerns around it — `deps`
    carries the four edges it cannot run without (speech, the intent parser,
    the conversation engine, the confirmation prompt), and everything else it
    calls for real.

    Raises nothing it can help; the caller's loop catches what escapes.
    """
    loop = asyncio.get_running_loop()
    from nora import ui_server
    mem_ctx: dict = {}  # ensure always bound before LLM branch

    if not text or len(text.strip()) < 2:
        return TurnOutcome(kind="ignored", text=text)

    # Mirror the utterance to the dashboard transcript *before* the guard
    # runs -- a blocked input should still show what was said, next to the
    # refusal, rather than disappearing.
    ui_server.notify_user(text)

    # NeuroSym: block adversarial voice commands before they reach the LLM
    ui_server.notify_stage("guarding")
    input_safe, input_violations = neurosym_guard.check_input(text)
    if not input_safe:
        severity = input_violations[0].get("severity", "unknown") if input_violations else "unknown"
        logger.warning(f"NeuroSym blocked input [{severity}]: {text[:80]}")
        deps.speak(phrasing.get("blocked"), mood="error")
        cognitive_memory.record_knowledge(text, source="blocked_input")
        ui_server.notify_stage("idle")
        return TurnOutcome(kind="blocked", text=text, stage="input_guard", violations=input_violations)

    logger.info(f"Heard: {text}")
    print(f"[NORA] Heard: {text}")
    text_lower = text.lower().strip().rstrip(".,!?")

    # Every heard utterance joins the transcript, command or not — a
    # follow-up two turns later may well reference a command.
    dialogue.record_user(text)

    # Log every utterance to the knowledge base
    ambient.log_entry(text, source="command")
    cognitive_memory.record_knowledge(text, source="command")
    proactive.notify_command_issued()

    # â"€â"€ Fast-path interrupts â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    if any(p == text_lower or text_lower.startswith(p + " ") for p in STOP_PHRASES):
        from nora.commands.interrupt import stop_all
        stop_all()
        deps.frustration.record(text_lower, rms=rms, success=True)
        return TurnOutcome(kind="interrupted", text=text)

    exit_words = ["exit", "quit", "goodbye", "good bye", "shut down nora", "stop nora", "go to sleep"]
    if text_lower in exit_words or any(text_lower.startswith(w + " ") and len(text_lower.split()) <= 4 for w in exit_words):
        deps.speak(phrasing.get("goodbye"))
        logger.info("Exit command received. Shutting down.")
        return TurnOutcome(kind="exit", text=text)

    if is_wake_phrase(text_lower):
        deps.speak(phrasing.get("already_awake"), mood="chat")
        return TurnOutcome(kind="chat", text=text, intent="already_awake")

    # 3a. Fast-path: deterministic resolution before the LLM is ever touched.
    # Handles ~40-50% of real commands (music, volume, time, open/close, etc.)
    # in <50ms with zero network calls.
    from nora import fast_path as _fp
    _fast_intent = _fp.resolve(text)
    if _fast_intent is not None and not _fast_intent.steps and _fast_intent.response:
        # Pure conversational shortcut — speak and loop immediately
        ui_server.notify_stage("speaking")
        deps.speak(_fast_intent.response, mood="chat")
        ui_server.notify_stage("idle")
        context.add_session_turn(
            text=text, intent="chat", actions=[], result_summary="",
            success=True, reply=_fast_intent.response,
        )
        deps.frustration.record(text_lower, rms=rms, success=True)
        return TurnOutcome(kind="chat", text=text, intent="chat", reply=_fast_intent.response, source="fast_path")

    if _fast_intent is not None and _fast_intent.steps:
        logger.info(f"Fast-path hit: {_fast_intent.intent}")
        print(f"[NORA] Fast-path: {_fast_intent.intent}")
        intent = _fast_intent
        has_blocked, needs_confirm = security.check_steps(intent.steps)
        if has_blocked:
            deps.speak(phrasing.get("blocked"), mood="error")
            deps.frustration.record(text_lower, rms=rms, success=False)
            ui_server.notify_stage("idle")
            return TurnOutcome(kind="blocked", text=text, stage="security", intent=intent.intent, actions=_actions(intent))
        if needs_confirm:
            intent.requires_confirmation = True
    else:
        # 3a'. Conversational routing — decide "talk" vs "do" before the
        # action planner is consulted. Chat used to be extracted from the
        # JSON execution prompt, which is why it sounded like a form
        # letter; conversational acts now go to a path built for speech.
        # The classifier resolves ambiguity toward COMMAND, so anything
        # that might be an instruction still takes the action path.
        _act = dialogue.classify(text)
        if conversation.should_handle(_act):
            logger.info(f"Conversational act: {_act.value}")
            print(f"[NORA] Conversation ({_act.value})")
            ui_server.notify_stage("thinking")

            _chat_ctx = memory.get_context_summary()
            _chat_ctx["relevant_context"] = cognitive_memory.get_context_for_prompt(
                text, n=2
            ).get("relevant_context", [])

            try:
                reply = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, deps.respond, text, _chat_ctx, _act
                    ),
                    timeout=deps.llm_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("Conversation reply timed out")
                reply = phrasing.get("too_slow")
            except Exception as exc:
                logger.warning(f"Conversation reply failed: {exc}")
                reply = phrasing.get("recovered")

            ui_server.notify_stage("speaking")
            deps.speak(reply, mood="chat")
            ui_server.notify_stage("idle")

            # Chat turns reach the session buffer too. They never did
            # before, which is why NORA could not follow its own thread.
            context.add_session_turn(
                text=text, intent=f"chat:{_act.value}", actions=[],
                result_summary="", success=True, reply=reply,
            )
            context.record_command(text=text, intent="chat", actions=[])
            focus.note_activity()
            deps.frustration.record(text_lower, rms=rms, success=True)
            return TurnOutcome(kind="chat", text=text, intent=f"chat:{_act.value}", reply=reply, source="conversation")

        # 3b. No fast-path match — build enriched context and call the LLM
        mem_ctx = memory.get_context_summary()
        mem_ctx["recent_commands"] = context.recent_commands()[:3]
        cog_ctx = cognitive_memory.get_context_for_prompt(text, n=2)
        mem_ctx["typical_actions_now"] = cog_ctx.get("typical_actions_now", [])
        mem_ctx["relevant_context"] = cog_ctx.get("relevant_context", [])
        mem_ctx["session_turns"] = [t.to_dict() for t in context.get_session_turns(5)]

        # Multimodal context fusion — pre-attach screen snippet for deictic commands
        screen_ctx: dict | None = None
        if intent_parser.needs_screen_context(text):
            try:
                from nora.commands.screen_intelligence import get_screen_snippet
                snippet, win_title = get_screen_snippet()
                if snippet or win_title:
                    screen_ctx = {"snippet": snippet, "window_title": win_title}
            except Exception:
                pass

        print(f"[NORA] Parsing intent...")
        ui_server.notify_stage("thinking")
        try:
            intent = await asyncio.wait_for(
                loop.run_in_executor(
                    None, deps.parse_intent, text, mem_ctx, screen_ctx
                ),
                timeout=deps.llm_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("Intent parsing timed out")
            print("[NORA] Intent parsing timed out")
            deps.speak(phrasing.get("too_slow"), mood="error")
            deps.frustration.record(text_lower, rms=rms, success=False)
            ui_server.notify_stage("idle")
            return TurnOutcome(kind="error", text=text, stage="intent_timeout")
        except Exception as e:
            logger.warning(f"Intent parsing failed: {e}")
            print(f"[NORA] Intent parsing error: {e}")
            deps.speak(phrasing.get("not_understood"), mood="error")
            deps.frustration.record(text_lower, rms=rms, success=False)
            ui_server.notify_stage("idle")
            return TurnOutcome(kind="error", text=text, stage="intent_failed")

        if intent.error:
            print(f"[NORA] LLM returned error: {intent.error}")
            _clarify_words = (
                "context", "specific", "clarif", "which", "what do you mean",
                "more information", "more detail", "please provide",
            )
            if any(w in intent.error.lower() for w in _clarify_words):
                deps.speak(phrasing.get("not_understood"), mood="error")
            else:
                deps.speak(f"{phrasing.get('error')} {intent.error}", mood="error")
            deps.frustration.record(text_lower, rms=rms, success=False)
            ui_server.notify_stage("idle")
            return TurnOutcome(kind="error", text=text, stage="intent_error", message=intent.error or "")

        # NeuroSym: validate action plan before execution
        ui_server.notify_stage("guarding")
        plan_safe, plan_needs_confirm, plan_violations = neurosym_guard.check_intent(intent)
        if not plan_safe:
            deps.speak(phrasing.get("blocked"), mood="error")
            deps.frustration.record(text_lower, rms=rms, success=False)
            ui_server.notify_stage("idle")
            return TurnOutcome(kind="blocked", text=text, stage="plan_guard", intent=intent.intent, actions=_actions(intent), violations=plan_violations)
        if plan_needs_confirm:
            intent.requires_confirmation = True

        # Config-based block list
        has_blocked, needs_confirm = security.check_steps(intent.steps)
        if has_blocked:
            deps.speak(phrasing.get("blocked"), mood="error")
            deps.frustration.record(text_lower, rms=rms, success=False)
            ui_server.notify_stage("idle")
            return TurnOutcome(kind="blocked", text=text, stage="security", intent=intent.intent, actions=_actions(intent))
        if needs_confirm:
            intent.requires_confirmation = True

        # Conversational response — no actionable steps.
        # The planner deciding there is nothing to execute *is* the
        # signal that this turn is conversation. Rather than speak the
        # `response` slot it squeezed out of a JSON schema, hand the
        # turn to the conversation engine and let it answer properly.
        if not intent.steps and not intent.error:
            _raw_reply = (intent.response or "").strip()
            if conversation.should_handle(dialogue.Act.UNKNOWN):
                ui_server.notify_stage("thinking")
                try:
                    reply = await asyncio.wait_for(
                        loop.run_in_executor(
                            None, deps.respond, text, mem_ctx, dialogue.Act.UNKNOWN
                        ),
                        timeout=deps.llm_timeout,
                    )
                except Exception as exc:
                    logger.warning(f"Conversation fallback failed: {exc}")
                    reply = conversation.for_speech(_raw_reply) or phrasing.get("need_specifics")
            else:
                _clarify_words = (
                    "more context", "more information", "more detail",
                    "be more specific", "what do you mean", "please specify",
                    "which one", "i'm not sure what", "i don't understand",
                    "can you tell me more",
                )
                if not _raw_reply or any(w in _raw_reply.lower() for w in _clarify_words):
                    reply = phrasing.get("need_specifics")
                else:
                    reply = conversation.for_speech(_raw_reply)

            ui_server.notify_stage("speaking")
            deps.speak(reply, mood="chat")
            ui_server.notify_stage("idle")
            context.add_session_turn(
                text=text, intent="chat", actions=[], result_summary="",
                success=True, reply=reply,
            )
            deps.frustration.record(text_lower, rms=rms, success=bool(reply))
            return TurnOutcome(kind="chat", text=text, intent="chat", reply=reply, source="planner_fallback")

    # Tag intent with user text for audit log
    object.__setattr__(intent, "_user_text", text) if hasattr(intent, "__fields__") else None
    try:
        intent._user_text = text
    except Exception:
        pass

    actions_str = " → ".join(s.action for s in intent.steps)
    print(f"[NORA] {intent.intent}  [{actions_str}]")

    # Record to recent-commands
    context.record_command(
        text=text,
        intent=intent.intent,
        actions=[s.action for s in intent.steps],
    )
    focus.note_activity()

    # 4a. Autonomous task routing — ReAct planner for complex goals
    if intent_parser.is_autonomous_task(text) and len(intent.steps) == 0:
        # No steps planned yet: route to ReAct planner
        ui_server.notify_stage("acting")
        results = await deps.run_plan(text, mem_ctx)
        context.wake_triggered = False
        summary = summarize_results(results)
        if summary:
            ui_server.notify_stage("speaking")
            deps.speak(summary, mood="info" if all(r.success for r in results) else "error")
        ui_server.notify_stage("idle")
        return TurnOutcome(kind="executed", text=text, intent=intent.intent, actions=_actions(intent), results=results, reply=summary, source="react_planner")

    # 4b. Confirmation only for genuinely destructive actions — not just multi-step.
    # Being asked to confirm "open chrome then search google" breaks flow.
    _destructive = {"delete_file", "shutdown", "close_all_apps", "patch_file",
                    "git_smart_commit", "move_file"}
    _has_destructive = any(s.action in _destructive for s in intent.steps)

    if _has_destructive and not intent.requires_confirmation:
        intent.requires_confirmation = True

    # 4b'. Codex autonomy layer — risk aggregation + tier classification
    # (CODEX_INTEGRATION.md 2.1/2.2). Can escalate confirmation on its own;
    # can drop it only via explicit config opt-in for proven, safe classes.
    action_risk = risk.assess(intent)
    tier = autonomy.classify(intent, action_risk)
    if tier in (autonomy.AutonomyTier.NEEDS_CONSENT, autonomy.AutonomyTier.SUGGEST):
        intent.requires_confirmation = True
    elif intent.requires_confirmation and not _has_destructive and autonomy.may_skip_confirmation(intent):
        intent.requires_confirmation = False

    # 4b''. Verification loop for low-confidence intents (CODEX_INTEGRATION.md
    # 2.6/5.11) — a plausible-but-wrong guess gets a confirm, not a silent run.
    intent_confidence = confidence.estimate(intent, text)
    if intent.steps and confidence.needs_clarification(intent_confidence):
        intent.requires_confirmation = True

    # 4c. Confirmation prompt — kept brief so it doesn't feel like bureaucracy
    if intent.requires_confirmation:
        step_labels = " → ".join(s.action.replace("_", " ") for s in intent.steps[:6])
        deps.speak(f"{step_labels}. Confirm?", mood="confirmation")
        confirmed = await deps.confirm()
        # Consent memory learns from every prompt (CODEX_INTEGRATION.md 5.2)
        consent_memory.record([s.action for s in intent.steps], confirmed)
        if not confirmed:
            deps.speak(phrasing.get("cancelled"))
            ui_server.notify_stage("idle")
            return TurnOutcome(kind="cancelled", text=text, stage="declined", intent=intent.intent, actions=_actions(intent))

    # 5. Execute
    ui_server.notify_stage("acting")
    results = await command_engine.execute(intent)
    context.wake_triggered = False

    # Tool trust ledger scores every invocation (CODEX_INTEGRATION.md 2.4)
    for r in results:
        if not r.withheld:      # a policy refusal isn't the tool's fault
            tool_trust.record(r.action, r.success)

    # Post-action explanation card — "why did you do that" (CODEX_INTEGRATION.md 5.3)
    post_action_cards.build(text, intent, results, intent_confidence)

    if context.is_cancelled():
        logger.info("Cancellation observed after execute -- dropping response.")
        context.clear_cancel()
        return TurnOutcome(kind="cancelled", text=text, stage="interrupted", intent=intent.intent, actions=_actions(intent))

    # 6. Respond
    summary = summarize_results(results)
    all_ok_early = all(r.success for r in results) if results else True
    if summary:
        ui_server.notify_stage("speaking")
        deps.speak(summary, mood="info" if all_ok_early else "error")
    ui_server.notify_stage("idle")

    # Record turn to session context buffer
    context.add_session_turn(
        text=text,
        intent=intent.intent,
        actions=[s.action for s in intent.steps],
        result_summary=summary,
        success=all(r.success for r in results) if results else True,
    )

    # Record actions to both legacy memory and cognitive memory
    executed_actions = [s.action for s in intent.steps]
    for action in executed_actions:
        memory.record_action(action)
    if len(executed_actions) >= 2:
        memory.record_workflow(text, executed_actions)

    # Record full episode to cognitive memory (semantic + episodic)
    outcomes = [{"action": r.action, "success": r.success, "message": r.message}
                for r in results]
    active_apps = context.active_apps()
    cognitive_memory.record_episode(
        text=text,
        intent=intent.intent,
        actions=executed_actions,
        outcomes=outcomes,
        active_apps=active_apps,
    )

    # Workflow prediction — log only; speaking mid-flow breaks momentum
    if executed_actions:
        last_action = executed_actions[-1]
        predicted = cognitive_memory.predict_next_action(last_action, min_confidence=3)
        if not predicted:
            predicted = memory.predict_next_action(last_action)
        if predicted:
            logger.info(f"Workflow prediction: {last_action} → {predicted}")

    # Check for frustration -- offer help if detected (Feature 5)
    all_ok = all(r.success for r in results) if results else True
    if deps.frustration.record(text_lower, rms=rms, success=all_ok):
        logger.info("Frustration detected -- offering proactive help")
        deps.speak("You seem stuck. Want me to ask Claude for help?", mood="proactive")


    return TurnOutcome(
        kind="executed", text=text, intent=intent.intent, actions=executed_actions,
        results=results, reply=summary,
    )


async def _next_utterance(listener: Listener, deps: TurnDeps) -> tuple[str, float] | None:
    """Block until there is something to act on. None means "nothing usable".

    Two sources feed the loop: text typed into the dashboard, which is checked
    first and costs nothing, and the microphone. Only the audio path can fail
    here — too short to be speech, or too slow to transcribe.
    """
    from nora import ui_server

    text = text_input.get_pending()
    if text:
        logger.info(f"Text input: {text}")
        print(f"[NORA] Text from UI: {text}", flush=True)
        return text, 0.0

    ui_server.notify_stage("listening")
    audio = await listener.listen()
    if audio is None or len(audio) < 1600:
        ui_server.notify_stage("idle")
        return None

    rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))

    # Play ack immediately in PTT mode (wakeword mode acks inside listen_wakeword)
    if context.get_ptt_enabled():
        _ack.speak_ack()

    ui_server.notify_stage("transcribing")
    loop = asyncio.get_running_loop()
    try:
        text = await asyncio.wait_for(
            loop.run_in_executor(None, transcriber.transcribe, audio),
            timeout=deps.transcribe_timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("Transcription timed out -- discarding audio")
        deps.speak(phrasing.get("too_slow"), mood="error")
        ui_server.notify_stage("idle")
        return None

    return text, rms


async def run() -> None:
    """Main NORA pipeline loop: assemble, boot, then one turn at a time."""
    command_engine.discover_commands()
    listener = Listener()

    if not intent_parser.check_ollama_connection():
        logger.error("LLM backend not reachable. Check config.yaml â†' llm.provider.")
        print("[NORA] ERROR: LLM backend not reachable. Check your config.")
        return

    actions = command_engine.get_available_actions()
    logger.info(f"Loaded {len(actions)} actions: {', '.join(actions)}")

    deps = wiring.build(listener)
    wiring.start_subsystems(deps.speak)

    # â"€â"€ Activate immediately â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    context.wake_triggered = True
    iron_man_entrance()
    greeting = daddys_home()
    deps.speak(greeting)

    # Session replay briefing — only on first wake of a new session
    briefing = session_briefing.get_briefing()
    if briefing:
        deps.speak(briefing, mood="info")

    print(f"[NORA] {greeting}")
    print(f"[NORA] Ready. Hold [{listener.hotkey}] to speak. Say 'exit' to shut down.")

    # â"€â"€ Command loop â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    while True:
        try:
            context.clear_cancel()

            heard = await _next_utterance(listener, deps)
            if heard is None:
                continue

            outcome = await handle_turn(heard[0], deps, rms=heard[1])
            if outcome.kind == "exit":
                wiring.stop_subsystems()
                return

        except KeyboardInterrupt:
            deps.speak("Shutting down.")
            logger.info("Keyboard interrupt. Exiting.")
            wiring.stop_subsystems()
            break
        except Exception as e:
            logger.error(f"Pipeline error: {e}", exc_info=True)
            deps.speak(phrasing.get("recovered"), mood="error")

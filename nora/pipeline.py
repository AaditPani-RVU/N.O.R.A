from __future__ import annotations

import asyncio
import logging
import threading

import numpy as np

from nora import ambient, anomaly_watchdog, audit_log, autonomy, cognitive_memory, command_engine, confidence, consent_memory, consolidation, context, conversation, dialogue, focus, intent_parser, memory, neurosym_guard, phrasing, post_action_cards, proactive, reversible, risk, security, session_briefing, speaker, terminal_monitor, text_input, tool_trust, transcriber, vision
from nora import ack as _ack
from nora import wakeword as _ww
from nora.config import get_config
from nora.commands.greetings import daddys_home
from nora.commands.music import iron_man_entrance
from nora.frustration import FrustrationTracker
from nora.listener import Listener
from nora.schemas import StepResult

logger = logging.getLogger("nora.pipeline")

WAKE_PHRASES = [
    "daddy's home", "daddys home", "daddy is home", "daddy's home",
    "wake up daddy's home", "wake up daddys home", "wake up daddy is home",
    "wake up, daddy's home", "wake up",
]

STOP_PHRASES = ("stop", "cancel", "cancel that", "pause everything", "shut up", "quiet")


def summarize_results(results: list[StepResult]) -> str:
    """Create a spoken summary of execution results."""
    if not results:
        return "No actions were taken."
    messages = []
    for r in results:
        if r.success:
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


async def run() -> None:
    """Main NORA pipeline loop."""
    command_engine.discover_commands()
    listener = Listener()
    frustration = FrustrationTracker()

    if not intent_parser.check_ollama_connection():
        logger.error("LLM backend not reachable. Check config.yaml â†' llm.provider.")
        print("[NORA] ERROR: LLM backend not reachable. Check your config.")
        return

    actions = command_engine.get_available_actions()
    logger.info(f"Loaded {len(actions)} actions: {', '.join(actions)}")

    # Start ambient transcription (no-op if disabled in config)
    ambient.start()

    # Start camera perception (no-op if vision.enabled is false)
    vision.start()

    # Warm up cognitive memory (ChromaDB + embedder) in background
    cognitive_memory.warm_up()

    # Start proactive intelligence engine
    # Proactive speech is gated by the focus/attention model (CODEX_INTEGRATION.md 5.5)
    proactive.register_callback(focus.gated(speaker.speak))
    proactive.start()

    # Start keyboard text input fallback
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

    # Start always-on wakeword detector (no-op if disabled in config)
    _ww.start()

    # Start nightly consolidation scheduler
    consolidation.start()

    # Start terminal co-pilot clipboard watcher
    terminal_monitor.start(speak_callback=speaker.speak)

    # Start anomaly watchdog (Sprint 4 #24 — background metrics alerts)
    anomaly_watchdog.start(speak_callback=speaker.speak)

    # Start remote mic server (accepts audio from e.g. a MacBook)
    _remote_cfg = get_config().get("remote_mic", {})
    if _remote_cfg.get("enabled", False):
        from nora import remote_mic as _rmic
        _rm_host = _remote_cfg.get("host", "0.0.0.0")
        _rm_port = int(_remote_cfg.get("port", 8767))
        _rmic.start(host=_rm_host, port=_rm_port)
        print(f"[NORA] Remote mic server listening on {_rm_host}:{_rm_port}")

    # Load MCP server tools (Sprint 5 — Ecosystem Expansion)
    from nora import mcp_bridge as _mcp
    _mcp.load_all()

    # Linux flagship modules (F3/F4/F5 hooks) — no-op on non-Linux or missing deps
    import sys as _sys
    if _sys.platform.startswith("linux"):
        try:
            from nora.commands import why_engine as _we
            _we.register_with_watchdog()
        except Exception as _e:
            logger.debug("why_engine hook skipped: %s", _e)
        try:
            from nora.commands import time_travel as _tt
            _tt.register_with_reversible()
        except Exception as _e:
            logger.debug("time_travel hook skipped: %s", _e)
        try:
            from nora.commands import ambient_linux as _al
            _al.register_with_wakeword()
        except Exception as _e:
            logger.debug("ambient_linux hook skipped: %s", _e)

    # Start authenticated WebSocket API (Sprint 5)
    _ws_cfg = get_config().get("websocket_api", {})
    if _ws_cfg.get("enabled", True):
        import os
        from nora import ui_server as _ui
        _ws_port = int(_ws_cfg.get("port", 8765))
        _ws_token = os.environ.get("NORA_API_TOKEN", "")
        _ws_url = _ui.start_ws(port=_ws_port, token=_ws_token)
        if _ws_url:
            auth_note = " (token auth enabled)" if _ws_token else " (no auth — localhost only)"
            print(f"[NORA] WebSocket API: {_ws_url}{auth_note}")

    # â"€â"€ Activate immediately â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    context.wake_triggered = True
    iron_man_entrance()
    greeting = daddys_home()
    speaker.speak(greeting)

    # Session replay briefing — only on first wake of a new session
    briefing = session_briefing.get_briefing()
    if briefing:
        speaker.speak(briefing, mood="info")

    print(f"[NORA] {greeting}")
    print(f"[NORA] Ready. Hold [{listener.hotkey}] to speak. Say 'exit' to shut down.")

    timeouts = get_config().get("timeouts", {})
    transcribe_timeout = float(timeouts.get("transcribe_sec", 30))
    llm_timeout = float(timeouts.get("llm_sec", 20))
    loop = asyncio.get_running_loop()
    from nora import ui_server  # imported here so both text-input and voice paths can use it

    # â"€â"€ Command loop â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    while True:
        try:
            context.clear_cancel()

            # 1. Check for keyboard text input first (non-blocking)
            text = text_input.get_pending()
            rms = 0.0
            mem_ctx: dict = {}  # ensure always bound before LLM branch

            if text:
                logger.info(f"Text input: {text}")
                print(f"[NORA] Text from UI: {text}", flush=True)
            else:
                # 1b. Listen for voice
                ui_server.notify_stage("listening")
                audio = await listener.listen()
                if audio is None or len(audio) < 1600:
                    ui_server.notify_stage("idle")
                    continue

                rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))

                # Play ack immediately in PTT mode (wakeword mode acks inside listen_wakeword)
                if context.get_ptt_enabled():
                    _ack.speak_ack()

                # 2. Transcribe with timeout
                ui_server.notify_stage("transcribing")
                try:
                    text = await asyncio.wait_for(
                        loop.run_in_executor(None, transcriber.transcribe, audio),
                        timeout=transcribe_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Transcription timed out -- discarding audio")
                    speaker.speak(phrasing.get("too_slow"), mood="error")
                    ui_server.notify_stage("idle")
                    continue

            if not text or len(text.strip()) < 2:
                continue

            # NeuroSym: block adversarial voice commands before they reach the LLM
            ui_server.notify_stage("guarding")
            input_safe, input_violations = neurosym_guard.check_input(text)
            if not input_safe:
                severity = input_violations[0].get("severity", "unknown") if input_violations else "unknown"
                logger.warning(f"NeuroSym blocked input [{severity}]: {text[:80]}")
                speaker.speak(phrasing.get("blocked"), mood="error")
                cognitive_memory.record_knowledge(text, source="blocked_input")
                ui_server.notify_stage("idle")
                continue

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
                frustration.record(text_lower, rms=rms, success=True)
                continue

            exit_words = ["exit", "quit", "goodbye", "good bye", "shut down nora", "stop nora", "go to sleep"]
            if text_lower in exit_words or any(text_lower.startswith(w + " ") and len(text_lower.split()) <= 4 for w in exit_words):
                speaker.speak(phrasing.get("goodbye"))
                logger.info("Exit command received. Shutting down.")
                ambient.stop()
                vision.stop()
                return

            if is_wake_phrase(text_lower):
                speaker.speak(phrasing.get("already_awake"), mood="chat")
                continue

            # 3a. Fast-path: deterministic resolution before the LLM is ever touched.
            # Handles ~40-50% of real commands (music, volume, time, open/close, etc.)
            # in <50ms with zero network calls.
            from nora import fast_path as _fp
            _fast_intent = _fp.resolve(text)
            if _fast_intent is not None and not _fast_intent.steps and _fast_intent.response:
                # Pure conversational shortcut — speak and loop immediately
                ui_server.notify_stage("speaking")
                speaker.speak(_fast_intent.response, mood="chat")
                ui_server.notify_stage("idle")
                context.add_session_turn(
                    text=text, intent="chat", actions=[], result_summary="",
                    success=True, reply=_fast_intent.response,
                )
                frustration.record(text_lower, rms=rms, success=True)
                continue

            if _fast_intent is not None and _fast_intent.steps:
                logger.info(f"Fast-path hit: {_fast_intent.intent}")
                print(f"[NORA] Fast-path: {_fast_intent.intent}")
                intent = _fast_intent
                has_blocked, needs_confirm = security.check_steps(intent.steps)
                if has_blocked:
                    speaker.speak(phrasing.get("blocked"), mood="error")
                    frustration.record(text_lower, rms=rms, success=False)
                    ui_server.notify_stage("idle")
                    continue
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
                                None, conversation.respond, text, _chat_ctx, _act
                            ),
                            timeout=llm_timeout,
                        )
                    except asyncio.TimeoutError:
                        logger.warning("Conversation reply timed out")
                        reply = phrasing.get("too_slow")
                    except Exception as exc:
                        logger.warning(f"Conversation reply failed: {exc}")
                        reply = phrasing.get("recovered")

                    ui_server.notify_stage("speaking")
                    speaker.speak(reply, mood="chat")
                    ui_server.notify_stage("idle")

                    # Chat turns reach the session buffer too. They never did
                    # before, which is why NORA could not follow its own thread.
                    context.add_session_turn(
                        text=text, intent=f"chat:{_act.value}", actions=[],
                        result_summary="", success=True, reply=reply,
                    )
                    context.record_command(text=text, intent="chat", actions=[])
                    focus.note_activity()
                    frustration.record(text_lower, rms=rms, success=True)
                    continue

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
                            None, intent_parser.parse_intent, text, mem_ctx, screen_ctx
                        ),
                        timeout=llm_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Intent parsing timed out")
                    print("[NORA] Intent parsing timed out")
                    speaker.speak(phrasing.get("too_slow"), mood="error")
                    frustration.record(text_lower, rms=rms, success=False)
                    ui_server.notify_stage("idle")
                    continue
                except Exception as e:
                    logger.warning(f"Intent parsing failed: {e}")
                    print(f"[NORA] Intent parsing error: {e}")
                    speaker.speak(phrasing.get("not_understood"), mood="error")
                    frustration.record(text_lower, rms=rms, success=False)
                    ui_server.notify_stage("idle")
                    continue

                if intent.error:
                    print(f"[NORA] LLM returned error: {intent.error}")
                    _clarify_words = (
                        "context", "specific", "clarif", "which", "what do you mean",
                        "more information", "more detail", "please provide",
                    )
                    if any(w in intent.error.lower() for w in _clarify_words):
                        speaker.speak(phrasing.get("not_understood"), mood="error")
                    else:
                        speaker.speak(f"{phrasing.get('error')} {intent.error}", mood="error")
                    frustration.record(text_lower, rms=rms, success=False)
                    ui_server.notify_stage("idle")
                    continue

                # NeuroSym: validate action plan before execution
                ui_server.notify_stage("guarding")
                plan_safe, plan_needs_confirm, plan_violations = neurosym_guard.check_intent(intent)
                if not plan_safe:
                    speaker.speak(phrasing.get("blocked"), mood="error")
                    frustration.record(text_lower, rms=rms, success=False)
                    ui_server.notify_stage("idle")
                    continue
                if plan_needs_confirm:
                    intent.requires_confirmation = True

                # Config-based block list
                has_blocked, needs_confirm = security.check_steps(intent.steps)
                if has_blocked:
                    speaker.speak(phrasing.get("blocked"), mood="error")
                    frustration.record(text_lower, rms=rms, success=False)
                    ui_server.notify_stage("idle")
                    continue
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
                                    None, conversation.respond, text, mem_ctx, dialogue.Act.UNKNOWN
                                ),
                                timeout=llm_timeout,
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
                    speaker.speak(reply, mood="chat")
                    ui_server.notify_stage("idle")
                    context.add_session_turn(
                        text=text, intent="chat", actions=[], result_summary="",
                        success=True, reply=reply,
                    )
                    frustration.record(text_lower, rms=rms, success=bool(reply))
                    continue

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
                from nora import planner
                ui_server.notify_stage("acting")
                results = await planner.run_plan(text, mem_ctx, listener)
                context.wake_triggered = False
                summary = summarize_results(results)
                if summary:
                    ui_server.notify_stage("speaking")
                    speaker.speak(summary, mood="info" if all(r.success for r in results) else "error")
                ui_server.notify_stage("idle")
                continue

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
                speaker.speak(f"{step_labels}. Confirm?", mood="confirmation")
                confirmed = await confirmation_flow(listener)
                # Consent memory learns from every prompt (CODEX_INTEGRATION.md 5.2)
                consent_memory.record([s.action for s in intent.steps], confirmed)
                if not confirmed:
                    speaker.speak(phrasing.get("cancelled"))
                    ui_server.notify_stage("idle")
                    continue

            # 5. Execute
            ui_server.notify_stage("acting")
            results = await command_engine.execute(intent)
            context.wake_triggered = False

            # Tool trust ledger scores every invocation (CODEX_INTEGRATION.md 2.4)
            for r in results:
                tool_trust.record(r.action, r.success)

            # Post-action explanation card — "why did you do that" (CODEX_INTEGRATION.md 5.3)
            post_action_cards.build(text, intent, results, intent_confidence)

            if context.is_cancelled():
                logger.info("Cancellation observed after execute -- dropping response.")
                context.clear_cancel()
                continue

            # 6. Respond
            summary = summarize_results(results)
            all_ok_early = all(r.success for r in results) if results else True
            if summary:
                ui_server.notify_stage("speaking")
                speaker.speak(summary, mood="info" if all_ok_early else "error")
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
            if frustration.record(text_lower, rms=rms, success=all_ok):
                logger.info("Frustration detected -- offering proactive help")
                speaker.speak("You seem stuck. Want me to ask Claude for help?", mood="proactive")

        except KeyboardInterrupt:
            speaker.speak("Shutting down.")
            logger.info("Keyboard interrupt. Exiting.")
            ambient.stop()
            vision.stop()
            proactive.stop()
            consolidation.stop()
            terminal_monitor.stop()
            anomaly_watchdog.stop()
            break
        except Exception as e:
            logger.error(f"Pipeline error: {e}", exc_info=True)
            speaker.speak(phrasing.get("recovered"), mood="error")

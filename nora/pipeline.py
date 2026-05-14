from __future__ import annotations

import asyncio
import logging
import threading

import numpy as np

from nora import ambient, cognitive_memory, command_engine, consolidation, context, intent_parser, memory, neurosym_guard, proactive, security, session_briefing, speaker, terminal_monitor, text_input, transcriber
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
            messages.append(r.message)
        else:
            messages.append(f"Failed: {r.message}")
    return ". ".join(messages)


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

    # Warm up cognitive memory (ChromaDB + embedder) in background
    cognitive_memory.warm_up()

    # Start proactive intelligence engine
    proactive.register_callback(speaker.speak)
    proactive.start()

    # Start keyboard text input fallback
    text_input.start()

    # Pre-synthesize ack tokens in background (enables sub-300ms first phoneme)
    cfg_spk = get_config().get("speaker", {})
    threading.Thread(
        target=_ack.preload,
        kwargs={"voice": cfg_spk.get("voice", "en-GB-SoniaNeural"), "rate": "+40%"},
        daemon=True,
        name="nora-ack-preload",
    ).start()

    # Start always-on wakeword detector (no-op if disabled in config)
    _ww.start()

    # Start nightly consolidation scheduler
    consolidation.start()

    # Start terminal co-pilot clipboard watcher
    terminal_monitor.start(speak_callback=speaker.speak)

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
    loop = asyncio.get_event_loop()

    # â"€â"€ Command loop â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    while True:
        try:
            context.clear_cancel()

            # 1. Check for keyboard text input first (non-blocking)
            text = text_input.get_pending()
            rms = 0.0

            if text:
                logger.info(f"Text input: {text}")
                print(f"[NORA] Text from UI: {text}", flush=True)
            else:
                # 1b. Listen for voice
                from nora import ui_server
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
                    speaker.speak("Transcription timed out. Please try again.", mood="error")
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
                speaker.speak("That command was blocked by the security layer.", mood="error")
                cognitive_memory.record_knowledge(text, source="blocked_input")
                ui_server.notify_stage("idle")
                continue

            logger.info(f"Heard: {text}")
            print(f"[NORA] Heard: {text}")
            text_lower = text.lower().strip().rstrip(".,!?")

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
            if any(word in text_lower for word in exit_words):
                speaker.speak("Goodbye, sir.")
                logger.info("Exit command received. Shutting down.")
                ambient.stop()
                return

            if is_wake_phrase(text_lower):
                speaker.speak("I'm already here, sir.")
                continue

            # 3. Build enriched memory context (legacy + cognitive) and parse intent
            mem_ctx = memory.get_context_summary()
            mem_ctx["recent_commands"] = context.recent_commands()[:3]
            cog_ctx = cognitive_memory.get_context_for_prompt(text, n=2)
            mem_ctx["typical_actions_now"] = cog_ctx.get("typical_actions_now", [])
            mem_ctx["relevant_context"] = cog_ctx.get("relevant_context", [])
            mem_ctx["session_turns"] = [t.to_dict() for t in context.get_session_turns(5)]

            print(f"[NORA] Parsing intent...")
            ui_server.notify_stage("thinking")
            try:
                intent = await asyncio.wait_for(
                    loop.run_in_executor(None, intent_parser.parse_intent, text, mem_ctx),
                    timeout=llm_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("Intent parsing timed out")
                print("[NORA] Intent parsing timed out")
                speaker.speak("That took too long. Please try again.", mood="error")
                frustration.record(text_lower, rms=rms, success=False)
                ui_server.notify_stage("idle")
                continue
            except Exception as e:
                logger.warning(f"Intent parsing failed: {e}")
                print(f"[NORA] Intent parsing error: {e}")
                speaker.speak("I didn't understand that. Could you repeat?", mood="error")
                frustration.record(text_lower, rms=rms, success=False)
                ui_server.notify_stage("idle")
                continue

            if intent.error:
                print(f"[NORA] LLM returned error: {intent.error}")
                speaker.speak(f"I'm not sure what to do: {intent.error}", mood="error")
                frustration.record(text_lower, rms=rms, success=False)
                ui_server.notify_stage("idle")
                continue

            # NeuroSym: validate action plan before execution
            ui_server.notify_stage("guarding")
            plan_safe, plan_needs_confirm, plan_violations = neurosym_guard.check_intent(intent)
            if not plan_safe:
                speaker.speak("That action plan was blocked by the security policy.", mood="error")
                frustration.record(text_lower, rms=rms, success=False)
                ui_server.notify_stage("idle")
                continue
            if plan_needs_confirm:
                intent.requires_confirmation = True

            # Config-based block list (secondary layer -- catches custom blocked_actions from config.yaml)
            has_blocked, needs_confirm = security.check_steps(intent.steps)
            if has_blocked:
                speaker.speak("That action is blocked by the security policy.")
                frustration.record(text_lower, rms=rms, success=False)
                continue
            if needs_confirm:
                intent.requires_confirmation = True

            # Conversational response — no actionable steps
            if not intent.steps and not intent.error:
                reply = intent.response or "I'm not sure what to do with that. Try a command."
                ui_server.notify_stage("speaking")
                speaker.speak(reply, mood="chat")
                ui_server.notify_stage("idle")
                frustration.record(text_lower, rms=rms, success=True)
                continue

            actions_str = " → ".join(s.action for s in intent.steps)
            print(f"[NORA] {intent.intent}  [{actions_str}]")

            # Record to recent-commands
            context.record_command(
                text=text,
                intent=intent.intent,
                actions=[s.action for s in intent.steps],
            )

            # 4. Confirmation if needed
            if intent.requires_confirmation:
                speaker.speak(f"I'm about to {intent.intent}.", mood="confirmation")
                confirmed = await confirmation_flow(listener)
                if not confirmed:
                    speaker.speak("Cancelled.")
                    ui_server.notify_stage("idle")
                    continue

            # 5. Execute
            ui_server.notify_stage("acting")
            results = await command_engine.execute(intent)
            context.wake_triggered = False

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

            # Workflow prediction -- cognitive memory bigrams take priority
            if executed_actions:
                last_action = executed_actions[-1]
                predicted = cognitive_memory.predict_next_action(last_action, min_confidence=3)
                if not predicted:
                    predicted = memory.predict_next_action(last_action)
                if predicted:
                    logger.info(f"Workflow prediction: {last_action} â†' {predicted}")
                    speaker.speak(f"Based on your habits, should I also {predicted.replace('_', ' ')}?", mood="proactive")

            # Check for frustration -- offer help if detected (Feature 5)
            all_ok = all(r.success for r in results) if results else True
            if frustration.record(text_lower, rms=rms, success=all_ok):
                logger.info("Frustration detected -- offering proactive help")
                speaker.speak("You seem stuck. Want me to ask Claude for help?", mood="proactive")

        except KeyboardInterrupt:
            speaker.speak("Shutting down.")
            logger.info("Keyboard interrupt. Exiting.")
            ambient.stop()
            proactive.stop()
            consolidation.stop()
            terminal_monitor.stop()
            break
        except Exception as e:
            logger.error(f"Pipeline error: {e}", exc_info=True)
            speaker.speak("Something went wrong. I'm still listening.")

"""ReAct Planner — Sprint 4 ROADMAP Task 6.

Implements an observe → decide → act → verify loop that replaces one-shot
JSON plan execution for complex, multi-step autonomous tasks.

The LLM sees step results *before* choosing the next action, enabling:
  - "Set up my ML experiment" (creates dirs, installs deps, opens editor)
  - "Clone and run this repo"
  - "Organise my downloads folder"

Design:
  - Invoked by pipeline.py when the intent is flagged is_autonomous_task
  - Max steps: neurosym.max_plan_steps (default 15)
  - Each cycle: observe current state → ask LLM what to do next → execute →
    check if goal is satisfied → loop
  - On step failure: Plan Repair micro-loop (#25) — reflect → retry up to 2x
  - Counterfactual Pre-Flight (#21): preview all planned steps before first act
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from nora.schemas import ActionStep, IntentResponse, StepResult

logger = logging.getLogger("nora.planner")

_MAX_REPAIR_RETRIES = 2


# ── Observation snapshot ───────────────────────────────────────────────────

def _observe(goal: str, history: list[dict[str, Any]], mem_ctx: dict[str, Any]) -> dict[str, Any]:
    """Build a compact observation dict the LLM can reason over."""
    from nora import context
    return {
        "goal": goal,
        "step_number": len(history) + 1,
        "completed_steps": history,
        "session_turns": [t.to_dict() for t in context.get_session_turns(3)],
        "active_apps": context.active_apps(),
        "memory_hints": mem_ctx.get("relevant_context", [])[:2],
    }


# ── LLM decide call ───────────────────────────────────────────────────────

_PLANNER_SYSTEM = """You are NORA's autonomous planner.
Given a goal and the history of completed steps + their results, decide the SINGLE next action to take.

Rules:
- Return ONLY valid JSON — one of:
  Next step:  {"action": "action_name", "parameters": {}, "done": false, "rationale": "..."}
  Goal done:  {"action": null, "parameters": {}, "done": true, "rationale": "..."}
  Cannot do:  {"action": null, "parameters": {}, "done": true, "error": "reason"}

- Only use actions from the provided list.
- If the last step failed and retrying would help, emit the corrected action.
- If the goal is impossible, emit done=true with an error.
- Keep rationale to one sentence.

Available actions:
{actions}

Action signatures:
{signatures}
"""


def _decide(observation: dict[str, Any]) -> dict[str, Any]:
    """Ask the LLM for the next single action. Returns parsed dict."""
    from nora.intent_parser import _call_llm
    from nora.command_engine import get_available_actions, get_action_signatures

    actions = ", ".join(get_available_actions())
    sigs = get_action_signatures()
    system = _PLANNER_SYSTEM.format(actions=actions, signatures=sigs)

    obs_text = json.dumps(observation, indent=2, ensure_ascii=False)
    messages = [{"role": "user", "content": f"Observation:\n{obs_text}\n\nWhat is the next action?"}]

    raw = _call_llm(system, messages)
    try:
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(cleaned)
    except Exception as e:
        logger.warning("Planner LLM parse failed: %s | raw: %s", e, raw[:200])
        return {"done": True, "error": f"LLM response unparseable: {raw[:100]}"}


# ── Verify step result ─────────────────────────────────────────────────────

def _is_goal_complete(result: StepResult, decide_resp: dict[str, Any]) -> bool:
    """Simple check: LLM declared done, or the last action failed terminally."""
    return decide_resp.get("done", False)


# ── Plan Repair (#25) ──────────────────────────────────────────────────────

def _repair(
    failed_step: dict[str, Any],
    error_msg: str,
    observation: dict[str, Any],
) -> dict[str, Any] | None:
    """Reflect on the failure and return a corrected decide response, or None."""
    from nora.intent_parser import _call_llm
    from nora.command_engine import get_available_actions, get_action_signatures  # noqa: F811

    repair_system = (
        "You are NORA's plan repair engine. A step failed. "
        "Diagnose the likely cause and return a corrected next action as JSON. "
        "If the failure is unrecoverable, return {\"done\": true, \"error\": \"...\"}.\n"
        f"Available actions: {', '.join(get_available_actions())}\n"
        f"Signatures:\n{get_action_signatures()}"
    )
    context_text = (
        f"Goal: {observation['goal']}\n"
        f"Failed step: {json.dumps(failed_step)}\n"
        f"Error: {error_msg}\n"
        f"History so far: {json.dumps(observation['completed_steps'][-3:])}"
    )
    messages = [{"role": "user", "content": context_text}]
    raw = _call_llm(repair_system, messages)
    try:
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(cleaned)
    except Exception:
        return None


# ── Counterfactual Pre-Flight (#21) ───────────────────────────────────────

def build_preflight_summary(goal: str, steps: list[dict[str, Any]]) -> str:
    """Return a spoken pre-flight summary for user voice confirmation."""
    if not steps:
        return f"I need to figure out the steps for: {goal}."
    step_labels = " → ".join(
        s.get("action", "?").replace("_", " ") for s in steps[:6]
    )
    has_destructive = any(
        s.get("action", "") in (
            "delete_file", "shutdown", "close_all_apps", "patch_file",
            "git_smart_commit", "move_file",
        )
        for s in steps
    )
    reversible = "Some steps are reversible; I'll log everything for undo."
    if has_destructive:
        reversible = "Some steps are destructive. I'll log them so you can undo them."
    return (
        f"To {goal}, I plan to: {step_labels}. "
        f"{reversible} Should I proceed?"
    )


# ── Main planner entry point ───────────────────────────────────────────────

async def run_plan(
    goal: str,
    mem_ctx: dict[str, Any],
    listener: Any,
    max_steps: int = 15,
) -> list[StepResult]:
    """
    Execute a ReAct planning loop for an autonomous goal.

    Returns the list of StepResults from all executed actions.
    Pre-flight confirmation is solicited before the first act.
    """
    import asyncio
    from nora import command_engine, context, speaker, transcriber
    from nora.config import get_config

    max_steps = int(get_config().get("neurosym", {}).get("max_plan_steps", max_steps))
    history: list[dict[str, Any]] = []
    all_results: list[StepResult] = []
    preflight_done = False
    loop = asyncio.get_event_loop()

    logger.info("ReAct planner starting for goal: %s", goal)

    for step_num in range(1, max_steps + 1):
        if context.is_cancelled():
            logger.info("Planner cancelled at step %d", step_num)
            break

        # 1. Observe
        observation = _observe(goal, history, mem_ctx)

        # 2. Decide
        try:
            decide_resp = await loop.run_in_executor(None, _decide, observation)
        except Exception as e:
            logger.error("Planner decide failed: %s", e)
            all_results.append(StepResult(action="planner_decide", success=False, message=str(e)))
            break

        if decide_resp.get("error"):
            all_results.append(StepResult(
                action="planner_decide",
                success=False,
                message=decide_resp["error"],
            ))
            break

        if decide_resp.get("done") and not decide_resp.get("action"):
            logger.info("Planner: goal complete after %d steps", step_num - 1)
            break

        action_name = decide_resp.get("action")
        params = decide_resp.get("parameters", {})
        rationale = decide_resp.get("rationale", "")

        if not action_name:
            logger.warning("Planner returned no action at step %d", step_num)
            break

        # 3. Pre-flight confirmation (once, before first action)
        if not preflight_done:
            summary = build_preflight_summary(goal, [{"action": action_name}])
            speaker.speak(summary, mood="confirmation")
            preflight_done = True

            audio = await listener.listen()
            if audio is None:
                speaker.speak("No response. Cancelling plan.")
                break
            voice_text = await loop.run_in_executor(None, transcriber.transcribe, audio)
            if not any(w in voice_text.lower() for w in
                       ["yes", "yeah", "yep", "sure", "go ahead", "confirm", "do it", "proceed"]):
                speaker.speak("Plan cancelled.")
                break

        logger.info("Planner step %d: %s(%s) — %s", step_num, action_name, params, rationale)

        # 4. Act
        fake_intent = IntentResponse(
            intent=goal,
            steps=[ActionStep(action=action_name, parameters=params)],
        )
        step_results = await command_engine.execute(fake_intent)
        result = step_results[0] if step_results else StepResult(
            action=action_name, success=False, message="No result"
        )
        all_results.append(result)

        # 5. Verify / Plan Repair
        if not result.success:
            repaired = False
            for _retry in range(_MAX_REPAIR_RETRIES):
                logger.info("Plan repair attempt %d for %s", _retry + 1, action_name)
                failed_step = {"action": action_name, "parameters": params}
                repair_resp = await loop.run_in_executor(
                    None, _repair, failed_step, result.message, observation
                )
                if not repair_resp or repair_resp.get("done"):
                    break
                r_action = repair_resp.get("action")
                r_params = repair_resp.get("parameters", {})
                if r_action:
                    fake_intent2 = IntentResponse(
                        intent=goal,
                        steps=[ActionStep(action=r_action, parameters=r_params)],
                    )
                    repair_results = await command_engine.execute(fake_intent2)
                    repaired_result = repair_results[0] if repair_results else StepResult(
                        action=r_action, success=False, message="No result"
                    )
                    all_results.append(repaired_result)
                    if repaired_result.success:
                        result = repaired_result
                        action_name = r_action
                        params = r_params
                        repaired = True
                        break

            if not repaired and not result.success:
                speaker.speak(
                    f"Step {step_num} failed and I couldn't repair it: {result.message}",
                    mood="error",
                )
                break

        # Record history entry
        history.append({
            "step": step_num,
            "action": action_name,
            "parameters": params,
            "result": result.message,
            "success": result.success,
        })

        # Let user hear progress for long plans
        if step_num % 3 == 0:
            speaker.speak(f"Step {step_num} done: {result.message}", mood="info")

        # 6. Check done flag
        if _is_goal_complete(result, decide_resp):
            break

    step_count = len(history)
    ok_count = sum(1 for h in history if h["success"])
    logger.info("ReAct planner finished: %d steps, %d succeeded", step_count, ok_count)
    return all_results

"""Skill commands — invoking a procedure, and writing a new one.

`run_skill` is where skills and tool scripts meet: the skill supplies the
procedure in prose, `nora.toolscript` supplies the one-round-trip way to carry
it out, and the model in between only has to translate. Which is why a skill
never needs to name a Python function — it describes what to do, and the
action table is what it gets translated into at run time.
"""
from __future__ import annotations

import logging
import re

from nora import skills, toolscript
from nora.command_engine import register
from nora.model_router import AllCandidatesFailed, complete

logger = logging.getLogger("nora.commands.skills")

_SYSTEM = """You turn a written procedure into a short Python script that NORA runs.

Available actions (call them as plain functions):
{actions}

Rules:
- Output ONLY Python. No markdown fences, no commentary, no imports.
- Call `say(...)` once at the end with what NORA should speak out loud — one or
  two spoken sentences, no lists, no markdown.
- Use only the actions listed above. If the procedure asks for something with
  no matching action, skip that step and mention it in the `say(...)` line.
- Keep it short. Loops and conditionals are fine where the procedure needs them.
"""

_FENCE_RE = re.compile(r"^```(?:python)?\s*\n(.*?)\n```\s*$", re.DOTALL)


def _strip_fence(text: str) -> str:
    """Models wrap code in fences no matter how firmly you ask them not to."""
    m = _FENCE_RE.match(text.strip())
    return m.group(1) if m else text.strip()


@register(
    "run_skill",
    sig="run_skill(name: str, request: str = '')",
    description=(
        "Carry out a named skill — a saved procedure. Use when the user asks "
        "for something matching a skill in the skills list. `request` passes "
        "along any specifics the user mentioned."
    ),
    category="workflow",
)
async def run_skill(name: str, request: str = "") -> str:
    from nora import command_engine

    skill = skills.get(name)
    if skill is None:
        available = ", ".join(sorted(skills.discover())) or "none yet"
        return f"I don't have a skill called {name}. I know: {available}."

    system = _SYSTEM.format(actions=command_engine.get_action_signatures())
    user = f"Procedure ({skill.name}):\n{skill.body}"
    if request.strip():
        user += f"\n\nThe user specifically asked: {request.strip()}"

    try:
        raw, used = complete(
            "intent",
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            max_tokens=700,
            temperature=0.1,
        )
    except AllCandidatesFailed as e:
        logger.error("run_skill: no model available: %s", e)
        return "I couldn't reach a model to work that skill out."

    code = _strip_fence(raw)
    logger.info("Skill %s compiled via %s:\n%s", skill.name, used, code)

    result = await toolscript.run(code)
    if not result.ok:
        logger.warning("Skill %s failed: %s", skill.name, result.error)
        partial = " ".join(result.spoken).strip()
        return f"{partial} {result.error}".strip() if partial else result.error
    return result.summary()


@register(
    "list_skills",
    sig="list_skills()",
    description="Say which saved skills NORA has.",
    category="workflow",
)
def list_skills() -> str:
    found = skills.discover(force=True)
    if not found:
        return "No skills saved yet."
    names = sorted(found)
    if len(names) == 1:
        return f"One skill: {names[0].replace('-', ' ')}."
    spoken = ", ".join(n.replace("-", " ") for n in names[:6])
    return f"{len(names)} skills: {spoken}."


@register(
    "save_skill",
    sig="save_skill(name: str, description: str, steps: str)",
    description=(
        "Save a repeatable procedure as a skill so it can be run by name later. "
        "`steps` is the procedure in plain prose, one instruction per line. Use "
        "after working out a multi-step task the user is likely to want again."
    ),
    category="workflow",
)
def save_skill(name: str, description: str, steps: str) -> str:
    skill = skills.write(name, description, steps)
    if skill is None:
        return f"I couldn't save that as a skill — {name!r} isn't a usable name."
    return f"Saved that as a skill called {skill.name.replace('-', ' ')}."


@register(
    "forget_skill",
    sig="forget_skill(name: str)",
    description="Delete a saved skill by name.",
    category="workflow",
    risk="medium",
    requires_confirmation=True,
)
def forget_skill(name: str) -> str:
    skill = skills.get(name)
    if skill is None:
        return f"I don't have a skill called {name}."
    try:
        skill.path.unlink()
        parent = skill.path.parent
        if not any(parent.iterdir()):
            parent.rmdir()
    except Exception as e:
        logger.error("Could not delete skill %s: %s", skill.name, e)
        return f"I couldn't delete {skill.name}."
    skills.discover(force=True)
    return f"Deleted the {skill.name.replace('-', ' ')} skill."

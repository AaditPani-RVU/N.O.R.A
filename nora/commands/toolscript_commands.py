"""The `run_script` action — the model's way to spend one inference on N steps.

Registered like any other command, which is the whole trick: no pipeline
surgery, no second execution path. `intent_parser` emits a single step whose
parameter happens to be Python, `nora.security.check_steps` reads the actions
out of that Python so the confirmation gate still sees them, and
`nora.toolscript` runs it against the command table.
"""
from __future__ import annotations

import logging

from nora import toolscript
from nora.command_engine import register

logger = logging.getLogger("nora.commands.toolscript")


@register(
    "run_script",
    sig="run_script(code: str)",
    description=(
        "Run several actions in one go as a short Python script. Use this "
        "whenever a request needs more than one action, needs a loop, or needs "
        "one action's result to decide the next — it is one round-trip instead "
        "of several, which the user hears as a faster answer. Call registered "
        "actions as plain functions and pass anything to say for the spoken "
        "reply, e.g.  pause_music(); brightness = set_brightness(40); "
        "say('Paused, and dimmed the screen.')  — no imports, no file access."
    ),
    category="system",
)
async def run_script(code: str) -> str:
    result = await toolscript.run(code)
    if not result.ok:
        logger.warning("run_script failed: %s", result.error)
        # Spoken output already accumulated before the failure is still worth
        # saying — half a plan that ran is not the same as nothing happening.
        partial = " ".join(result.spoken).strip()
        return f"{partial} {result.error}".strip() if partial else result.error
    return result.summary()

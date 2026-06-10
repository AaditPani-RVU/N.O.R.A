"""Voice commands for the Persona Calibration System.

Voice commands
--------------
"be more concise"               → set_persona(verbosity="concise")
"give me longer answers"        → set_persona(verbosity="detailed")
"use a casual tone"             → set_persona(tone="casual")
"be more technical"             → set_persona(tone="technical")
"be professional"               → set_persona(tone="professional")
"be more conversational"        → set_persona(style="conversational")
"be more direct"                → set_persona(style="direct")
"what's your current persona"   → get_persona()
"reset your persona"            → reset_persona()
"""
from __future__ import annotations

from nora.command_engine import register


@register(
    "set_persona",
    sig='set_persona(verbosity: str = "", tone: str = "", style: str = "")',
    description="Tune NORA's communication style: verbosity, tone, or style",
    category="",
)
def set_persona(verbosity: str = "", tone: str = "", style: str = "") -> str:
    from nora import persona as _p
    kwargs: dict[str, str] = {}
    changes: list[str] = []
    if verbosity:
        kwargs["verbosity"] = verbosity
        changes.append(f"verbosity to {verbosity}")
    if tone:
        kwargs["tone"] = tone
        changes.append(f"tone to {tone}")
    if style:
        kwargs["style"] = style
        changes.append(f"style to {style}")
    if not kwargs:
        return "Specify at least one of verbosity, tone, or style."
    result = _p.update(**kwargs)
    applied = [k for k in kwargs if result.get(k) == kwargs[k].lower()]
    if not applied:
        return "Could not apply those settings. Check that the values are valid."
    return "Persona updated: " + ", ".join(changes) + "."


@register(
    "get_persona",
    sig="get_persona()",
    description="Describe NORA's current communication style",
    category="",
)
def get_persona() -> str:
    from nora import persona as _p
    p = _p.get()
    return (
        f"Current persona: {p['verbosity']} verbosity, "
        f"{p['tone']} tone, "
        f"{p['style']} style."
    )


@register(
    "reset_persona",
    sig="reset_persona()",
    description="Reset NORA's communication style to defaults",
    category="",
)
def reset_persona() -> str:
    from nora import persona as _p
    _p.reset()
    return (
        "Persona reset to defaults: normal verbosity, "
        "professional tone, direct style."
    )

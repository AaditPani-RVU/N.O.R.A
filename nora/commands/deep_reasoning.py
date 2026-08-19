"""Deep reasoning command — routes hard, multi-step questions through
nora.model_router's "reasoning" role (NVIDIA Nemotron 253-class model first,
Groq 70B / local Ollama as fallback). Reserved for questions that actually
need that class of model — see NORA_ODYSSEUS_PLAN.md §1.4/§2.2: NVIDIA NIM
credits don't refill, so this isn't the everyday research path
(tell_me_about / ask_claude cover that).
"""
from __future__ import annotations

import logging

from nora.command_engine import register
from nora.model_router import AllCandidatesFailed, complete

logger = logging.getLogger("nora.commands.deep_reasoning")

_SYSTEM_PROMPT = (
    "Answer in 2-4 spoken sentences. No bullet points, no markdown, no filler "
    "words ('so', 'well', 'basically') — plain conversational text as if "
    "speaking out loud. Think it through, but only speak the conclusion."
)


@register(
    "deep_reasoning",
    sig="deep_reasoning(question: str)",
    description=(
        "For genuinely hard multi-step reasoning, math, or logic questions "
        "that need a large reasoning-tuned model — not everyday research "
        "(use tell_me_about for that). Routes through NVIDIA/Groq/local."
    ),
    category="web",
)
def deep_reasoning(question: str) -> str:
    logger.info("Deep reasoning: %s", question)
    try:
        text, used = complete(
            "reasoning",
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ],
            max_tokens=400,
            temperature=0.2,
        )
        logger.info("Deep reasoning answered via %s", used)
        return text.strip()
    except AllCandidatesFailed as e:
        logger.error("deep_reasoning: all candidates failed: %s", e)
        return "I couldn't reach any reasoning model right now."

"""Deferred answers — questions NORA takes away and comes back with.

The failure this fixes: ask NORA something hard about code and it says "let me
get back to you on that", then never does. Two separate causes, both real.

*The chat model promises what the process cannot do.* Asked a hard question on
the conversation path, a 70B model does the humanly reasonable thing and offers
to go away and think. Nothing was listening for that offer. There was no queue,
no worker, no second turn — the sentence was the whole of it.

*The budgets don't fit.* `command_engine.execute` kills any step at
`timeouts.command_sec` (45s). `ask_claude` in document mode asks for 180s and
reads the repo. A question that needs the repo read was structurally incapable
of being answered on the turn that asked it; the engine killed it two minutes
early, every time.

So a hard question now becomes a real `nora.jobs` job. NORA says it's going
away to work — truthfully — the turn ends, the microphone goes back to
listening, and when the answer lands NORA speaks it unprompted, re-stating what
it was answering. That is the behaviour the model was already promising; this
is the machinery that makes the promise good.
"""
from __future__ import annotations

import logging

from nora import jobs
from nora.command_engine import register

logger = logging.getLogger("nora.commands.deferred")

# Spoken the moment a question is handed off. Varied so a user who defers
# several things in a row doesn't hear the same sentence back each time, and
# phrased as a commitment with a follow-up rather than an apology.
_HANDOFF = [
    "Let me look into that properly — I'll come back to you.",
    "That one needs a minute. I'll get back to you.",
    "Working on it in the background. I'll tell you when I have it.",
    "Give me a couple of minutes on that one.",
]


def _handoff_line() -> str:
    import random
    return random.choice(_HANDOFF)


def _shorten(question: str, limit: int = 60) -> str:
    """A title short enough to speak back inside a sentence.

    Used as "Back to <title> — <answer>", so it has to read as a noun phrase.
    """
    q = " ".join(question.strip().split())
    q = q.rstrip("?.!")
    for prefix in (
        "can you tell me", "could you tell me", "i want to know",
        "tell me about", "can you explain", "could you explain",
        "explain to me", "i was wondering", "do you know",
        "can you find out", "find out", "look into", "figure out",
        "what do you think about", "your question about",
    ):
        if q.lower().startswith(prefix):
            q = q[len(prefix):].strip()
            break
    if len(q) > limit:
        q = q[:limit].rsplit(" ", 1)[0] + "..."
    return f"your question about {q}" if q else "your question"


def _answer_with_claude(question: str, model: str, read_repo: bool) -> str:
    """Run the real answering call on the worker thread.

    Calls `ask_claude`'s private `_call_claude` rather than the registered
    command, deliberately: the command wraps document-mode replies in "saved it
    to <path>" phrasing meant for a turn that is still in progress. Here the
    answer itself is what gets spoken, minutes later.
    """
    from nora.commands.ask_claude import _build_context_block, _call_claude

    prompt = (
        f"{_build_context_block()}"
        f"You are answering a question the user asked out loud a few minutes "
        f"ago; your reply is going to be spoken back to them. Lead with the "
        f"answer. Four to six spoken sentences, no markdown, no bullet points, "
        f"no code blocks — if code matters, describe it in words.\n\n"
        f"Question: {question}"
    )
    return _call_claude(prompt, model_key=model, read_repo=read_repo)


@register(
    "answer_later",
    sig="answer_later(question: str, read_repo: bool = False)",
    description=(
        "For a question too slow to answer on this turn — anything needing "
        "real analysis, reading the codebase, or careful reasoning. Hands it "
        "to a background worker and speaks the answer unprompted when ready. "
        "Set read_repo=True for questions about the user's own code. Prefer "
        "this over ask_claude whenever the honest answer is 'that'll take a "
        "minute' — never promise to get back to the user any other way."
    ),
    category="web",
)
def answer_later(question: str, read_repo: bool = False) -> str:
    read_repo = str(read_repo).lower() not in ("false", "0", "no", "")
    # Opus for repo-grounded work, Sonnet otherwise. The repo path is already
    # paying 180s and a tool-enabled session; the better model is marginal on
    # top of that, and these are the questions worth getting right.
    model = "opus" if read_repo else "sonnet"

    jobs.submit(
        _shorten(question),
        lambda: _answer_with_claude(question, model, read_repo),
        kind="answer",
    )
    logger.info("Deferred question (repo=%s): %s", read_repo, question[:80])
    return _handoff_line()


@register(
    "pending_work",
    sig="pending_work()",
    description="Say what NORA is still working on in the background.",
    category="tasks",
)
def pending_work() -> str:
    return jobs.describe_pending()


@register(
    "recall_answer",
    sig="recall_answer(topic: str)",
    description=(
        "Retrieve the answer to something NORA went away to work on — "
        "'what did you find out about X', 'did you get that thing yet'."
    ),
    category="tasks",
)
def recall_answer(topic: str) -> str:
    job = jobs.find(topic)
    if job is None:
        return "I don't have anything on that. Want me to look into it?"
    if job.status == jobs.STATUS_DONE:
        return job.result or "I got it, but the answer came back empty."
    if job.status == jobs.STATUS_FAILED:
        return f"That one failed. {job.error}".strip()
    return f"Still working on {job.title}. I'll speak up when it's done."

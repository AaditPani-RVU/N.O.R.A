"""Ask Claude command — shells out to the `claude` CLI so no API key is needed."""
from __future__ import annotations

import logging
import subprocess
import shutil
from pathlib import Path

from nora.command_engine import register
from nora import claude_logs

logger = logging.getLogger("nora.commands.ask_claude")

# Bare CLI aliases, not pinned IDs: `claude --model opus|sonnet|haiku` always
# resolves to the latest model in that tier, so these never go stale. The pins
# they replaced (claude-opus-4-7 / claude-sonnet-4-6) still resolve, but they
# are previous-generation — which is why "ask Claude what's happening in Iran"
# came back with an August 2025 knowledge cutoff.
CLAUDE_MODELS = {
    "opus":   "opus",
    "sonnet": "sonnet",
    "haiku":  "haiku",
}
DEFAULT_MODEL = "sonnet"

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Stated to the CLI on every call. The failure this prevents: Claude describing a
# file it "created" that no approval could ever land, which NORA then reported as
# done. It has no write tools here, so saying so up front keeps it from narrating
# a write instead of doing the work.
_NO_WRITE_RULE = (
    "You have no file-writing tools in this session and cannot save anything. "
    "NORA saves your reply for you, into the claude_logs folder of the JARVIS "
    "project. Never say you created, wrote, or saved a file, never name a "
    "destination path, and never ask for approval to write -- there is no one "
    "to approve it. Return the finished content itself."
)


def _active_window_title() -> str:
    for cmd in (
        ["xdotool", "getactivewindow", "getwindowname"],
        ["wmctrl", "-l"],  # fallback — returns all windows
    ):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=1).stdout.strip()
            if out:
                return out.splitlines()[0]
        except Exception:
            pass
    return ""


def _clipboard_text() -> str:
    for cmd in (
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
        ["wl-paste"],  # Wayland
    ):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=1).stdout
            if out:
                return out[:2000]
        except Exception:
            pass
    return ""


def _build_context_block() -> str:
    parts: list[str] = []
    window = _active_window_title()
    if window:
        parts.append(f"Active window: {window}")
    clip = _clipboard_text().strip()
    if clip:
        parts.append(f"Clipboard:\n{clip}")
    if not parts:
        return ""
    return "[Current context]\n" + "\n".join(parts) + "\n\n"


def _call_claude(
    prompt: str,
    model_key: str = DEFAULT_MODEL,
    *,
    read_repo: bool = False,
) -> str:
    """Send a prompt to Claude via the `claude` CLI and return the response text.

    Deliberately runs with no file-writing tools. A one-shot `claude -p` has no
    way to get a write approved (stdin is closed, there is nobody to prompt), so
    when it tried it would answer "I created a log at logs/foo.md -- approve the
    write prompt" and NORA would speak that as a completed fact for a file that
    never existed. Claude returns document *text*; `claude_logs.write` puts it on
    disk. `read_repo` grants read-only tools so a log can describe real code.
    """
    if not shutil.which("claude"):
        return "Claude CLI not found. Make sure Claude Code is installed and `claude` is in PATH."

    model_id = CLAUDE_MODELS.get(model_key, CLAUDE_MODELS[DEFAULT_MODEL])
    cmd = [
        "claude", "-p", prompt,
        "--model", model_id,
        "--output-format", "text",
        # NORA owns persistence. Claude gets no way to touch the filesystem, so
        # it cannot claim a write it did not perform.
        "--disallowedTools", "Write,Edit,NotebookEdit,Bash,Task",
        "--append-system-prompt", _NO_WRITE_RULE,
    ]
    if read_repo:
        cmd += ["--allowedTools", "Read,Glob,Grep", "--add-dir", str(_REPO_ROOT)]

    try:
        result = subprocess.run(
            cmd,
            # Without this the CLI inherits NORA's stdin and waits 3s for piped
            # input that never comes, adding that delay to every answer.
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=180 if read_repo else 60,
            cwd=str(_REPO_ROOT),
        )
        if result.returncode != 0:
            logger.error("claude CLI error: %s", result.stderr[:200])
            return "Claude returned an error. Check that you're logged in with `claude`."
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "Claude took too long to respond."
    except Exception as e:
        logger.error("ask_claude subprocess error: %s", e)
        return "Couldn't reach Claude right now."


def _capture_phantom_paths(question: str, response: str) -> tuple[str, Path | None]:
    """Back-stop: if a reply still names a file, make that file real.

    The system prompt should prevent it, but a model naming `foo.md` in prose is
    the exact failure this whole module exists to prevent. Rather than let the
    claim stand unbacked, persist the reply and restate the true location.
    """
    claims = [c for c in claude_logs.find_claimed_paths(response)
              if not claude_logs.resolve(c).exists()]
    if not claims:
        return response, None
    path = claude_logs.write(
        question or "Claude note", response, source="ask_claude (recovered claim)",
        question=question,
    )
    rel = claude_logs.describe(path)
    logger.warning("Claude claimed unwritten path(s) %s -- saved reply to %s", claims, rel)
    for claim in claims:
        response = response.replace(claim, rel)
    return response, path


@register(
    "ask_claude",
    sig="ask_claude(question: str, model: str = 'sonnet')",
    description="Ask Claude (via claude CLI) and speak the answer. model: opus | sonnet | haiku",
    category="web",
)
def ask_claude(question: str, model: str = DEFAULT_MODEL) -> str:
    context_block = _build_context_block()
    as_document = claude_logs.wants_document(question)

    if as_document:
        prompt = (
            f"{context_block}"
            f"{_NO_WRITE_RULE}\n\n"
            f"Produce the document as your entire reply: GitHub-flavoured Markdown, "
            f"no preamble, no sign-off, no offer to save it. Start at the first "
            f"heading or bullet of the content itself.\n\n"
            f"Request: {question}"
        )
    else:
        prompt = (
            f"{context_block}"
            f"Answer in 2-4 spoken sentences. No bullet points, no markdown — "
            f"plain conversational text as if speaking out loud.\n\nQuestion: {question}"
        )

    logger.info("Asking Claude (%s, document=%s): %s", model, as_document, question)
    response = _call_claude(prompt, model_key=model, read_repo=as_document)

    if response.startswith(("Claude CLI not found", "Claude returned an error",
                            "Claude took too long", "Couldn't reach Claude")):
        return response

    if as_document:
        path = claude_logs.write(
            question, response, source=f"ask_claude:{model}", question=question,
        )
        logger.info("Claude document saved: %s", path)
        return (
            f"Saved it to {claude_logs.describe(path)} in your JARVIS folder. "
            f"{claude_logs.gist(response)}"
        )

    response, _ = _capture_phantom_paths(question, response)
    logger.info("Claude responded: %s…", response[:80])
    return response


@register(
    "ask_claude_opus",
    sig="ask_claude_opus(question: str)",
    description="Ask Claude Opus (most capable) for a detailed answer.",
    category="web",
)
def ask_claude_opus(question: str) -> str:
    return ask_claude(question, model="opus")


@register(
    "recent_logs",
    sig="recent_logs(limit: int = 5)",
    description="List the most recent documents NORA saved to claude_logs/.",
    category="file",
)
def recent_logs(limit: int = 5) -> str:
    """Ground truth for 'where did you put that file?' -- reads the directory."""
    files = claude_logs.recent(int(limit))
    if not files:
        return "No saved logs yet. They go in the claude_logs folder inside JARVIS."
    lines = [f"{len(files)} most recent in {claude_logs.describe(claude_logs.LOG_DIR)}:"]
    lines += [f"- {p.name}" for p in files]
    return "\n".join(lines)

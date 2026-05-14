"""Voice-Driven Code Surgery — explain, refactor, and analyse code via clipboard selection.

The user selects code in their editor, copies it, then issues a voice command.
All operations read from the clipboard and (for refactor) write back to it.

Commands:
  explain_selection()            — explain what the selected code does
  refactor_selection(instruction) — apply a transformation and put result in clipboard
  add_tests_for_selection()      — generate pytest tests for the selected code
  bisect_failure(error)          — root-cause an error given clipboard code context
  dependency_audit()             — audit requirements.txt / pyproject.toml for issues
"""
from __future__ import annotations

import logging

from nora.command_engine import register

logger = logging.getLogger("nora.commands.code_surgery")


def _clipboard() -> str:
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        try:
            return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT) or ""
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return ""


def _set_clipboard(text: str) -> None:
    try:
        import win32clipboard
        import win32con
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        win32clipboard.CloseClipboard()
    except Exception as e:
        logger.warning("set_clipboard failed: %s", e)


def _claude_haiku(prompt: str, max_tokens: int = 400) -> str:
    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


@register(
    "explain_selection",
    sig="explain_selection()",
    description="Explain the code currently in the clipboard (editor selection)",
    category="dev",
)
def explain_selection() -> str:
    code = _clipboard().strip()
    if not code:
        return "Clipboard is empty. Copy some code first."
    if len(code) > 4000:
        code = code[:4000] + "\n... (truncated)"
    try:
        reply = _claude_haiku(
            f"Explain this code in 2-4 spoken sentences. No markdown, no bullet points:\n\n{code}",
            max_tokens=300,
        )
        return reply
    except Exception as e:
        logger.error("explain_selection error: %s", e)
        return f"Could not explain selection: {e}"


@register(
    "refactor_selection",
    sig="refactor_selection(instruction: str)",
    description="Refactor clipboard code per instruction and put result back in clipboard",
    category="dev",
)
def refactor_selection(instruction: str) -> str:
    code = _clipboard().strip()
    if not code:
        return "Clipboard is empty. Copy some code first."
    if len(code) > 4000:
        code = code[:4000]
    try:
        result = _claude_haiku(
            f"Refactor this code: {instruction}\n\n"
            f"Return ONLY the refactored code, no explanation, no markdown fences:\n\n{code}",
            max_tokens=600,
        )
        _set_clipboard(result)
        lines = result.splitlines()
        return f"Done. {len(lines)}-line result in clipboard. First line: {lines[0][:60]}."
    except Exception as e:
        logger.error("refactor_selection error: %s", e)
        return f"Refactor failed: {e}"


@register(
    "add_tests_for_selection",
    sig="add_tests_for_selection()",
    description="Generate pytest tests for clipboard code and put them in clipboard",
    category="dev",
)
def add_tests_for_selection() -> str:
    code = _clipboard().strip()
    if not code:
        return "Clipboard is empty. Copy some code first."
    if len(code) > 3000:
        code = code[:3000]
    try:
        result = _claude_haiku(
            "Write pytest unit tests for this code. Include edge cases. "
            "Return ONLY valid Python test code, no explanation:\n\n" + code,
            max_tokens=600,
        )
        _set_clipboard(result)
        lines = result.splitlines()
        return f"Generated {len(lines)}-line test file — pasted to clipboard."
    except Exception as e:
        logger.error("add_tests_for_selection error: %s", e)
        return f"Test generation failed: {e}"


@register(
    "bisect_failure",
    sig="bisect_failure(error: str = '')",
    description="Root-cause an error; uses clipboard for code context if available",
    category="dev",
)
def bisect_failure(error: str = "") -> str:
    code_ctx = _clipboard().strip()
    error_text = error or code_ctx
    if not error_text:
        return "Provide the error as a parameter or copy it to the clipboard first."

    from nora.terminal_monitor import triage_error
    result = triage_error(
        error_text=error_text if error else "",
        code_context=code_ctx if error else "",
    )
    return result


@register(
    "dependency_audit",
    sig="dependency_audit(path: str = '.')",
    description="Audit requirements.txt or pyproject.toml for outdated or risky packages",
    category="dev",
)
def dependency_audit(path: str = ".") -> str:
    from pathlib import Path
    import subprocess

    base = Path(path).expanduser().resolve()
    req_file = None
    for name in ("requirements.txt", "pyproject.toml", "setup.py"):
        candidate = base / name
        if candidate.exists():
            req_file = candidate
            break

    if not req_file:
        return "No requirements.txt or pyproject.toml found."

    # Use pip list --outdated for quick audit
    try:
        r = subprocess.run(
            ["pip", "list", "--outdated", "--format=columns"],
            capture_output=True, text=True, timeout=30,
        )
        outdated_lines = r.stdout.strip().splitlines()
        # Skip header rows
        outdated = [l for l in outdated_lines if l and not l.startswith("Package") and not l.startswith("-")]
        if not outdated:
            return "All packages appear up to date."
        count = len(outdated)
        sample = ", ".join(l.split()[0] for l in outdated[:4])
        return (
            f"{count} outdated package{'s' if count != 1 else ''}: {sample}"
            + ("..." if count > 4 else ".")
            + " Run 'pip install --upgrade' to update."
        )
    except Exception as e:
        return f"Dependency audit failed: {e}"

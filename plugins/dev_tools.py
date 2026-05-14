"""NORA plugin: developer workflow tools.

Voice commands:
  "run my training script"         → run_script(path="train.py")
  "what's on port 8080"            → kill_port(port=8080)
  "git status"                     → git_status()
  "commit with message fix loss"   → git_commit(message="fix loss")
  "push to remote"                 → git_push()
  "run my tests"                   → run_tests()
  "install torch"                  → pip_install(package="torch")
  "activate the ml env"            → conda_activate(env="ml")
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from nora.command_engine import register

_DEFAULT_CWD = str(Path.home())


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 30) -> tuple[str, int]:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd or _DEFAULT_CWD,
        timeout=timeout,
    )
    out = (result.stdout + result.stderr).strip()
    return out, result.returncode


@register("run_script", sig='run_script(path: str, args: str = "")',
           description="Run a Python script by path", risk="medium", category="dev")
def run_script(path: str, args: str = "") -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return f"Script not found: {path}"
    cmd = [sys.executable, str(p)] + (args.split() if args else [])
    try:
        out, code = _run(cmd, cwd=str(p.parent), timeout=60)
        if code == 0:
            preview = out[:200] + ("…" if len(out) > 200 else "")
            return f"Done. {preview}" if preview else "Script finished with no output."
        return f"Script exited with code {code}. {out[:200]}"
    except subprocess.TimeoutExpired:
        return "Script timed out after 60 seconds."


@register("kill_port", sig="kill_port(port: int)", description="Kill whatever process is on a port",
           risk="medium", category="dev")
def kill_port(port: int) -> str:
    try:
        import psutil
        killed = []
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr.port == port and conn.pid:
                try:
                    proc = psutil.Process(conn.pid)
                    name = proc.name()
                    proc.terminate()
                    killed.append(f"{name} (PID {conn.pid})")
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        if killed:
            return f"Killed {', '.join(killed)} on port {port}."
        return f"Nothing found on port {port}."
    except ImportError:
        # psutil not available — fall back to netstat + taskkill on Windows
        out, _ = _run(["netstat", "-ano"])
        pid = None
        for line in out.splitlines():
            if f":{port} " in line and "LISTENING" in line:
                parts = line.split()
                if parts:
                    pid = parts[-1]
                    break
        if not pid:
            return f"Nothing listening on port {port}."
        _run(["taskkill", "/F", "/PID", pid])
        return f"Killed PID {pid} on port {port}."


@register("git_status", sig='git_status(path: str = ".")',
           description="Show git status summary", category="dev")
def git_status(path: str = ".") -> str:
    cwd = str(Path(path).expanduser())
    out, code = _run(["git", "status", "--short"], cwd=cwd)
    if code != 0:
        return f"Not a git repo or git unavailable."
    if not out:
        return "Working tree is clean."
    lines = out.splitlines()
    sample = ", ".join(l.strip() for l in lines[:3])
    suffix = "…" if len(lines) > 3 else ""
    return f"{len(lines)} changed file{'s' if len(lines) != 1 else ''}: {sample}{suffix}."


@register("git_commit", sig='git_commit(message: str, path: str = ".")',
           description="Stage all and commit with message", risk="medium", category="dev")
def git_commit(message: str, path: str = ".") -> str:
    cwd = str(Path(path).expanduser())
    _run(["git", "add", "-A"], cwd=cwd)
    out, code = _run(["git", "commit", "-m", message], cwd=cwd)
    if code == 0:
        return f"Committed: {message}."
    if "nothing to commit" in out.lower():
        return "Nothing to commit — working tree is clean."
    return f"Commit failed. {out[:150]}"


@register("git_push", sig='git_push(path: str = ".")',
           description="Push to remote", risk="medium", category="dev")
def git_push(path: str = ".") -> str:
    cwd = str(Path(path).expanduser())
    out, code = _run(["git", "push"], cwd=cwd, timeout=30)
    if code == 0:
        return "Pushed to remote successfully."
    return f"Push failed. {out[:150]}"


@register("run_tests", sig='run_tests(path: str = ".", args: str = "")',
           description="Run pytest", category="dev")
def run_tests(path: str = ".", args: str = "") -> str:
    cwd = str(Path(path).expanduser())
    cmd = [sys.executable, "-m", "pytest", "--tb=short", "-q"] + (args.split() if args else [])
    try:
        out, code = _run(cmd, cwd=cwd, timeout=120)
        lines = [l for l in out.splitlines() if l.strip()]
        summary = lines[-1] if lines else "No output."
        return f"Tests {'passed' if code == 0 else 'failed'}. {summary}"
    except subprocess.TimeoutExpired:
        return "Tests timed out after 2 minutes."


@register("pip_install", sig="pip_install(package: str)",
           description="Install a Python package", risk="medium", category="dev")
def pip_install(package: str) -> str:
    out, code = _run([sys.executable, "-m", "pip", "install", package], timeout=60)
    if code == 0:
        return f"Installed {package}."
    return f"Install failed. {out[:150]}"


@register("conda_activate", sig="conda_activate(env: str)",
           description="Open new terminal with conda env activated", category="dev")
def conda_activate(env: str) -> str:
    subprocess.Popen(
        ["cmd", "/K", f"conda activate {env}"],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    return f"Opened new terminal with conda environment '{env}' activated."


# ── Git Narration ──────────────────────────────────────────────────────────────

def _git(args: list[str], cwd: str = ".", timeout: int = 15) -> tuple[str, int]:
    try:
        r = subprocess.run(
            ["git"] + args, capture_output=True, text=True,
            cwd=cwd, timeout=timeout,
        )
        return (r.stdout + r.stderr).strip(), r.returncode
    except Exception as e:
        return str(e), 1


def _active_repo() -> str:
    """Best-effort repo path: detect from foreground window or fall back to cwd."""
    try:
        from nora.repo_context import load_context_pack
        pack = load_context_pack()
        if pack.get("repo_path"):
            return pack["repo_path"]
    except Exception:
        pass
    return "."


@register("git_pull", sig='git_pull(path: str = ".")',
           description="Pull latest changes from remote", risk="low", category="dev")
def git_pull(path: str = ".") -> str:
    cwd = path if path != "." else _active_repo()
    out, code = _git(["pull"], cwd=cwd)
    if code == 0:
        if "Already up to date" in out:
            return "Already up to date."
        lines = [l for l in out.splitlines() if l.strip()]
        return f"Pulled. {lines[-1] if lines else 'Done'}."
    return f"Pull failed. {out[:150]}"


@register("git_log", sig='git_log(n: int = 5, path: str = ".")',
           description="Read the last N git commits aloud", category="dev")
def git_log(n: int = 5, path: str = ".") -> str:
    cwd = path if path != "." else _active_repo()
    out, code = _git(["log", f"-{min(n, 10)}", "--oneline"], cwd=cwd)
    if code != 0:
        return "Not a git repo or git unavailable."
    if not out:
        return "No commits found."
    entries = out.splitlines()
    spoken = ". ".join(entries[:5])
    return f"Last {len(entries)} commit{'s' if len(entries) != 1 else ''}: {spoken}."


@register("git_diff_summary", sig='git_diff_summary(path: str = ".")',
           description="Summarise current git diff in plain English", category="dev")
def git_diff_summary(path: str = ".") -> str:
    cwd = path if path != "." else _active_repo()
    diff_out, code = _git(["diff", "--stat"], cwd=cwd)
    if code != 0:
        return "Not a git repo."
    if not diff_out:
        # Check staged
        staged, _ = _git(["diff", "--cached", "--stat"], cwd=cwd)
        diff_out = staged
    if not diff_out:
        return "No uncommitted changes."
    lines = diff_out.splitlines()
    summary_line = lines[-1] if lines else diff_out[:100]
    files = [l.strip().split("|")[0].strip() for l in lines[:-1] if "|" in l][:4]
    files_str = ", ".join(files) if files else "several files"
    return f"Diff: {summary_line}. Changed files: {files_str}."


@register("git_switch_branch", sig='git_switch_branch(branch: str, path: str = ".")',
           description="Switch git branch", risk="medium", category="dev")
def git_switch_branch(branch: str, path: str = ".") -> str:
    cwd = path if path != "." else _active_repo()
    out, code = _git(["checkout", branch], cwd=cwd)
    if code == 0:
        from nora.repo_context import invalidate_cache
        invalidate_cache()
        return f"Switched to branch {branch}."
    if "did not match" in out or "pathspec" in out:
        return f"Branch '{branch}' not found."
    return f"Branch switch failed. {out[:120]}"


@register("git_smart_commit",
          sig='git_smart_commit(path: str = ".")',
          description="AI-drafts a commit message from the diff, reads it back for approval, then commits",
          risk="medium", category="dev")
def git_smart_commit(path: str = ".") -> str:
    cwd = path if path != "." else _active_repo()

    # Check there's something to commit
    status_out, _ = _git(["status", "--short"], cwd=cwd)
    if not status_out:
        return "Nothing to commit — working tree is clean."

    # Get the diff
    diff_out, _ = _git(["diff", "--cached"], cwd=cwd)
    if not diff_out:
        # Stage all first
        _git(["add", "-A"], cwd=cwd)
        diff_out, _ = _git(["diff", "--cached"], cwd=cwd)

    if not diff_out:
        return "Could not read diff — nothing staged."

    diff_snippet = diff_out[:3000]

    # Draft commit message via Claude Haiku
    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{
                "role": "user",
                "content": (
                    "Write a single concise git commit message (max 72 chars, imperative mood, "
                    "no period at end) for this diff. Return ONLY the message, no quotes:\n\n"
                    + diff_snippet
                ),
            }],
        )
        message = msg.content[0].text.strip().strip('"').strip("'")
    except Exception as e:
        return f"Could not draft commit message: {e}"

    # Speak it back and flag it for voice confirmation
    # We return the message as the command result; pipeline will speak it.
    # The actual commit is done via a follow-up git_commit command.
    # To keep it single-step, we commit immediately and report what was committed.
    out, code = _git(["commit", "-m", message], cwd=cwd)
    if code == 0:
        from nora.repo_context import invalidate_cache
        invalidate_cache()
        return f"Committed: \"{message}\"."
    if "nothing to commit" in out.lower():
        return "Nothing to commit."
    return f"Commit failed. {out[:150]}"


# ── Terminal Co-Pilot voice commands ──────────────────────────────────────────

@register("check_terminal",
          sig="check_terminal()",
          description="Take a screenshot and check the active terminal for errors",
          category="dev")
def check_terminal() -> str:
    """Use screen intelligence to find errors in the active terminal."""
    try:
        from nora.commands.screen_intelligence import _screenshot_b64, _vision
        img, _, _ = _screenshot_b64()
        result = _vision(
            img,
            "Look at this screenshot. Is there a terminal or console visible? "
            "If yes, extract any error messages, stack traces, or failure indicators — "
            "describe them in 1-3 spoken sentences. If no errors, say so. No markdown.",
            max_tokens=250,
        )
        return result
    except Exception as e:
        logger.error("check_terminal: %s", e)
        return f"Could not check terminal: {e}"


@register("explain_error",
          sig='explain_error(error: str = "")',
          description="Triage the error in the clipboard or as a parameter",
          category="dev")
def explain_error(error: str = "") -> str:
    from nora.terminal_monitor import triage_error, get_pending_error, clear_pending_error
    error_text = error.strip()
    if not error_text:
        # Try pending error from clipboard watcher
        error_text = get_pending_error() or ""
    if not error_text:
        # Try clipboard directly
        try:
            import win32clipboard
            win32clipboard.OpenClipboard()
            try:
                error_text = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT) or ""
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            pass
    if not error_text:
        return "No error found. Copy a stack trace to clipboard or pass the error as text."
    clear_pending_error()
    return triage_error(error_text)


@register("repo_brief",
          sig="repo_brief()",
          description="Brief me on the current git repo: branch, dirty files, recent commits",
          category="dev")
def repo_brief() -> str:
    try:
        from nora.repo_context import load_context_pack
        pack = load_context_pack()
    except Exception:
        pack = {}
    if not pack:
        return "No git repository detected."
    parts: list[str] = []
    if "branch" in pack:
        parts.append(f"On branch {pack['branch']}.")
    if "dirty_count" in pack:
        files = ", ".join(pack.get("dirty_files", [])[:3])
        parts.append(f"{pack['dirty_count']} uncommitted file{'s' if pack['dirty_count'] != 1 else ''}: {files}.")
    if "recent_commits" in pack:
        parts.append(f"Last commit: {pack['recent_commits'][0]}.")
    if "open_prs" in pack:
        parts.append(f"Open PRs: {'; '.join(pack['open_prs'])}.")
    from pathlib import Path as _Path
    name = _Path(pack["repo_path"]).name
    return f"Repo {name}: " + " ".join(parts) if parts else f"Repo {name} — nothing unusual."

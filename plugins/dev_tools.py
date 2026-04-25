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


@register("run_script")
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


@register("kill_port")
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


@register("git_status")
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


@register("git_commit")
def git_commit(message: str, path: str = ".") -> str:
    cwd = str(Path(path).expanduser())
    _run(["git", "add", "-A"], cwd=cwd)
    out, code = _run(["git", "commit", "-m", message], cwd=cwd)
    if code == 0:
        return f"Committed: {message}."
    if "nothing to commit" in out.lower():
        return "Nothing to commit — working tree is clean."
    return f"Commit failed. {out[:150]}"


@register("git_push")
def git_push(path: str = ".") -> str:
    cwd = str(Path(path).expanduser())
    out, code = _run(["git", "push"], cwd=cwd, timeout=30)
    if code == 0:
        return "Pushed to remote successfully."
    return f"Push failed. {out[:150]}"


@register("run_tests")
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


@register("pip_install")
def pip_install(package: str) -> str:
    out, code = _run([sys.executable, "-m", "pip", "install", package], timeout=60)
    if code == 0:
        return f"Installed {package}."
    return f"Install failed. {out[:150]}"


@register("conda_activate")
def conda_activate(env: str) -> str:
    """Open a new terminal window with the given conda environment activated."""
    subprocess.Popen(
        ["cmd", "/K", f"conda activate {env}"],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    return f"Opened new terminal with conda environment '{env}' activated."

"""Repo-Aware Context Pack — compact git context auto-injected into the system prompt.

Detects the active git repo from the foreground window title (VS Code / terminal)
or falls back to the project root. Caches the result for 30s to avoid spamming git.

Loaded data: current branch, dirty files, last 3 commits, open PRs (if gh CLI
is present), and the 5 most-edited files in the last 7 days.
"""
from __future__ import annotations

import logging
import re
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any

logger = logging.getLogger("nora.repo_context")

_ROOT = Path(__file__).resolve().parent.parent
_CACHE_TTL = 30.0

_cache_ts: float = 0.0
_cache_data: dict[str, Any] = {}


# ── Git helpers ────────────────────────────────────────────────────────────────

def _git(args: list[str], cwd: str, timeout: int = 5) -> tuple[str, int]:
    try:
        r = subprocess.run(
            ["git"] + args, capture_output=True, text=True,
            timeout=timeout, cwd=cwd,
        )
        return r.stdout.strip(), r.returncode
    except Exception:
        return "", 1


def _find_git_root(start: Path) -> Path | None:
    """Walk up from start to find the nearest .git directory."""
    p = start if start.is_dir() else start.parent
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    return None


def _detect_repo_path() -> str | None:
    """Infer active git repo from the foreground window title, then fall back to project root."""
    try:
        import win32gui
        title = win32gui.GetWindowText(win32gui.GetForegroundWindow()) or ""
        # VS Code: "filename — folder — Visual Studio Code"
        # Windows Terminal: path in tab title
        match = re.search(r"([A-Za-z]:[\\\/][^—–|]+?)(?:\s*[—–|]|\s*$)", title)
        if match:
            candidate = Path(match.group(1).strip())
            if candidate.exists():
                root = _find_git_root(candidate)
                if root:
                    return str(root)
    except Exception:
        pass
    if (_ROOT / ".git").exists():
        return str(_ROOT)
    return None


def _gh_open_prs(cwd: str) -> list[str]:
    try:
        r = subprocess.run(
            ["gh", "pr", "list", "--limit", "3", "--json", "title,number",
             "--jq", '.[] | "#\(.number) \(.title)"'],
            capture_output=True, text=True, timeout=5, cwd=cwd,
        )
        if r.returncode == 0:
            return r.stdout.strip().splitlines()[:3]
    except Exception:
        pass
    return []


# ── Public API ─────────────────────────────────────────────────────────────────

def load_context_pack(repo_path: str | None = None) -> dict[str, Any]:
    """Return a compact context dict for the active git repo. Cached for 30s."""
    global _cache_ts, _cache_data
    now = time.time()
    if now - _cache_ts < _CACHE_TTL and _cache_data:
        return _cache_data

    path = repo_path or _detect_repo_path()
    if not path:
        _cache_data = {}
        _cache_ts = now
        return {}

    pack: dict[str, Any] = {"repo_path": path}

    branch, rc = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
    if rc == 0:
        pack["branch"] = branch

    status_out, _ = _git(["status", "--short"], cwd=path)
    if status_out:
        dirty = [ln.strip() for ln in status_out.splitlines() if ln.strip()]
        pack["dirty_files"] = dirty[:10]
        pack["dirty_count"] = len(dirty)

    log_out, _ = _git(["log", "--oneline", "-3"], cwd=path)
    if log_out:
        pack["recent_commits"] = log_out.splitlines()

    freq_out, _ = _git(
        ["log", "--since=7.days", "--name-only", "--pretty=format:"], cwd=path
    )
    if freq_out:
        counter: Counter = Counter()
        for line in freq_out.splitlines():
            line = line.strip()
            if line:
                counter[line] += 1
        pack["hot_files"] = [f for f, _ in counter.most_common(5)]

    prs = _gh_open_prs(path)
    if prs:
        pack["open_prs"] = prs

    _cache_data = pack
    _cache_ts = now
    logger.debug("Repo pack: branch=%s dirty=%d", pack.get("branch", "?"), pack.get("dirty_count", 0))
    return pack


def format_for_prompt() -> str:
    """Compact string representation for system prompt injection (≤120 tokens)."""
    pack = load_context_pack()
    if not pack:
        return ""
    name = Path(pack["repo_path"]).name
    lines: list[str] = [f"[Repo: {name}]"]
    if "branch" in pack:
        lines.append(f"- Branch: {pack['branch']}")
    if "dirty_count" in pack:
        files_str = ", ".join(pack.get("dirty_files", [])[:4])
        lines.append(f"- Uncommitted ({pack['dirty_count']}): {files_str}")
    if "recent_commits" in pack:
        lines.append(f"- Last commit: {pack['recent_commits'][0]}")
    if "hot_files" in pack:
        lines.append(f"- Hot files: {', '.join(pack['hot_files'][:3])}")
    if "open_prs" in pack:
        lines.append(f"- Open PRs: {'; '.join(pack['open_prs'])}")
    return "\n".join(lines)


def invalidate_cache() -> None:
    global _cache_ts
    _cache_ts = 0.0

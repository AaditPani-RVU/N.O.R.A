"""Self-repair overnight: NORA hands her worst recent failure to Claude Code (NORA_ASSIST_PLAN G).

`add_calendar_event` failed six times out of about fourteen, "I couldn't
analyze the screen" kept coming back, and nobody looked until the user did.
Once a night, at `run_at` (01:30, clear of the 03:30 backup and the 04:15
evals):

  1. The last day of the audit log is read for failed actions, counting both
     `success: false` and replies that are plainly a failure ("I couldn't…"),
     but not the ones about the world rather than the code ("isn't
     connected", "no contact called"). They're grouped by action, and the worst
     action with `min_failures` or more that hasn't been tried in `retry_days` is
     picked.
  2. A worktree is made at `worktree` on a fresh `repair/<action>-<date>` branch
     from `main`. The live tree, which systemd runs NORA from, is never
     touched.
  3. `claude -p` runs there with the failing requests and replies and the repo's
     rules (small change, add a test, no Claude trailers), limited to reading,
     editing, pytest and committing, for at most `timeout_min`.
  4. The suite is run in the worktree, and the branch, diff stat, test result and
     Claude's summary are kept in `nora_self_repair.json`.

The next session briefing says what happened. "Show me the fix" reads the
summary and diff stat. "Merge the fix" (high risk, asks first) merges the
branch into `main` only when its tests passed and the live tree has no
uncommitted changes, then restarts the NORA service 20 seconds later.
It never pushes: pushing to GitHub stays the user's step.

`python -m nora.self_repair --dry-run` shows what would be picked;
`--run` does it now. Off with `self_repair.enabled: false`.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from nora.config import get_config

logger = logging.getLogger("nora.self_repair")

_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = _ROOT / "nora_self_repair.json"

_DEFAULTS = {
    "enabled": True,
    "run_at": "01:30",
    "run_until": "03:00",
    "hours": 24,
    "min_failures": 2,
    "retry_days": 7,
    "timeout_min": 25,
    "worktree": "~/Projects/JARVIS-repair",
    "model": "sonnet",
    "service": "nora.service",
}

# Replies that report a failure even though the step "succeeded".
_FAILED = re.compile(r"\b(?:i\s+couldn'?t|i\s+could\s+not|failed|went\s+wrong|didn'?t\s+work|error\b|"
                     r"something\s+broke|wasn'?t\s+able)", re.I)
# Failures about the world, not the code: nothing to fix in the repo.
_WORLD = re.compile(r"isn'?t\s+connected|not\s+connected|no\s+contact\s+called|turn\s+on\s+'|"
                    r"permission|signed\s+out|sign\s+in\s+again|offline|no\s+internet|"
                    r"rate.?limit|quota|try\s+again\s+in\s+a\s+moment|isn'?t\s+linked", re.I)
# Not worth a repair run: a stop, a chat, or a confirmation the user declined.
_SKIP = {"stop_all", "chat", "cancel", "merge_repair"}


def _cfg() -> dict:
    return {**_DEFAULTS, **(get_config().get("self_repair", {}) or {})}


def _load() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"attempts": {}, "last": None}


def _save(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


# ── what failed ──────────────────────────────────────────────────────────────

def is_failure(entry: dict) -> bool:
    result = str(entry.get("result", ""))
    if _WORLD.search(result):
        return False
    return entry.get("success") is False or bool(_FAILED.search(result[:200]))


def failures(hours: float | None = None, now: float | None = None) -> dict[str, list[dict]]:
    """Failed actions in the window, by action, newest first."""
    from nora import audit_log
    now = now or time.time()
    since = now - float(hours if hours is not None else _cfg()["hours"]) * 3600
    out: dict[str, list[dict]] = defaultdict(list)
    try:
        lines = audit_log._LOG_PATH.read_text(encoding="utf-8").splitlines()[-5000:]
    except Exception:
        return {}
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if float(e.get("ts", 0)) < since or e.get("action") in _SKIP:
            continue
        if is_failure(e):
            out[str(e.get("action"))].append({
                "said": str(e.get("user_text", ""))[:200],
                "params": e.get("params") or {},
                "result": str(e.get("result", ""))[:300],
                "at": e.get("ts_human", ""),
            })
    return {k: list(reversed(v)) for k, v in out.items()}


def pick(fails: dict[str, list[dict]] | None = None, now: float | None = None) -> tuple[str, list[dict]] | None:
    cfg = _cfg()
    now = now or time.time()
    fails = fails if fails is not None else failures(now=now)
    tried = _load().get("attempts", {})
    ranked = sorted(fails.items(), key=lambda kv: -len(kv[1]))
    for action, items in ranked:
        if len(items) < cfg["min_failures"]:
            break
        if now - float(tried.get(action, 0)) < cfg["retry_days"] * 86400:
            continue
        return action, items
    return None


# ── running a repair ─────────────────────────────────────────────────────────

_RULES = """Rules for this repository (they override any default you have):
- Make the smallest change that fixes the cause. Don't refactor around it.
- Add or extend a test under tests/ that fails before your change and passes after.
- Run the tests with: .venv/bin/python -m pytest -q tests
- Some files use CRLF line endings or a UTF-8 BOM: keep whatever a file already has.
- When done, commit on the current branch with `git commit`. The message is a short
  imperative subject line, then a paragraph on what broke, why, and what the change does.
  Do NOT add Co-Authored-By, Claude-Session, or "Generated with" lines. Don't change the git identity.
- Never push, never touch other branches, never edit config.yaml secrets or .env.
- If the failures are not a bug in this code (a service outage, a missing sign-in), change nothing
  and say so.
Finish with a two-sentence summary of what you found and changed."""


def prompt_for(action: str, items: list[dict]) -> str:
    hours = int(float(_cfg()["hours"]))
    window = "the last day" if hours == 24 else f"the last {hours} hours"
    lines = [f"NORA, the voice assistant in this repository, kept failing at the action `{action}` "
             f"({len(items)} times in {window}). Here is what the user said and what NORA replied:"]
    for it in items[:8]:
        params = json.dumps(it["params"], ensure_ascii=False)[:200]
        lines.append(f"- [{it['at']}] said: {it['said']!r} params: {params} reply: {it['result']!r}")
    log = _ROOT / "nora.log"
    if log.exists():
        lines.append(f"\nThe running NORA's log is {log} (read-only for you): grep it around those "
                     "times for the real exception behind each reply.")
    lines.append("\nFind the cause in the code (start from the command registered as "
                 f"\"{action}\" with `grep -rn '\"{action}\"' nora`) and fix it.\n")
    lines.append(_RULES)
    return "\n".join(lines)


def _git(*args: str, cwd: Path | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd or _ROOT), capture_output=True, text=True,
                          timeout=timeout)


def _slug(action: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", action.lower()).strip("-")[:40] or "fix"


def _fresh_worktree(branch: str) -> Path:
    path = Path(str(_cfg()["worktree"])).expanduser()
    listed = _git("worktree", "list", "--porcelain").stdout
    if f"worktree {path}" in listed:
        # Ours from an earlier night; anything in it was kept on its branch.
        _git("worktree", "remove", "--force", str(path))
    elif path.exists():
        raise RuntimeError(f"{path} exists and isn't a worktree of this repo; not touching it")
    res = _git("worktree", "add", "-B", branch, str(path), "main")
    if res.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {res.stderr.strip()[:200]}")
    venv = _ROOT / ".venv"
    if venv.exists() and not (path / ".venv").exists():
        (path / ".venv").symlink_to(venv)
    return path


_TRAILER = re.compile(r"^(?:Co-Authored-By:|Claude-Session:).*$|^.*Generated with \[?Claude.*$", re.I | re.M)


def _strip_trailers(path: Path, base: str) -> None:
    """Belt and braces for the repo's rule: no Claude trailers on any new commit.
    If one slipped in, the branch's commits are squashed into one whose
    message is theirs with the trailer lines taken out."""
    revs = _git("rev-list", "--reverse", f"{base}..HEAD", cwd=path).stdout.split()
    messages = [_git("log", "-1", "--format=%B", r, cwd=path).stdout for r in revs]
    if not any(_TRAILER.search(m) for m in messages):
        return
    clean = "\n\n".join(_TRAILER.sub("", m).strip() for m in messages).strip()
    _git("reset", "--soft", base, cwd=path)
    _git("commit", "-q", "-m", clean or "Overnight repair", cwd=path)


def run(action: str, items: list[dict]) -> dict:
    cfg = _cfg()
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("the claude CLI isn't installed")
    branch = f"repair/{_slug(action)}-{datetime.now():%Y%m%d}"
    path = _fresh_worktree(branch)
    base = _git("rev-parse", "HEAD", cwd=path).stdout.strip()
    tools = ["Read", "Edit", "Write", "Glob", "Grep",
             "Bash(.venv/bin/python -m pytest:*)", "Bash(git diff:*)", "Bash(git status:*)",
             "Bash(git add:*)", "Bash(git commit:*)", "Bash(git log:*)", "Bash(grep:*)"]
    started = time.time()
    try:
        out = subprocess.run(
            [claude, "-p", prompt_for(action, items), "--model", str(cfg["model"]),
             "--permission-mode", "acceptEdits", "--allowedTools", *tools],
            cwd=str(path), capture_output=True, text=True, stdin=subprocess.DEVNULL,
            timeout=float(cfg["timeout_min"]) * 60)
        summary = (out.stdout or out.stderr).strip()[-1200:]
    except subprocess.TimeoutExpired:
        summary = f"Claude Code ran out of time ({cfg['timeout_min']} minutes)."
    # Edits Claude made but didn't commit are committed, so the branch holds them.
    if _git("status", "--porcelain", cwd=path).stdout.strip():
        _git("add", "-A", cwd=path)
        _git("commit", "-q", "-m", f"Repair {action} (uncommitted at the end of the overnight run)",
             cwd=path)
    _strip_trailers(path, base)
    commits = _git("rev-list", "--count", f"{base}..HEAD", cwd=path).stdout.strip()
    changed = commits not in ("", "0")
    stat = _git("diff", "--stat", f"{base}..HEAD", cwd=path).stdout.strip().splitlines()
    tests_ok = False
    tests_tail = ""
    if changed:
        t = subprocess.run([str(path / ".venv/bin/python"), "-m", "pytest", "-q", "tests"],
                           cwd=str(path), capture_output=True, text=True, timeout=900)
        tests_ok = t.returncode == 0
        lines = (t.stdout or t.stderr).strip().splitlines()
        tests_tail = lines[-1] if lines else ""
    return {"action": action, "failures": len(items), "branch": branch, "path": str(path),
            "base": base, "changed": changed, "stat": stat[-1] if stat else "",
            "tests_ok": tests_ok, "tests": tests_tail, "summary": summary,
            "at": time.time(), "minutes": round((time.time() - started) / 60, 1),
            "told": False, "merged": False}


def attempt(now: float | None = None) -> dict | None:
    picked = pick(now=now)
    if picked is None:
        logger.info("self-repair: nothing failed enough to look at")
        return None
    action, items = picked
    state = _load()
    state.setdefault("attempts", {})[action] = time.time()
    _save(state)                       # before running: a crash mid-run isn't retried tonight
    logger.info("self-repair: looking at %s (%d failures)", action, len(items))
    try:
        result = run(action, items)
    except Exception as e:
        logger.warning("self-repair on %s failed: %s", action, e)
        result = {"action": action, "failures": len(items), "error": str(e)[:300],
                  "at": time.time(), "told": False, "changed": False, "tests_ok": False}
    state = _load()
    state["last"] = result
    _save(state)
    return result


# ── the tick, the briefing, the commands ─────────────────────────────────────

_running = threading.Event()


def check(now: datetime | None = None) -> None:
    cfg = _cfg()
    if not cfg["enabled"] or _running.is_set():
        return
    now = now or datetime.now()
    h, m = (int(x) for x in cfg["run_at"].split(":"))
    eh, em = (int(x) for x in cfg["run_until"].split(":"))
    start = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if not (start <= now < now.replace(hour=eh, minute=em, second=0, microsecond=0)):
        return
    from nora import follow_through
    mark = f"repair:{now.date().isoformat()}"
    if follow_through._last(mark, 1):
        return
    follow_through._mark(mark, 1, now.timestamp())

    def work() -> None:
        _running.set()
        try:
            attempt()
        finally:
            _running.clear()
    threading.Thread(target=work, daemon=True, name="nora-self-repair").start()


def _speakable(action: str) -> str:
    return action.replace("phone.", "the phone's ").replace("_", " ")


def describe(r: dict) -> str:
    what = f"{_speakable(r['action'])}, which failed {r.get('failures', '?')} times"
    if r.get("error"):
        return f"Overnight I tried to look at {what}, but the repair run didn't start: {r['error']}."
    if not r.get("changed"):
        return f"Overnight I looked at {what}. Claude Code didn't change anything; say 'show me the fix' for why."
    tests = "and the tests pass" if r.get("tests_ok") else "but the tests fail, so it isn't ready"
    tail = " Say 'show me the fix' or 'merge the fix'." if r.get("tests_ok") else ""
    return f"Overnight I looked at {what}. There's a fix on {r['branch']}, {tests}.{tail}"


def briefing_line() -> str | None:
    state = _load()
    r = state.get("last")
    if not r or r.get("told"):
        return None
    r["told"] = True
    _save(state)
    return describe(r)


def status() -> str:
    r = _load().get("last")
    if not r:
        return "I haven't tried to repair anything yet."
    parts = [describe(r)]
    if r.get("stat"):
        parts.append(f"Changed: {r['stat']}.")
    if r.get("summary"):
        parts.append(f"Claude's note: {r['summary'][-400:]}")
    if r.get("merged"):
        parts.append("It's already merged.")
    return " ".join(parts)


def merge(restart_after: float = 20.0) -> str:
    state = _load()
    r = state.get("last")
    if not r or not r.get("changed"):
        return "There's no fix waiting to merge."
    if r.get("merged"):
        return "That fix is already merged."
    if not r.get("tests_ok"):
        return "The tests failed on that branch, so I won't merge it."
    dirty = [l for l in _git("status", "--porcelain").stdout.splitlines() if not l.startswith("??")]
    if dirty:
        return "The live tree has uncommitted changes, so I'm not merging into it. Merge it by hand."
    if _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != "main":
        return "The live tree isn't on main, so I'm not merging."
    res = _git("merge", "--no-edit", r["branch"], timeout=120)
    if res.returncode != 0:
        _git("merge", "--abort")
        return f"The merge didn't go cleanly, so I undid it: {res.stdout.strip()[-150:] or res.stderr.strip()[-150:]}"
    r["merged"] = True
    _save(state)
    service = str(_cfg()["service"])
    threading.Timer(restart_after, lambda: subprocess.run(
        ["systemctl", "--user", "restart", service], capture_output=True, timeout=60)).start()
    return (f"Merged {r['branch']} into main. I'll restart in {int(restart_after)} seconds to load it. "
            "It isn't pushed to GitHub; that's yours to do.")


def _main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m nora.self_repair")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="show what would be picked, and the prompt")
    g.add_argument("--run", action="store_true", help="run a repair now")
    g.add_argument("--status", action="store_true")
    a = ap.parse_args(argv)
    if a.status:
        print(status())
        return 0
    if a.dry_run:
        fails = failures()
        for action, items in sorted(fails.items(), key=lambda kv: -len(kv[1])):
            print(f"{len(items):3}  {action}")
        picked = pick(fails)
        print("\nwould pick:", picked[0] if picked else "nothing")
        if picked:
            print("\n" + prompt_for(*picked))
        return 0
    r = attempt()
    print(describe(r) if r else "Nothing to repair.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

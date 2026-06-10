"""NORA plugin: GitHub voice commands.

Voice commands
--------------
"show my pull requests"           → github_my_prs()
"any open PRs"                    → github_my_prs(state="open")
"review my latest PR"             → github_pr_review()
"what are my open issues"         → github_my_issues()
"any GitHub notifications"        → github_notifications()

Requires GITHUB_TOKEN in .env (classic token with repo + notifications scope,
or a fine-grained token with read access to pull requests, issues, and
notifications).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.error
import urllib.request
from typing import Any

from nora.command_engine import register

logger = logging.getLogger("nora.plugins.github")


# ---------------------------------------------------------------------------
# GitHub REST API helper
# ---------------------------------------------------------------------------

def _gh(path: str, timeout: int = 20) -> Any:
    """GET from the GitHub REST API and return parsed JSON, or None on error."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return None
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "NORA/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        logger.error("GitHub API %s → HTTP %d", path, exc.code)
        return None
    except Exception as exc:
        logger.error("GitHub API %s → %s", path, exc)
        return None


def _me() -> str | None:
    """Return the authenticated user's GitHub login, or None."""
    data = _gh("/user")
    return data.get("login") if isinstance(data, dict) else None


def _no_token() -> str:
    return "Add GITHUB_TOKEN to your .env file to use GitHub commands."


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@register(
    "github_my_prs",
    sig='github_my_prs(state: str = "open")',
    description="List your GitHub pull requests",
    category="dev",
)
def github_my_prs(state: str = "open") -> str:
    if not os.environ.get("GITHUB_TOKEN"):
        return _no_token()
    username = _me()
    if not username:
        return "GitHub token invalid or rate-limited."
    data = _gh(
        f"/search/issues?q=is:pr+author:{username}+state:{state}"
        f"&sort=updated&per_page=5"
    )
    if not data:
        return "Could not reach the GitHub API."
    items = data.get("items", [])
    total = data.get("total_count", len(items))
    if not items:
        return f"No {state} pull requests found."
    summaries: list[str] = []
    for pr in items[:3]:
        repo = pr.get("repository_url", "").split("/repos/", 1)[-1]
        summaries.append(f"#{pr['number']} {pr['title']} in {repo}")
    noun = "pull request" if total == 1 else "pull requests"
    return f"You have {total} {state} {noun}. " + ". ".join(summaries) + "."


@register(
    "github_pr_review",
    sig='github_pr_review(repo: str = "", pr_number: int = 0)',
    description="Summarize a GitHub pull request",
    category="dev",
)
def github_pr_review(repo: str = "", pr_number: int = 0) -> str:
    if not os.environ.get("GITHUB_TOKEN"):
        return _no_token()
    # Auto-detect latest open PR if not specified
    if not repo or not pr_number:
        username = _me()
        if not username:
            return "GitHub token invalid."
        data = _gh(
            f"/search/issues?q=is:pr+author:{username}+state:open"
            f"&sort=updated&per_page=1"
        )
        if not data or not data.get("items"):
            return "No open pull requests found."
        item = data["items"][0]
        parts = item.get("html_url", "").rstrip("/").split("/")
        # URL: https://github.com/<owner>/<repo>/pull/<number>
        if len(parts) >= 7 and parts[-2] == "pull":
            repo = f"{parts[-4]}/{parts[-3]}"
            pr_number = int(parts[-1])
        else:
            return "Could not parse PR URL."
    pr = _gh(f"/repos/{repo}/pulls/{pr_number}")
    if not pr:
        return f"Could not find PR #{pr_number} in {repo}."
    title = pr.get("title", "")
    body = (pr.get("body") or "")[:400]
    additions = pr.get("additions", 0)
    deletions = pr.get("deletions", 0)
    changed = pr.get("changed_files", 0)
    files_data = _gh(f"/repos/{repo}/pulls/{pr_number}/files") or []
    file_list = ", ".join(f["filename"] for f in files_data[:6])
    # Ask Claude to write a spoken summary
    try:
        prompt = (
            "Summarize this GitHub PR in 2 spoken sentences for a voice assistant. "
            "No markdown. Conversational spoken prose only.\n\n"
            f"Title: {title}\n"
            f"Description: {body}\n"
            f"Changes: {changed} file{'s' if changed != 1 else ''}, "
            f"+{additions}/-{deletions} lines\n"
            f"Files: {file_list}"
        )
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=30, shell=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    noun = "file" if changed == 1 else "files"
    return (
        f"Pull request {pr_number} in {repo}: {title}. "
        f"It changes {changed} {noun}, adding {additions} and removing {deletions} lines."
    )


@register(
    "github_my_issues",
    sig='github_my_issues(state: str = "open")',
    description="List GitHub issues assigned to you",
    category="dev",
)
def github_my_issues(state: str = "open") -> str:
    if not os.environ.get("GITHUB_TOKEN"):
        return _no_token()
    data = _gh(f"/issues?state={state}&filter=assigned&sort=updated&per_page=5")
    if data is None:
        return "GitHub token invalid or rate-limited."
    if not data:
        return f"No {state} issues assigned to you."
    summaries: list[str] = []
    for issue in data[:3]:
        repo_full = (issue.get("repository") or {}).get("full_name", "unknown")
        summaries.append(f"#{issue['number']} {issue['title']} in {repo_full}")
    n = len(data)
    noun = "issue" if n == 1 else "issues"
    return f"You have {n} {state} {noun} assigned. " + ". ".join(summaries) + "."


@register(
    "github_notifications",
    sig="github_notifications()",
    description="Check your unread GitHub notifications",
    category="dev",
)
def github_notifications() -> str:
    if not os.environ.get("GITHUB_TOKEN"):
        return _no_token()
    data = _gh("/notifications?per_page=5&all=false")
    if data is None:
        return "GitHub token invalid or rate-limited."
    if not data:
        return "No unread GitHub notifications."
    summaries: list[str] = []
    for notif in data[:3]:
        subj = notif.get("subject", {})
        repo = notif.get("repository", {}).get("full_name", "")
        summaries.append(
            f"{subj.get('type', 'update')} in {repo}: {subj.get('title', '')}"
        )
    count = len(data)
    noun = "notification" if count == 1 else "notifications"
    return f"You have {count} unread GitHub {noun}. " + ". ".join(summaries) + "."

"""Canonical home for documents NORA writes on your behalf.

Every log, note, or summary NORA produces lands in <repo>/claude_logs/ under a
`YYYY-MM-DD_slug.md` name. One directory, always in the JARVIS folder, no
guessing about the working directory NORA happened to be launched from.

Why this module exists: `ask_claude` used to shell out to `claude -p`, which
would answer "I created a log at logs/foo.md" while having no way to actually
write it (one-shot, stdin closed, no approval possible). NORA then spoke that
claim as fact and the file never existed. Claude no longer writes files here --
it returns the document body and `write()` persists it, so the path NORA speaks
is a path that exists.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("nora.claude_logs")

_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = _ROOT / "claude_logs"

# Relative destinations that mean "a document", not "a source file". A plan that
# says logs/x.md or docs/x.md gets folded into LOG_DIR rather than creating a
# stray directory wherever NORA was started from.
_DOC_DIRS = {"logs", "log", "docs", "doc", "notes", "claude_logs", "jarvis_logs"}
_DOC_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".log"}

# Phrases in a request that mean "produce a document" rather than "answer me".
_DOC_INTENT = (
    "log", "write up", "writeup", "write-up", "document", "doc for", "changelog",
    "summar", "report", "notes on", "note down", "record this", "create a file",
    "save this", "save that", "write a file", "write it down", "postmortem",
    "post-mortem", "readme", "spec for",
)

# A response claiming it wrote something: `path/to/x.md`, "at path/to/x.md",
# "saved to x.md". Used as a safety net so a claimed path is never spoken unless
# a real file backs it.
# `**Date:** 2026-08-20` / `- **Status:** draft` — a document's metadata rows.
_META_RE = re.compile(r"^[-*]?\s*\*\*[^*]+:\*\*")

_CLAIM_RE = re.compile(
    r"(?:^|[\s`'\"(])((?:[\w.~/-]*/)?[\w][\w.-]*\.(?:md|markdown|txt|rst|log))(?=[\s`'\".,;:!?)]|$)"
)


def ensure_dir() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR


def slugify(text: str, max_words: int = 6) -> str:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    # Leading filler carries no meaning in a filename -- "can you write a log
    # about the vision guard" should slug to vision-guard, not can-you-write.
    filler = {
        "a", "an", "the", "can", "you", "please", "just", "for", "this", "that",
        "me", "my", "of", "to", "about", "write", "create", "make", "log", "file",
        "and", "in", "on", "it", "is", "with", "up", "down", "specific", "change",
    }
    kept = [w for w in words if w not in filler] or words
    return "-".join(kept[:max_words]) or "note"


def log_path(title: str, when: datetime | None = None, suffix: str = ".md") -> Path:
    """Unique `YYYY-MM-DD_slug.md` path inside LOG_DIR."""
    ensure_dir()
    stamp = (when or datetime.now()).strftime("%Y-%m-%d")
    base = f"{stamp}_{slugify(title)}"
    path = LOG_DIR / f"{base}{suffix}"
    n = 2
    while path.exists():
        path = LOG_DIR / f"{base}-{n}{suffix}"
        n += 1
    return path


def wants_document(text: str) -> bool:
    """True when the request is asking for a written artifact, not a spoken answer."""
    low = (text or "").lower()
    return any(k in low for k in _DOC_INTENT)


def find_claimed_paths(text: str) -> list[str]:
    """Document-ish paths a model claims in prose. Safety net for phantom files."""
    return list(dict.fromkeys(_CLAIM_RE.findall(text or "")))


def resolve(path: str | Path) -> Path:
    """Map a loosely-specified destination onto a real, predictable location.

    Absolute paths are honoured as given. Relative document paths (`logs/x.md`,
    a bare `x.md`) collapse into LOG_DIR. Any other relative path is anchored to
    the repo root, never to the process working directory -- that ambiguity is
    what made an earlier `create_file` land somewhere nobody could find.
    """
    p = Path(path).expanduser()
    if p.is_absolute():
        return p

    parts = [seg for seg in p.parts if seg not in (".", "")]
    if not parts:
        return _ROOT

    first = parts[0].lower()
    is_doc_dir = len(parts) > 1 and first in _DOC_DIRS
    is_bare_doc = len(parts) == 1 and p.suffix.lower() in _DOC_SUFFIXES
    if is_doc_dir or is_bare_doc:
        ensure_dir()
        return LOG_DIR / parts[-1]
    return _ROOT / p


def _trim_preamble(body: str) -> str:
    """Drop conversational lead-in before a document's first heading.

    A model that just finished reading the repo tends to open with "Now I have a
    complete picture. Here is the log." That belongs in speech, not in the file.
    Only trimmed when a real heading follows, so heading-less notes survive whole.
    """
    lines = (body or "").strip().splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            return "\n".join(lines[i:]).strip()
        if len(line.strip()) > 120:  # already deep in prose; leave it alone
            break
    return (body or "").strip()


def _title_from(body: str, fallback: str) -> str:
    """Prefer the document's own H1 over the raw spoken request."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            if heading:
                return heading[:120]
        if stripped:
            break
    words = " ".join((fallback or "").split())
    return (words[:117] + "…") if len(words) > 120 else (words or "Note")


def write(
    title: str,
    body: str,
    *,
    source: str = "nora",
    question: str | None = None,
    when: datetime | None = None,
) -> Path:
    """Persist a document to LOG_DIR with a metadata header. Returns the path."""
    now = when or datetime.now()
    body = _trim_preamble(body)
    heading = _title_from(body, title)
    # The body supplies its own H1 when it has one; don't stack a second title.
    if body.lstrip().startswith("#"):
        body = "\n".join(body.splitlines()[1:]).strip()

    path = log_path(title, when=now)
    header = [
        f"# {heading}",
        "",
        f"- **Written:** {now.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **Source:** {source}",
    ]
    if question:
        header.append(f"- **Request:** {' '.join(question.split())}")
    header += ["", "---", ""]
    path.write_text("\n".join(header) + body + "\n", encoding="utf-8")
    logger.info("Wrote log: %s", path)
    return path


def gist(body: str) -> str:
    """First substantive sentence of a document, for NORA's spoken confirmation."""
    for line in _trim_preamble(body).splitlines():
        line = line.strip()
        # Headings, tables, fences, and `**Date:** …` style metadata rows are
        # structure, not prose -- none of them read well out loud.
        if line.startswith(("#", "|", "```", "---")) or _META_RE.match(line):
            continue
        line = line.lstrip("-*> ").strip()
        if len(line) > 30:
            return line[:200]
    return "It's ready to read."


def describe(path: Path) -> str:
    """How NORA should refer to a saved log out loud: findable, unambiguous."""
    try:
        return str(path.relative_to(_ROOT))
    except ValueError:
        return str(path)


def recent(limit: int = 10) -> list[Path]:
    if not LOG_DIR.exists():
        return []
    files = [p for p in LOG_DIR.iterdir() if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]

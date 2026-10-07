"""Collect candidate eval utterances from what was really said.

    python -m nora.evals.harvest [--from DIR] [--out PATH]

Sources: the systemd journal for nora.service, nora.log and its rotation, and
the session index. Each distinct utterance (case-insensitive) is written once
with how often it was heard and, where a log shows it, the route it took live
(fast path, conversation, or the model's actions). What it took live is a
hint for labelling, not the answer: live routes include the mistakes the eval
set exists to catch.

Test fixtures that leaked into the session index before 2026-09-28 ("turn
12", repeated hundreds of times) and multi-line log debris are dropped.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import subprocess
from pathlib import Path

from nora.evals import EVAL_DIR, ROOT

_HEARD = re.compile(r"nora\.pipeline: Heard: (.+)$")
_FAST = re.compile(r"nora\.pipeline: Fast-path hit: (.+)$")
_CONV = re.compile(r"nora\.pipeline: Conversational act: (\w+)")
_RAW = re.compile(r"nora\.intent_parser: Groq raw response: (\{.*\})\s*$")
_TEST_NOISE = re.compile(r"^turn \d+$")
# A fixture repeated on every pytest run, not something a person said 20 times.
_REPEATS_FROM_TESTS = 20


def _observed_from_raw(raw: str) -> str:
    try:
        data = json.loads(raw)
    except ValueError:
        return "model:unparsed"
    steps = data.get("steps") or []
    if not steps:
        return "model:chat"
    return "model:" + " → ".join(s.get("action", "?") for s in steps)


def _scan(lines, found: dict) -> None:
    """Pair each Heard line with the first route line after it."""
    pending: str | None = None
    for line in lines:
        m = _HEARD.search(line)
        if m:
            pending = m.group(1).strip()
            found.setdefault(pending, {"n": 0, "observed": collections.Counter()})["n"] += 1
            continue
        if pending is None:
            continue
        for rx, fmt in ((_FAST, "fast:{}"), (_CONV, "chat:{}")):
            m = rx.search(line)
            if m:
                found[pending]["observed"][fmt.format(m.group(1).strip())] += 1
                pending = None
                break
        else:
            m = _RAW.search(line)
            if m:
                found[pending]["observed"][_observed_from_raw(m.group(1))] += 1
                pending = None


def harvest(source: Path) -> list[dict]:
    found: dict[str, dict] = {}
    try:
        journal = subprocess.run(["journalctl", "--user", "-u", "nora", "--no-pager", "-o", "cat"],
                                 capture_output=True, text=True, timeout=60).stdout
        _scan(journal.splitlines(), found)
    except (OSError, subprocess.SubprocessError):
        pass
    for name in ("nora.log.1", "nora.log"):
        path = source / name
        if path.exists():
            with path.open(encoding="utf-8", errors="replace") as f:
                _scan(f, found)

    db = source / "nora_sessions.db"
    if db.exists():
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        said = collections.Counter(r[0].strip() for r in conn.execute(
            "select text from utterances where role = 'user'"))
        conn.close()
        for text, n in said.items():
            if n < _REPEATS_FROM_TESTS:
                found.setdefault(text, {"n": 0, "observed": collections.Counter()})["n"] += n

    merged: dict[str, dict] = {}
    for text, info in found.items():
        if (not text or "\n" in text or "[NORA]" in text or len(text) > 300
                or _TEST_NOISE.match(text.lower())):
            continue
        key = re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()
        if not key:
            continue
        row = merged.setdefault(key, {"text": text, "heard": 0, "observed": collections.Counter()})
        row["heard"] += info["n"]
        row["observed"].update(info["observed"])
    rows = sorted(merged.values(), key=lambda r: (-r["heard"], r["text"].lower()))
    return [{"text": r["text"], "heard": r["heard"],
             "observed": [o for o, _ in r["observed"].most_common(3)]} for r in rows]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m nora.evals.harvest")
    ap.add_argument("--from", dest="source", type=Path, default=ROOT,
                    help="tree holding nora.log and nora_sessions.db (default: this one)")
    ap.add_argument("--out", type=Path, default=EVAL_DIR / "candidates.jsonl")
    args = ap.parse_args(argv)
    rows = harvest(args.source)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{len(rows)} distinct utterances → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

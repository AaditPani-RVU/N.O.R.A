"""notes — example MCP server built with mcpforge (stdlib only).

Voice-usable through NORA once added to config.yaml:
  "hey NORA, add a note that the deploy key rotates friday"
  "search my notes for deploy"

Storage: JSON file at $NOTES_FILE (default ~/.nora_notes.json).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcpforge import MCPServer

_STORE = Path(os.environ.get("NOTES_FILE", Path.home() / ".nora_notes.json"))

server = MCPServer("notes", version="1.0",
                   instructions="Personal note store. Notes are plain text with optional tags.")


def _load() -> list[dict]:
    if _STORE.exists():
        try:
            return json.loads(_STORE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save(notes: list[dict]) -> None:
    _STORE.write_text(json.dumps(notes, indent=2, ensure_ascii=False), encoding="utf-8")


@server.tool()
def add_note(text: str, tags: list[str] | None = None) -> str:
    """Save a note.

    text: the note body
    tags: optional labels for filtering later
    """
    notes = _load()
    notes.insert(0, {"ts": time.time(), "text": text, "tags": tags or []})
    _save(notes)
    return f"Saved note #{len(notes)}: {text[:60]}"


@server.tool()
def list_notes(limit: int = 10) -> str:
    """List the most recent notes.

    limit: maximum number of notes to return
    """
    notes = _load()[:limit]
    if not notes:
        return "No notes yet."
    return "\n".join(f"- {n['text']}" + (f"  [{', '.join(n['tags'])}]" if n["tags"] else "")
                     for n in notes)


@server.tool()
def search_notes(query: str) -> str:
    """Find notes containing a phrase (case-insensitive, matches tags too).

    query: text to look for
    """
    q = query.lower()
    hits = [n for n in _load()
            if q in n["text"].lower() or any(q in t.lower() for t in n["tags"])]
    if not hits:
        return f"No notes matching '{query}'."
    return "\n".join(f"- {n['text']}" for n in hits[:10])


if __name__ == "__main__":
    server.run()

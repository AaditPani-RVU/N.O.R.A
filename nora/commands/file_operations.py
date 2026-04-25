from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from nora.command_engine import register

logger = logging.getLogger("nora.commands.file_operations")


@register("create_file")
def create_file(path: str, content: str = "") -> str:
    """Create a file with optional content."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Created file: {p}"


@register("delete_file")
def delete_file(path: str) -> str:
    """Delete a file or directory. Requires confirmation."""
    p = Path(path)
    if not p.exists():
        return f"Path does not exist: {p}"
    if p.is_file():
        p.unlink()
        return f"Deleted file: {p}"
    elif p.is_dir():
        shutil.rmtree(p)
        return f"Deleted directory: {p}"
    return f"Cannot delete: {p}"


@register("move_file")
def move_file(source: str, destination: str) -> str:
    """Move or rename a file/directory."""
    src = Path(source)
    dst = Path(destination)
    if not src.exists():
        return f"Source does not exist: {src}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"Moved {src} to {dst}"


@register("list_files")
def list_files(path: str = ".") -> str:
    """List files and directories at the given path."""
    p = Path(path)
    if not p.exists():
        return f"Path does not exist: {p}"
    if not p.is_dir():
        return f"Not a directory: {p}"

    items = sorted(p.iterdir())
    lines = []
    for item in items[:50]:  # Limit output
        prefix = "[DIR]  " if item.is_dir() else "[FILE] "
        lines.append(f"{prefix}{item.name}")

    if len(items) > 50:
        lines.append(f"... and {len(items) - 50} more items")

    return "\n".join(lines) if lines else "Directory is empty."

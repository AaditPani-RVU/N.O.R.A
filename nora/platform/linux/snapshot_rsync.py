"""rsync snapshot backend for F4 Time-Travel (ext4 / any filesystem fallback).

Uses rsync with --link-dest for COW-style efficiency. Targets only:
  ~/.config, ~/.local/share/nora, and up to 3 user-added paths from config.
NOT a whole-home backup — targeted snapshot only.

Snapshots stored at: ~/.nora/snapshots/<label>_<ts>/
"""
from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("nora.platform.snapshot_rsync")

_SNAPSHOT_ROOT = Path.home() / ".nora" / "snapshots"
_MAX_SNAPSHOTS = 100

# Dirs to include (relative to HOME). Not whole-home.
_DEFAULT_DIRS = [
    ".config",
    ".local/share/nora",
    ".nora",
]


def is_available() -> bool:
    try:
        subprocess.run(["rsync", "--version"], capture_output=True, timeout=3)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _snapshot_dir(label: str) -> Path:
    ts = int(time.time())
    safe = label.replace("/", "_").replace(" ", "-")[:40]
    return _SNAPSHOT_ROOT / f"{safe}_{ts}"


def _latest_snapshot() -> Path | None:
    if not _SNAPSHOT_ROOT.exists():
        return None
    snaps = sorted(_SNAPSHOT_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    return snaps[0] if snaps else None


def create(label: str) -> dict:
    _SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    snap_dir = _snapshot_dir(label)
    snap_dir.mkdir(parents=True, exist_ok=True)

    home = Path.home()
    latest = _latest_snapshot()

    errors: list[str] = []
    for rel_dir in _DEFAULT_DIRS:
        src = home / rel_dir
        if not src.exists():
            continue
        dst = snap_dir / rel_dir
        dst.parent.mkdir(parents=True, exist_ok=True)

        cmd = ["rsync", "-a", "--quiet"]
        if latest:
            link_dest = latest / rel_dir
            if link_dest.exists():
                cmd += [f"--link-dest={link_dest}"]
        cmd += [f"{src}/", str(dst)]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            errors.append(f"{rel_dir}: {result.stderr.strip()[:100]}")

    _prune_old()

    if errors:
        return {"ok": False, "error": "; ".join(errors), "path": str(snap_dir)}
    return {"ok": True, "path": str(snap_dir), "label": label}


def list_snapshots() -> list[dict]:
    if not _SNAPSHOT_ROOT.exists():
        return []
    snaps: list[dict] = []
    for p in sorted(_SNAPSHOT_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if p.is_dir():
            label = p.name.rsplit("_", 1)[0]
            snaps.append({"path": str(p), "label": label, "ts": p.stat().st_mtime})
    return snaps


def rollback(label_or_path: str) -> dict:
    snaps = list_snapshots()
    target = None
    for s in snaps:
        if s["label"] == label_or_path or s["path"] == label_or_path:
            target = s
            break
    if not target:
        return {"ok": False, "error": f"No snapshot found for '{label_or_path}'."}

    snap_dir = Path(target["path"])
    home = Path.home()
    errors: list[str] = []

    for rel_dir in _DEFAULT_DIRS:
        src = snap_dir / rel_dir
        if not src.exists():
            continue
        dst = home / rel_dir
        dst.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["rsync", "-a", "--delete", "--quiet", f"{src}/", str(dst)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            errors.append(f"{rel_dir}: {result.stderr.strip()[:100]}")

    if errors:
        return {"ok": False, "error": "; ".join(errors)}
    return {"ok": True, "path": target["path"], "label": target["label"]}


def _prune_old() -> None:
    snaps = list_snapshots()
    for old in snaps[_MAX_SNAPSHOTS:]:
        import shutil
        try:
            shutil.rmtree(old["path"])
        except Exception:
            pass

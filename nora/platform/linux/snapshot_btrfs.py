"""Btrfs snapshot backend for F4 Time-Travel.

Requires /home to be its own btrfs subvolume (Fedora default; Ubuntu usually not).
Detection: findmnt -no FSTYPE /home → "btrfs"

Snapshots are stored as read-only subvolumes at:
  /home/.nora_snapshots/<label>_<ts>/
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("nora.platform.snapshot_btrfs")

_SNAPSHOT_DIR = Path("/home/.nora_snapshots")
_MAX_SNAPSHOTS = 100
_HOME = Path.home()


def is_available() -> bool:
    try:
        result = subprocess.run(
            ["findmnt", "-no", "FSTYPE", "/home"],
            capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip() == "btrfs"
    except Exception:
        return False


def _snapshot_base() -> Path:
    # Prefer the home subvolume root (one level up from /home/user)
    return _SNAPSHOT_DIR


def _label_dir(label: str) -> Path:
    ts = int(time.time())
    safe = label.replace("/", "_").replace(" ", "-")[:40]
    return _snapshot_base() / f"{safe}_{ts}"


def _list_all() -> list[Path]:
    if not _snapshot_base().exists():
        return []
    return sorted(_snapshot_base().iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)


def create(label: str) -> dict:
    snap_dir = _label_dir(label)
    try:
        _snapshot_base().mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["btrfs", "subvolume", "snapshot", "-r", str(_HOME), str(snap_dir)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip()}

        # Prune oldest unlabeled snapshots past the cap
        all_snaps = _list_all()
        if len(all_snaps) > _MAX_SNAPSHOTS:
            for old in all_snaps[_MAX_SNAPSHOTS:]:
                try:
                    subprocess.run(
                        ["btrfs", "subvolume", "delete", str(old)],
                        capture_output=True, timeout=15,
                    )
                except Exception:
                    pass

        return {"ok": True, "path": str(snap_dir), "label": label}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_snapshots() -> list[dict]:
    snaps: list[dict] = []
    for p in _list_all():
        snaps.append({
            "path": str(p),
            "label": p.name.rsplit("_", 1)[0],
            "ts": p.stat().st_mtime,
        })
    return snaps


def rollback(label_or_path: str) -> dict:
    """Roll back home to a snapshot. Swaps subvolumes — requires nora-snap-runner."""
    snap = _find_snapshot(label_or_path)
    if not snap:
        return {"ok": False, "error": f"No snapshot found for '{label_or_path}'."}
    try:
        # Use the privileged snap_runner for the actual subvolume swap
        from nora.platform.linux import snap_runner
        return snap_runner.rollback(snap["path"])
    except ImportError:
        return {
            "ok": False,
            "error": "snap_runner not available. Run: nora-linux-setup",
        }


def _find_snapshot(label_or_path: str) -> dict | None:
    for snap in list_snapshots():
        if snap["label"] == label_or_path or snap["path"] == label_or_path:
            return snap
    return None

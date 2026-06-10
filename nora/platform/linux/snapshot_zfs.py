"""ZFS snapshot backend for F4 Time-Travel.

Requires ZFS on Linux (zfsutils-linux on Ubuntu, DKMS + reboot on Fedora).
Detection: findmnt -no FSTYPE /home → "zfs"

Snapshots are stored as:  <pool>/home/<user>@nora_<label>_<ts>
"""
from __future__ import annotations

import logging
import subprocess
import time

logger = logging.getLogger("nora.platform.snapshot_zfs")

_MAX_SNAPSHOTS = 100


def is_available() -> bool:
    try:
        result = subprocess.run(
            ["findmnt", "-no", "FSTYPE", "/home"],
            capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip() == "zfs"
    except Exception:
        return False


def _get_dataset() -> str | None:
    """Return the ZFS dataset name for /home."""
    try:
        result = subprocess.run(
            ["zfs", "list", "-Ho", "name", "/home"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _snapshot_name(label: str) -> str:
    ts = int(time.time())
    safe = label.replace("@", "").replace("/", "_").replace(" ", "-")[:40]
    return f"nora_{safe}_{ts}"


def create(label: str) -> dict:
    dataset = _get_dataset()
    if not dataset:
        return {"ok": False, "error": "Could not determine ZFS dataset for /home."}

    snap_tag = _snapshot_name(label)
    full_name = f"{dataset}@{snap_tag}"
    try:
        result = subprocess.run(
            ["zfs", "snapshot", full_name],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip()}

        _prune_old(dataset)
        return {"ok": True, "snapshot": full_name, "label": label}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_snapshots() -> list[dict]:
    dataset = _get_dataset()
    if not dataset:
        return []
    try:
        result = subprocess.run(
            ["zfs", "list", "-Ho", "name,creation", "-t", "snapshot", "-r", dataset],
            capture_output=True, text=True, timeout=10,
        )
        snaps: list[dict] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 1 and "@nora_" in parts[0]:
                name = parts[0]
                label = name.split("@nora_")[1].rsplit("_", 1)[0]
                snaps.append({"snapshot": name, "label": label, "creation": parts[1] if len(parts) > 1 else ""})
        return snaps
    except Exception:
        return []


def _prune_old(dataset: str) -> None:
    snaps = list_snapshots()
    if len(snaps) <= _MAX_SNAPSHOTS:
        return
    for old in snaps[_MAX_SNAPSHOTS:]:
        try:
            subprocess.run(
                ["zfs", "destroy", old["snapshot"]],
                capture_output=True, timeout=15,
            )
        except Exception:
            pass


def rollback(label_or_snapshot: str) -> dict:
    snaps = list_snapshots()
    target = None
    for s in snaps:
        if s["label"] == label_or_snapshot or s["snapshot"] == label_or_snapshot:
            target = s
            break
    if not target:
        return {"ok": False, "error": f"No ZFS snapshot found for '{label_or_snapshot}'."}

    # zfs rollback requires -r to delete newer snapshots
    try:
        result = subprocess.run(
            ["zfs", "rollback", "-r", target["snapshot"]],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip()}
        return {"ok": True, "snapshot": target["snapshot"]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

#!/usr/bin/env python3
"""Encrypted backup of NORA's secrets and state, pushed off this machine.

    python3 deploy/backup_state.py              # snapshot, encrypt, upload
    python3 deploy/backup_state.py --local-only # snapshot and encrypt, no upload

Run daily by the nora-backup.timer user unit. Standard library only, so it runs
on the system python3 and does not depend on the venv being intact — the venv
is exactly the kind of thing a restore would be rebuilding.

What goes in: `.env`, the Google OAuth client and token, every `nora_*` state
file, and the ChromaDB directory. SQLite files are copied with the online
backup API rather than read off disk, because NORA may be writing to them
while this runs and a raw copy of a live database can be torn.

Encryption is gpg symmetric AES-256 with the passphrase in
~/.config/nora-backup/passphrase. That file is the only way to read a backup:
it has to be kept somewhere other than this laptop as well (a password
manager), or a dead disk takes the key down with the data.

Restore:
    gpg -d nora-backup-<stamp>.tar.gz.gpg | tar -xz -C ~/Projects/JARVIS
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASSPHRASE = Path.home() / ".config" / "nora-backup" / "passphrase"
LOCAL_DIR = Path.home() / ".local" / "share" / "nora-backups"
REMOTE = os.environ.get("NORA_BACKUP_REMOTE", "gdrive:NORA-backups")
KEEP_LOCAL = 7
REMOTE_MAX_AGE = "60d"

# Top-level names and globs, relative to the repo root.
_PATTERNS = (
    ".env",
    "client_secret_*.json",
    "google_token.json",
    "nora_*.json",
    "nora_*.jsonl",
    "nora_*.db",
)
_DIRS = ("nora_cognitive_db",)
_SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")


def collect(root: Path = ROOT) -> list[Path]:
    """Every file to back up, relative to *root*, sorted."""
    found: set[Path] = set()
    for pattern in _PATTERNS:
        found.update(p for p in root.glob(pattern) if p.is_file())
    for name in _DIRS:
        d = root / name
        if d.is_dir():
            found.update(p for p in d.rglob("*") if p.is_file())
    return sorted(p.relative_to(root) for p in found)


def _copy_sqlite(src: Path, dst: Path) -> None:
    # Read-only URI so a missing file is an error, not a new empty database.
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def snapshot(root: Path, staging: Path) -> list[Path]:
    """Copy the backup set from *root* into *staging*; return what was copied."""
    files = collect(root)
    for rel in files:
        src, dst = root / rel, staging / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix in _SQLITE_SUFFIXES:
            _copy_sqlite(src, dst)
        else:
            shutil.copy2(src, dst)
    return files


def _encrypt(tarball: Path, out: Path) -> None:
    subprocess.run(
        ["gpg", "--batch", "--yes", "--quiet", "--symmetric",
         "--cipher-algo", "AES256", "--pinentry-mode", "loopback",
         "--passphrase-file", str(PASSPHRASE), "--output", str(out), str(tarball)],
        check=True,
    )


def _prune_local() -> None:
    backups = sorted(LOCAL_DIR.glob("nora-backup-*.tar.gz.gpg"))
    for old in backups[:-KEEP_LOCAL]:
        old.unlink()


def _upload(path: Path) -> None:
    if shutil.which("rclone") is None:
        raise RuntimeError("rclone is not installed")
    subprocess.run(["rclone", "copy", str(path), REMOTE], check=True)
    # Only files rclone itself put there are visible to it (drive.file scope),
    # so this cannot reach anything else in the Drive.
    subprocess.run(
        ["rclone", "delete", "--min-age", REMOTE_MAX_AGE,
         "--include", "nora-backup-*.tar.gz.gpg", REMOTE],
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--local-only", action="store_true",
                        help="encrypt into ~/.local/share/nora-backups but do not upload")
    args = parser.parse_args(argv)

    if not PASSPHRASE.is_file():
        print(f"No passphrase at {PASSPHRASE}. Run deploy/install_home_core.sh first.",
              file=sys.stderr)
        return 2

    LOCAL_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = LOCAL_DIR / f"nora-backup-{stamp}.tar.gz.gpg"

    # The plaintext never leaves a 0700 temp dir, and is gone when this exits.
    with tempfile.TemporaryDirectory(prefix="nora-backup-") as tmp:
        staging = Path(tmp) / "stage"
        staging.mkdir()
        files = snapshot(ROOT, staging)
        tarball = Path(tmp) / "backup.tar.gz"
        with tarfile.open(tarball, "w:gz") as tar:
            for rel in files:
                tar.add(staging / rel, arcname=str(rel))
        _encrypt(tarball, out)
    os.chmod(out, 0o600)
    _prune_local()
    print(f"Encrypted {len(files)} files → {out}")

    if args.local_only:
        return 0
    try:
        _upload(out)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        # The local copy is still there; fail loudly so the timer shows it.
        print(f"Upload to {REMOTE} failed: {exc}", file=sys.stderr)
        return 1
    print(f"Uploaded to {REMOTE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

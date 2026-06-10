"""CRIU session checkpoint/restore — F4 Time-Travel.

Dumps the current process tree to disk, allowing a full session (nvim,
terminals, dev server) to be paused and resumed across reboots.

Safety constraints:
  - Refuses to checkpoint PIDs holding /dev/nvidia* unless
    experimental_gpu_checkpoint: true in config.yaml.
  - Uses pycriu for RPC or falls back to the criu CLI.
  - Session dumps stored at ~/.nora/sessions/<name>/

Requires: sudo apt install criu && pip install pycriu && nora-linux-setup
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger("nora.platform.criu_session")

_SESSION_ROOT = Path.home() / ".nora" / "sessions"


def is_available() -> bool:
    try:
        result = subprocess.run(
            ["criu", "check"], capture_output=True, timeout=5
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _session_dir(name: str) -> Path:
    safe = name.replace("/", "_").replace(" ", "-")[:40]
    return _SESSION_ROOT / safe


def _holds_nvidia(pid: int) -> bool:
    try:
        fds = list(Path(f"/proc/{pid}/fd").iterdir())
        for fd in fds:
            try:
                target = fd.resolve()
                if "nvidia" in str(target).lower():
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _experimental_gpu_checkpoint() -> bool:
    try:
        from nora.config import get_config
        return bool(get_config().get("experimental_gpu_checkpoint", False))
    except Exception:
        return False


def dump(name: str, pids: list[int] | None = None) -> dict:
    """Checkpoint a process tree. If pids is None, dump the current session."""
    sess_dir = _session_dir(name)
    sess_dir.mkdir(parents=True, exist_ok=True)

    if pids is None:
        # Dump the user's login session tree (login shell's PID)
        try:
            pids = [int(subprocess.check_output(
                ["loginctl", "session-status", "--no-ask-password"],
                text=True, timeout=5
            ).split()[0])]
        except Exception:
            return {"ok": False, "error": "Could not determine session PID. Provide pids= explicitly."}

    blocked_pids = [p for p in pids if _holds_nvidia(p) and not _experimental_gpu_checkpoint()]
    if blocked_pids:
        return {
            "ok": False,
            "error": (
                f"PID(s) {blocked_pids} hold /dev/nvidia* handles. "
                "GPU process checkpointing is experimental. Set "
                "experimental_gpu_checkpoint: true in config.yaml to override."
            ),
        }

    results: list[dict] = []
    for pid in pids:
        pid_dir = sess_dir / str(pid)
        pid_dir.mkdir(exist_ok=True)
        try:
            result = subprocess.run(
                ["criu", "dump", "-t", str(pid), "-D", str(pid_dir),
                 "--shell-job", "--leave-stopped"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                results.append({"pid": pid, "ok": False, "error": result.stderr.strip()})
            else:
                results.append({"pid": pid, "ok": True})
        except FileNotFoundError:
            return {"ok": False, "error": "criu not installed. Run: sudo apt install criu"}
        except Exception as e:
            results.append({"pid": pid, "ok": False, "error": str(e)})

    failed = [r for r in results if not r["ok"]]
    if failed:
        return {"ok": False, "error": str(failed), "partial": results}

    # Write metadata
    import json
    (sess_dir / "meta.json").write_text(
        json.dumps({"name": name, "pids": pids}), encoding="utf-8"
    )
    return {"ok": True, "session": name, "path": str(sess_dir), "pids": pids}


def restore(name: str) -> dict:
    """Restore a checkpointed session."""
    sess_dir = _session_dir(name)
    if not sess_dir.exists():
        return {"ok": False, "error": f"No session dump found for '{name}'."}

    import json
    meta_path = sess_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    except Exception:
        meta = {}

    pids = meta.get("pids", [])
    errors: list[str] = []
    for pid in pids:
        pid_dir = sess_dir / str(pid)
        if not pid_dir.exists():
            errors.append(f"No dump dir for PID {pid}")
            continue
        try:
            result = subprocess.run(
                ["criu", "restore", "-D", str(pid_dir), "--shell-job"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                errors.append(f"PID {pid}: {result.stderr.strip()}")
        except FileNotFoundError:
            return {"ok": False, "error": "criu not installed."}
        except Exception as e:
            errors.append(f"PID {pid}: {e}")

    if errors:
        return {"ok": False, "error": "; ".join(errors)}
    return {"ok": True, "session": name, "restored_pids": pids}


def list_sessions() -> list[dict]:
    if not _SESSION_ROOT.exists():
        return []
    import json
    sessions: list[dict] = []
    for p in _SESSION_ROOT.iterdir():
        if p.is_dir():
            meta_path = p / "meta.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text())
                    sessions.append(meta)
                except Exception:
                    sessions.append({"name": p.name, "path": str(p)})
    return sessions

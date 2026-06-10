"""Privileged bpftrace runner — F3 Linux flagship.

Install as:  /usr/local/libexec/nora-bpf-runner
Capabilities: cap_bpf,cap_perfmon,cap_sys_resource+ep  (kernel 5.8+)
Fallback:    setuid root for kernel 5.4–5.7

Security model:
  - Only allowlisted script names are accepted via argv.
  - Script paths are resolved relative to this file's directory/bpf_scripts/.
  - No arbitrary bpftrace code paths from callers.
  - Each run is capped at MAX_DURATION_SEC seconds and MAX_EVENTS lines.

Usage (from nora-linux-setup):
  sudo cp nora/observability/bpf_runner.py /usr/local/libexec/nora-bpf-runner
  sudo setcap cap_bpf,cap_perfmon,cap_sys_resource+ep /usr/local/libexec/nora-bpf-runner

Protocol (stdin/stdout JSON lines):
  Input line:  {"script": "cpu_hotspot", "duration_sec": 5, "args": []}
  Output line: {"ok": true, "events": [...]}  or  {"ok": false, "error": "..."}
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent / "bpf_scripts"

ALLOWLIST = {
    "cpu_hotspot",
    "io_hotspot",
    "net_talkers",
    "dev_opens",
    "child_procs",
    "path_watch",
}

MAX_DURATION_SEC = 30
MAX_EVENTS = 10_000


def _check_lockdown() -> bool:
    """Return True if Secure Boot lockdown blocks kprobes."""
    try:
        content = Path("/sys/kernel/security/lockdown").read_text().strip()
        return "none" not in content.lower()
    except FileNotFoundError:
        return False


def run_script(script_name: str, duration_sec: int, args: list[str]) -> dict:
    if script_name not in ALLOWLIST:
        return {"ok": False, "error": f"Script '{script_name}' not in allowlist."}

    script_path = SCRIPT_DIR / f"{script_name}.bt"
    if not script_path.exists():
        return {"ok": False, "error": f"Script not found: {script_path}"}

    if _check_lockdown():
        return {
            "ok": False,
            "error": "Secure Boot lockdown active — kprobes unavailable. "
                     "Only tracepoints work; check /sys/kernel/security/lockdown.",
        }

    duration_sec = min(max(1, duration_sec), MAX_DURATION_SEC)
    cmd = ["bpftrace", "-f", "json", str(script_path)] + args

    events: list[dict] = []
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        start = time.monotonic()
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= duration_sec or len(events) >= MAX_EVENTS:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if obj.get("type") not in ("attached_probes",):
                    events.append(obj)
            except json.JSONDecodeError:
                events.append({"type": "raw", "data": line})

        stderr = proc.stderr.read()
        if proc.returncode and proc.returncode != -15:  # -15 = SIGTERM (expected)
            return {"ok": False, "error": stderr[:500]}

        return {"ok": True, "events": events}

    except FileNotFoundError:
        return {"ok": False, "error": "bpftrace not installed. Run: sudo apt install bpftrace"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"ok": False, "error": "Invalid JSON"}), flush=True)
            continue

        script = req.get("script", "")
        duration = int(req.get("duration_sec", 5))
        args = [str(a) for a in req.get("args", [])]
        result = run_script(script, duration, args)
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()

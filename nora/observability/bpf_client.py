"""Client that calls the privileged bpf_runner subprocess.

The runner binary (/usr/local/libexec/nora-bpf-runner) has cap_bpf+ep so
the main NORA process stays unprivileged.

If the runner is not installed, calls degrade gracefully: the function
returns {"ok": False, "error": "runner not installed"}.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger("nora.observability.bpf_client")

# Installed location (set by nora-linux-setup)
_RUNNER_INSTALLED = Path("/usr/local/libexec/nora-bpf-runner")
# Development fallback: run as the current user (only works if user has cap_bpf)
_RUNNER_DEV = Path(__file__).resolve().parent / "bpf_runner.py"


def _runner_cmd() -> list[str]:
    if _RUNNER_INSTALLED.exists():
        return [str(_RUNNER_INSTALLED)]
    # Dev fallback — try running with python3 directly (needs cap_bpf on the interpreter)
    if _RUNNER_DEV.exists():
        return ["python3", str(_RUNNER_DEV)]
    return []


def run_script(
    script: str,
    duration_sec: int = 5,
    args: list[str] | None = None,
) -> dict[str, Any]:
    """Run a named bpftrace script and return the result dict.

    Returns {"ok": bool, "events": [...]} or {"ok": False, "error": "..."}.
    """
    cmd = _runner_cmd()
    if not cmd:
        return {
            "ok": False,
            "error": "nora-bpf-runner not installed. Run: nora-linux-setup",
        }

    request = json.dumps({
        "script": script,
        "duration_sec": duration_sec,
        "args": args or [],
    })

    try:
        result = subprocess.run(
            cmd,
            input=request + "\n",
            capture_output=True,
            text=True,
            timeout=duration_sec + 10,
        )
        if result.returncode not in (0, None):
            return {"ok": False, "error": result.stderr[:500]}
        for line in result.stdout.splitlines():
            line = line.strip()
            if line:
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return {"ok": False, "error": "No response from runner."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "bpf_runner timed out."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def summarize_events(events: list[dict]) -> dict[str, Any]:
    """Flatten bpftrace JSON events into a summary dict suitable for LLM narration."""
    summary: dict[str, Any] = {}
    for event in events:
        etype = event.get("type", "")
        data = event.get("data", {})
        if etype == "map":
            for map_name, map_data in data.items():
                if map_name.startswith("@"):
                    summary.setdefault(map_name, {}).update(
                        data.get(map_name, {}) if isinstance(data.get(map_name), dict) else {}
                    )
                    if isinstance(map_data, list):
                        summary[map_name] = map_data
        elif etype == "raw":
            summary.setdefault("raw_lines", []).append(data)
    return summary

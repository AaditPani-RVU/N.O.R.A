"""eBPF Causal Observer ("Why Engine") — F3 Linux flagship.

NORA can now answer "why is my fan loud?", "what's writing to disk?",
"which process touched /dev/video0?" using kernel-grade bpftrace observability.

The anomaly_watchdog upgrades from "CPU > 90%" to "CPU > 90% — and here's
the call stack causing it" via the register_drill_down hook.

Requires bpftrace + nora-linux-setup (one-time privileged install).
Install: sudo apt install bpftrace && nora-linux-setup
"""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

from nora.command_engine import register

logger = logging.getLogger("nora.commands.why_engine")

# Active path watchers: path → (thread, stop_event)
_watchers: dict[str, tuple[threading.Thread, threading.Event]] = {}


def _client():
    from nora.observability import bpf_client
    return bpf_client


def _narrate(script: str, summary: dict[str, Any], duration: int) -> str:
    """Ask Claude Haiku to narrate the eBPF summary in plain English."""
    try:
        import anthropic
        client = anthropic.Anthropic()
        summary_text = str(summary)[:2000]
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    f"A bpftrace script '{script}' ran for {duration}s and produced this data:\n"
                    f"{summary_text}\n\n"
                    "Narrate the top finding in 1-2 plain English sentences suitable for a "
                    "voice assistant to speak aloud. Name the specific process and why it matters. "
                    "Be concise and direct."
                ),
            }],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        logger.debug("Haiku narration failed: %s", e)
        # Fallback: summarize manually
        lines: list[str] = []
        for key, val in summary.items():
            if isinstance(val, (list, dict)):
                lines.append(f"{key}: {str(val)[:200]}")
        return "; ".join(lines[:3]) if lines else "No significant activity detected."


async def _run_and_narrate(script: str, duration_sec: int, args: list[str] | None = None) -> str:
    loop = asyncio.get_event_loop()
    client = _client()

    result = await loop.run_in_executor(
        None, client.run_script, script, duration_sec, args or []
    )

    if not result.get("ok"):
        err = result.get("error", "Unknown error")
        if "not installed" in err:
            return (
                f"eBPF observability is not set up. Run 'nora-linux-setup' once to install "
                f"the privileged bpf runner. ({err})"
            )
        return f"eBPF trace failed: {err}"

    events = result.get("events", [])
    summary = client.summarize_events(events)

    if not summary:
        return f"No significant activity detected in {duration_sec}s trace."

    narration = await loop.run_in_executor(None, _narrate, script, summary, duration_sec)
    return narration


# ── Voice Commands ────────────────────────────────────────────────────────────

@register(
    "why_busy",
    sig="why_busy(duration_sec: int = 5)",
    description="Run a CPU profiling trace and narrate what's consuming CPU (answers 'why is my fan loud?').",
    risk="low",
    category="observe",
)
async def why_busy(duration_sec: int = 5) -> str:
    return await _run_and_narrate("cpu_hotspot", min(duration_sec, 10))


@register(
    "what_writes_disk",
    sig="what_writes_disk(duration_sec: int = 5)",
    description="Trace block I/O and identify what's writing heavily to disk.",
    risk="low",
    category="observe",
)
async def what_writes_disk(duration_sec: int = 5) -> str:
    return await _run_and_narrate("io_hotspot", min(duration_sec, 10))


@register(
    "top_talkers",
    sig="top_talkers(duration_sec: int = 5)",
    description="Trace network activity and name the top processes sending or receiving packets.",
    risk="low",
    category="observe",
)
async def top_talkers(duration_sec: int = 5) -> str:
    return await _run_and_narrate("net_talkers", min(duration_sec, 10))


@register(
    "who_opened",
    sig="who_opened(path: str, duration_sec: int = 10)",
    description="Trace which processes open a specific file or device path (e.g. /dev/video0, /etc/passwd).",
    risk="low",
    category="observe",
)
async def who_opened(path: str, duration_sec: int = 10) -> str:
    return await _run_and_narrate("dev_opens", min(duration_sec, 15), [path])


@register(
    "watch_path",
    sig="watch_path(path: str)",
    description="Start a persistent watcher that speaks an alert each time a file or device is opened.",
    risk="medium",
    requires_confirmation=False,
    category="observe",
)
async def watch_path(path: str) -> str:
    if path in _watchers:
        return f"Already watching '{path}'."

    stop_event = threading.Event()

    def _watcher_thread() -> None:
        import subprocess
        client = _client()
        from nora.platform.linux import dbus_catalog  # noqa: F401

        logger.info("Starting persistent path watch: %s", path)
        cmd = client._runner_cmd()
        if not cmd:
            logger.error("bpf_runner not installed, cannot watch path")
            return

        import json as _json
        import subprocess as _sp

        request = _json.dumps({"script": "path_watch", "duration_sec": 86400, "args": [path]}) + "\n"
        try:
            proc = _sp.Popen(cmd, stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.PIPE, text=True)
            proc.stdin.write(request)
            proc.stdin.flush()

            while not stop_event.is_set():
                line = proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if "OPEN" in line or path in line:
                    logger.info("path_watch event: %s", line)
                    try:
                        from nora import speaker as _spk
                        _spk.speak(f"File access alert: {line}")
                    except Exception:
                        pass
            proc.terminate()
        except Exception as e:
            logger.error("path_watch thread error: %s", e)

    t = threading.Thread(target=_watcher_thread, daemon=True, name=f"nora-watch-{path[:20]}")
    t.start()
    _watchers[path] = (t, stop_event)
    return f"Now watching '{path}' for access events. Say 'unwatch path {path}' to stop."


@register(
    "unwatch_path",
    sig="unwatch_path(path: str)",
    description="Stop a persistent path watcher started by watch_path().",
    risk="low",
    category="observe",
)
async def unwatch_path(path: str) -> str:
    entry = _watchers.pop(path, None)
    if entry is None:
        return f"No active watcher for '{path}'."
    _, stop_event = entry
    stop_event.set()
    return f"Stopped watching '{path}'."


@register(
    "list_watchers",
    sig="list_watchers()",
    description="List all currently active path watchers.",
    risk="low",
    category="observe",
)
async def list_watchers() -> str:
    if not _watchers:
        return "No active path watchers."
    paths = ", ".join(f"'{p}'" for p in _watchers)
    return f"Active watchers ({len(_watchers)}): {paths}."


# ── Anomaly watchdog drill-down hook ─────────────────────────────────────────

def _drill_down_cb(metric: str, value: float) -> None:
    """Called by anomaly_watchdog when a threshold fires. Runs a quick eBPF trace."""
    script_map = {
        "cpu_percent": "cpu_hotspot",
        "disk_write_mbps": "io_hotspot",
        "ram_percent": None,  # no useful bpf trace for RAM pressure
    }
    script = script_map.get(metric)
    if not script:
        return

    async def _trace_and_speak() -> None:
        try:
            narration = await _run_and_narrate(script, 5)
            from nora import speaker as _spk
            _spk.speak(narration)
        except Exception as e:
            logger.debug("drill-down trace failed: %s", e)

    # Fire from the watchdog thread — schedule in the event loop if available
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(_trace_and_speak(), loop)
    except Exception:
        pass


def register_with_watchdog() -> None:
    """Wire the drill-down callback into anomaly_watchdog. Call at startup."""
    try:
        from nora import anomaly_watchdog
        anomaly_watchdog.register_drill_down(_drill_down_cb)
        logger.debug("why_engine drill-down registered with anomaly_watchdog")
    except Exception as e:
        logger.debug("Could not register with anomaly_watchdog: %s", e)

"""End-to-end health check — verifies every pipeline stage can actually run.

Each check answers "would this subsystem work right now?" without doing
heavy work (no model loads, no MCP server spawns): imports, file/binary
presence, API-key presence, and local-server reachability only, so the
whole sweep stays under a second.

Voice surface: health_check (nora/commands/health_commands.py).
"""
from __future__ import annotations

import logging
import shutil
import sys
from typing import Any, Callable

from nora.config import get_config

logger = logging.getLogger("nora.health")


def _check_microphone() -> tuple[bool, str]:
    import sounddevice as sd
    devices = sd.query_devices()
    inputs = [d for d in devices if d.get("max_input_channels", 0) > 0]
    if not inputs:
        return False, "no input device found"
    default = sd.query_devices(kind="input")
    return True, f"input: {default.get('name', 'unknown')}"


def _check_stt() -> tuple[bool, str]:
    import faster_whisper  # noqa: F401
    model = get_config().get("transcriber", {}).get("model_size", "base.en")
    return True, f"faster-whisper ready (model: {model})"


def _check_tts() -> tuple[bool, str]:
    backend = get_config().get("speaker", {}).get("backend", "edge")
    if backend == "kokoro":
        from pathlib import Path
        import kokoro_onnx  # noqa: F401
        cfg = get_config().get("speaker", {}).get("kokoro", {})
        model = Path(cfg.get("model_path", "~/models/kokoro-v1.0.onnx")).expanduser()
        if not model.exists():
            return False, f"kokoro model file missing: {model}"
        return True, "kokoro local backend ready"
    import edge_tts  # noqa: F401
    return True, "edge-tts ready (cloud)"


def _check_llm() -> tuple[bool, str]:
    from nora import intent_parser
    provider = get_config().get("llm", {}).get("provider", "ollama")
    ok = intent_parser.check_ollama_connection()
    if not ok:
        detail = "Ollama server unreachable" if provider == "ollama" else "API key missing"
        return False, f"{provider}: {detail}"
    return True, f"provider: {provider}"


def _check_memory() -> tuple[bool, str]:
    import chromadb  # noqa: F401
    from nora.cognitive_memory import _CHROMA_DIR
    return True, f"chromadb ready ({'existing' if _CHROMA_DIR.exists() else 'fresh'} store)"


def _check_wakeword() -> tuple[bool, str]:
    if not get_config().get("wakeword", {}).get("enabled", False):
        return True, "disabled in config"
    import openwakeword  # noqa: F401
    return True, "openwakeword ready"


def _check_pipewire() -> tuple[bool, str]:
    if not sys.platform.startswith("linux"):
        return True, "not Linux — focus model degrades to available"
    if shutil.which("pw-dump") is None:
        return False, "pw-dump not on PATH (focus/ambient degraded)"
    return True, "pw-dump present"


def _check_atspi() -> tuple[bool, str]:
    if not sys.platform.startswith("linux"):
        return True, "not Linux — screen control unavailable by design"
    import gi  # noqa: F401
    return True, "GObject introspection present"


def _check_dbus() -> tuple[bool, str]:
    if not sys.platform.startswith("linux"):
        return True, "not Linux"
    import dbus_next  # noqa: F401
    return True, "dbus-next present"


def _check_input_injection() -> tuple[bool, str]:
    if not sys.platform.startswith("linux"):
        return True, "not Linux"
    if shutil.which("ydotool") is None:
        return False, "ydotool not on PATH (typing/click injection degraded)"
    return True, "ydotool present"


def _check_mcp() -> tuple[bool, str]:
    servers = get_config().get("mcp_servers", []) or []
    from nora import mcp_bridge
    running = len(mcp_bridge._servers)
    return True, f"{len(servers)} configured, {running} running"


_CHECKS: list[tuple[str, Callable[[], tuple[bool, str]]]] = [
    ("microphone", _check_microphone),
    ("speech-to-text", _check_stt),
    ("text-to-speech", _check_tts),
    ("llm", _check_llm),
    ("memory", _check_memory),
    ("wakeword", _check_wakeword),
    ("pipewire", _check_pipewire),
    ("at-spi", _check_atspi),
    ("d-bus", _check_dbus),
    ("input-injection", _check_input_injection),
    ("mcp", _check_mcp),
]


def run_checks() -> list[dict[str, Any]]:
    """Run every subsystem check; a check that raises counts as degraded."""
    results: list[dict[str, Any]] = []
    for name, fn in _CHECKS:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        results.append({"name": name, "ok": ok, "detail": detail})
    return results


def report() -> str:
    """Speakable one-paragraph health summary."""
    results = run_checks()
    bad = [r for r in results if not r["ok"]]
    if not bad:
        return f"All {len(results)} subsystems healthy."
    degraded = "; ".join(f"{r['name']} ({r['detail']})" for r in bad)
    return (f"{len(results) - len(bad)} of {len(results)} subsystems healthy. "
            f"Degraded: {degraded}.")

"""PipeWire graph manipulation via pw-dump / pw-cli.

Uses subprocess against the stable CLI contract — native Python bindings
are still alpha. All node lookups are by node.name or application.name
(never by numeric ID, which changes on restart).
"""
from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

logger = logging.getLogger("nora.platform.pipewire")


def is_available() -> bool:
    try:
        subprocess.run(["pw-cli", "info", "0"], capture_output=True, timeout=3)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _pw_dump() -> list[dict]:
    try:
        result = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return []
        return json.loads(result.stdout)
    except Exception as e:
        logger.debug("pw-dump failed: %s", e)
        return []


def find_sink_input(app_name: str) -> dict | None:
    """Find a PipeWire sink-input (playing app) by application name."""
    nodes = _pw_dump()
    app_lower = app_name.lower()
    for node in nodes:
        if node.get("type") != "PipeWire:Interface:Node":
            continue
        props = node.get("info", {}).get("props", {})
        name = (
            props.get("node.name", "")
            + " "
            + props.get("application.name", "")
            + " "
            + props.get("media.name", "")
        ).lower()
        if app_lower in name:
            return {"id": node["id"], "name": props.get("application.name", app_name)}
    return None


def find_source(name: str = "default") -> dict | None:
    """Find a PipeWire source (microphone) by name."""
    nodes = _pw_dump()
    for node in nodes:
        if node.get("type") != "PipeWire:Interface:Node":
            continue
        props = node.get("info", {}).get("props", {})
        media_class = props.get("media.class", "")
        if "Source" not in media_class:
            continue
        node_name = props.get("node.name", "").lower()
        if name == "default" or name.lower() in node_name:
            return {"id": node["id"], "name": props.get("node.name", name)}
    return None


def set_volume(node_id: int, volume: float) -> bool:
    """Set the volume of a PipeWire node (0.0 – 1.0)."""
    try:
        result = subprocess.run(
            ["pw-cli", "set-param", str(node_id), "Props",
             f"{{volume: {volume:.3f}}}"],
            capture_output=True, timeout=3,
        )
        return result.returncode == 0
    except Exception as e:
        logger.debug("pw-cli set-volume failed: %s", e)
        return False


def duck_app(app_name: str, volume: float = 0.2) -> bool:
    """Lower volume of an app to duck_level (default 20%)."""
    node = find_sink_input(app_name)
    if not node:
        return False
    return set_volume(node["id"], volume)


def restore_volume(app_name: str) -> bool:
    """Restore full volume for a previously ducked app."""
    node = find_sink_input(app_name)
    if not node:
        return False
    return set_volume(node["id"], 1.0)


def load_rnnoise_filter() -> bool:
    """Load RNNoise denoising filter on the default mic input."""
    try:
        # Try pipewire-filter-chain or pw-loopback with ladspa filter
        result = subprocess.run(
            ["pw-loopback",
             "--capture-props", "node.name=nora_denoised_src media.class=Audio/Source",
             "--playback-props", "node.name=nora_denoised_dst"],
            capture_output=True, timeout=3,
        )
        return result.returncode == 0
    except Exception as e:
        logger.debug("RNNoise load failed: %s", e)
        return False


def create_virtual_sink(name: str) -> dict | None:
    """Create a virtual audio sink for app audio tapping."""
    try:
        result = subprocess.run(
            ["pw-cli", "create-node", "adapter",
             f"{{factory.name=support.null-audio-sink "
             f"node.name={name} "
             f"media.class=Audio/Sink}}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return {"name": name}
    except Exception as e:
        logger.debug("create_virtual_sink failed: %s", e)
    return None

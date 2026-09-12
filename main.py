"""NORA — Never Off, Rarely Asked. Local Voice-Controlled AI Assistant.

Usage:
    python main.py

Prerequisites:
    1. Install dependencies: pip install -r requirements.txt
    2. Start Ollama: ollama serve
    3. Pull a model: ollama pull phi3:mini
    4. Ensure microphone is connected

Controls:
    - Press Ctrl+Shift+Space to speak a command
    - Say "exit" or "quit" to shut down
"""
from __future__ import annotations

import asyncio
import logging
import sys
import webbrowser

from pathlib import Path

from nora.config import get_config, load_config
from nora.logger import setup_logger


def _load_dotenv() -> None:
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    import os
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _pin_audio_node() -> None:
    """Pin every capture this process opens to one PipeWire node, if configured.

    The documented way to choose a microphone is `wpctl set-default`, and on
    this machine it does not stick: the configured default is already the analog
    input, and WirePlumber hands out the ACP digital mic anyway — the one that
    emits a DC rumble with nothing above 4 kHz. `wakeword.input_device` cannot
    rescue it either, because the only PortAudio device with a distinct name is
    the raw ALSA one and it runs at 48 kHz only.

    PIPEWIRE_NODE is the escape hatch: PipeWire reads it when a client connects a
    stream, so setting it before any audio starts routes NORA — wake word,
    push-to-talk, ambient — to the node named, and leaves every other app on the
    system default. Set it here, before the first import that might open a
    stream. `training/wakeword/mic_probe.py --scan` prints the node names.

    A name, not a number. PIPEWIRE_NODE takes either, and the number is a trap:
    node ids are assigned in graph order, so connecting a headset before boot
    renumbers the internal microphones and the id in the config silently comes
    to mean a different device. That has already happened here once — every turn
    returned "No speech in recording" because the pinned id had drifted onto a
    dead input. A node.name is derived from the PCI address and ALSA profile and
    does not move, so a bare number gets a warning rather than quiet obedience.
    """
    import os
    node = get_config().get("audio", {}).get("pipewire_node")
    if node in (None, ""):
        return
    logger = logging.getLogger("nora.startup")
    if str(node).strip().isdigit():
        logger.warning(
            "audio.pipewire_node is the numeric id %s. Node ids are reassigned "
            "on reboot and replug, so this can silently come to mean a "
            "different microphone. Prefer the node name — run "
            "training/wakeword/mic_probe.py --scan to get it.", node,
        )
    # An explicit environment variable is a deliberate override and outranks
    # the config file, the same way it does for everything in .env.
    os.environ.setdefault("PIPEWIRE_NODE", str(node))
    logger.info("Audio pinned to PipeWire node %s", node)


def check_prerequisites() -> bool:
    """Verify that required services and hardware are available."""
    logger = logging.getLogger("nora.startup")
    ok = True

    from nora.intent_parser import check_ollama_connection
    if not check_ollama_connection():
        logger.error("LLM backend not reachable. Check config.yaml â†' llm.provider and API keys.")
        ok = False
    else:
        logger.info("LLM backend: OK")

    # Check microphone
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        input_devices = [d for d in devices if d["max_input_channels"] > 0]
        if not input_devices:
            logger.error("No input audio device found. Connect a microphone.")
            ok = False
        else:
            default = sd.query_devices(kind="input")
            logger.info(f"Microphone: {default['name']}")
    except Exception as e:
        logger.error(f"Audio device check failed: {e}")
        ok = False

    return ok


def main() -> None:
    _load_dotenv()
    # Load configuration
    load_config()

    # Setup logging
    logger = setup_logger()
    logger.info("Starting Nora...")

    # Before anything opens a microphone.
    _pin_audio_node()

    # Check prerequisites
    if not check_prerequisites():
        logger.error("Prerequisites check failed. Fix the issues above and try again.")
        sys.exit(1)

    logger.info("All systems go. Launching pipeline...")

    # Launch NORA blob UI
    from nora import ui_server
    ui_url = ui_server.start()
    # The URL carries NORA_API_TOKEN when one is set, so the browser gets it
    # but the log file does not.
    logger.info(f"NORA UI: {ui_url.split('?')[0]}")
    webbrowser.open(ui_url)

    # Run the main async pipeline
    from nora.pipeline import run
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Nora shut down by user.")
    except Exception as e:
        logger.exception(f"Nora crashed: {e}")
        raise
    finally:
        logger.info("Nora process terminated.")


if __name__ == "__main__":
    main()

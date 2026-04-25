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

from nora.config import load_config
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


def check_prerequisites() -> bool:
    """Verify that required services and hardware are available."""
    logger = logging.getLogger("nora.startup")
    ok = True

    from nora.intent_parser import check_ollama_connection
    if not check_ollama_connection():
        logger.error("LLM backend not reachable. Check config.yaml â†’ llm.provider and API keys.")
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

    # Check prerequisites
    if not check_prerequisites():
        logger.error("Prerequisites check failed. Fix the issues above and try again.")
        sys.exit(1)

    logger.info("All systems go. Launching pipeline...")

    # Launch NORA blob UI
    from nora import ui_server
    ui_url = ui_server.start()
    logger.info(f"NORA UI: {ui_url}")
    webbrowser.open(ui_url)

    # Run the main async pipeline
    from nora.pipeline import run
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Nora shut down by user.")
    finally:
        logger.info("Nora process terminated.")
        sys.exit(0)


if __name__ == "__main__":
    main()

"""NORA vision — camera capture, persistent face memory, presence perception.

Public surface mirrors `nora.ambient`: `start()` / `stop()`, no-ops when
`vision.enabled` is false in config.yaml.

    camera      — single owner of /dev/videoN, reference-counted
    faces       — persistent face memory at ~/.nora/faces.json
    perception  — background loop: who is present, greet on arrival
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nora.vision")


def start() -> None:
    """Start the perception loop if vision is enabled. Fails soft."""
    try:
        from nora.vision import perception
        perception.start()
    except Exception as e:
        logger.debug("Vision unavailable: %s", e)


def stop() -> None:
    """Stop perceiving and release the camera. Safe to call repeatedly."""
    try:
        from nora.vision import camera, perception
        perception.stop()
        camera.stop()
    except Exception as e:
        logger.debug("Vision shutdown skipped: %s", e)

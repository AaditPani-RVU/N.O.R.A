from __future__ import annotations

import os
from pathlib import Path

import yaml

_config: dict | None = None

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: Path | None = None) -> dict:
    global _config
    p = path or CONFIG_PATH
    with open(p, "r", encoding="utf-8") as f:
        _config = yaml.safe_load(f)
    return _config


def get_config() -> dict:
    if _config is None:
        return load_config()
    return _config

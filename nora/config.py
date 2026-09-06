from __future__ import annotations

from pathlib import Path

import yaml

from nora.config_schema import ConfigError, NoraConfig, build_config

_config: dict | None = None
_typed: NoraConfig | None = None

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: Path | None = None) -> dict:
    """Parse config.yaml, validate the sections that are typed, and cache both.

    Validation happens here rather than at each read so a bad config fails at
    startup with every problem named, instead of turning into a surprising
    default somewhere deep in a turn. Only the sections declared in
    `config_schema.SECTIONS` are checked; the rest pass through untouched, which
    is what lets sections get typed one at a time.
    """
    global _config, _typed
    p = path or CONFIG_PATH
    with open(p, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    _typed = build_config(raw)  # raises ConfigError, listing everything wrong
    _config = raw
    return _config


def get_config() -> dict:
    """The raw dict, unchanged. Still how the untyped forty-one sections are read."""
    if _config is None:
        return load_config()
    return _config


def get_typed() -> NoraConfig:
    """The validated view over the typed sections.

    Prefer this for anything it covers: `get_typed().llm.temperature` cannot
    return None for a misspelling the way `get_config()["llm"].get(...)` can.
    """
    if _typed is None:
        load_config()
    assert _typed is not None
    return _typed

"""Persistent user preferences and learned behavior.

Storage: JSON file at project_root/nora_memory.json.
Thread-safe. Tracks:
  - preferred_music  : last explicitly-played track/artist
  - preferred_apps   : frequency count per app name
  - action_frequency : frequency count per registered action
  - workflows        : recent multi-step command sequences
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

logger = logging.getLogger("nora.memory")

_PATH = Path(__file__).resolve().parent.parent / "nora_memory.json"
_lock = threading.RLock()

_default: dict[str, Any] = {
    "preferred_music": {"track": "", "artist": "", "source": ""},
    "preferred_apps": {},
    "action_frequency": {},
    "workflows": [],
}

_state: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    global _state
    if _state is not None:
        return _state
    try:
        if _PATH.exists():
            _state = json.loads(_PATH.read_text(encoding="utf-8"))
        else:
            _state = json.loads(json.dumps(_default))
    except Exception as e:
        logger.warning("Memory load failed (%s); starting fresh.", e)
        _state = json.loads(json.dumps(_default))
    # Ensure all keys exist (forward-compat)
    for k, v in _default.items():
        _state.setdefault(k, v)
    return _state


def _save() -> None:
    if _state is None:
        return
    try:
        _PATH.write_text(json.dumps(_state, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("Memory save failed: %s", e)


def remember_music(track: str, artist: str = "", source: str = "") -> None:
    with _lock:
        s = _load()
        s["preferred_music"] = {"track": track, "artist": artist, "source": source}
        _save()


def get_preferred_music() -> dict[str, str]:
    with _lock:
        return dict(_load()["preferred_music"])


def remember_app(name: str) -> None:
    name = name.lower().strip()
    if not name:
        return
    with _lock:
        s = _load()
        counter = Counter(s["preferred_apps"])
        counter[name] += 1
        s["preferred_apps"] = dict(counter)
        _save()


def top_apps(n: int = 5) -> list[str]:
    with _lock:
        c = Counter(_load()["preferred_apps"])
        return [name for name, _ in c.most_common(n)]


def record_action(action: str) -> None:
    if not action:
        return
    with _lock:
        s = _load()
        counter = Counter(s["action_frequency"])
        counter[action] += 1
        s["action_frequency"] = dict(counter)
        _save()


def record_workflow(text: str, actions: list[str]) -> None:
    if len(actions) < 2:
        return  # only multi-step sequences count as workflows
    with _lock:
        s = _load()
        s["workflows"].insert(0, {
            "text": text,
            "actions": actions,
            "ts": time.time(),
        })
        del s["workflows"][50:]
        _save()


def predict_next_action(current_action: str) -> str | None:
    """Return the most likely next action based on workflow history.

    Uses a simple bigram Markov model over recorded action sequences.
    Only returns a suggestion when observed 3+ times (confidence gate).
    """
    if not current_action:
        return None
    with _lock:
        workflows = _load().get("workflows", [])

    transitions: Counter = Counter()
    for wf in workflows:
        actions = wf.get("actions", [])
        for i, action in enumerate(actions[:-1]):
            if action == current_action:
                transitions[actions[i + 1]] += 1

    if not transitions:
        return None

    best, count = transitions.most_common(1)[0]
    return best if count >= 3 else None


def get_context_summary() -> dict:
    """Return a concise memory snapshot for injection into the LLM system prompt."""
    with _lock:
        s = _load()
    return {
        "preferred_music": dict(s.get("preferred_music", {})),
        "top_apps": [name for name, _ in Counter(s.get("preferred_apps", {})).most_common(3)],
        "top_actions": [name for name, _ in Counter(s.get("action_frequency", {})).most_common(5)],
    }

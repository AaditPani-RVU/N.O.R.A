"""Reversible Actions — Sprint 4 Deliverable #22 (P1).

Every state-changing action records a compensating inverse so NORA can
undo its own work on voice command.

Storage: nora_reversible_log.json  (list, newest first, capped at 500 entries)

Voice commands:
  undo_last_action()          — reverse the most recent reversible action
  undo_actions_since(minutes) — reverse all reversible actions since N minutes ago

Built-in inverse pairs (registered from here; file commands call record_action):
  move_file(src, dst)   → move_file(src=dst, dst=src)
  delete_file(path)     → note only (recycle-bin path expected from caller)
  patch_file(path, ...)  → restore from pre-patch content snapshot
  type_text(text)       → noted only (cannot un-type)
  git_smart_commit()    → git_revert (SHA provided by caller)
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from nora.command_engine import register

logger = logging.getLogger("nora.reversible")

_ROOT = Path(__file__).resolve().parent.parent
_STORE_PATH = _ROOT / "nora_reversible_log.json"
_MAX_ENTRIES = 500
_lock = threading.Lock()

_entries: list[dict[str, Any]] = []
_loaded = False
_pre_action_hooks: list[Callable] = []


def _load() -> None:
    global _entries, _loaded
    if _loaded:
        return
    if _STORE_PATH.exists():
        try:
            raw = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
            _entries = raw if isinstance(raw, list) else []
        except Exception as e:
            logger.warning("reversible load failed: %s", e)
            _entries = []
    _loaded = True


def _save() -> None:
    try:
        _STORE_PATH.write_text(
            json.dumps(_entries, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        logger.warning("reversible save failed: %s", e)


def register_pre_action_hook(cb: Callable) -> None:
    """Register a callback that fires before each record_action() call.

    cb(action: str, params: dict) — fires synchronously; exceptions are swallowed.
    Used by the snapshot module (F4) to take a COW snapshot before any risky action.
    """
    _pre_action_hooks.append(cb)


def record_action(
    action: str,
    params: dict[str, Any],
    inverse_action: str | None,
    inverse_params: dict[str, Any],
    description: str,
    reversible: bool = True,
) -> None:
    """Record a state-changing action and its compensating inverse.

    inverse_action=None marks an action as irreversible (logged but can't be undone).
    """
    for hook in _pre_action_hooks:
        try:
            hook(action, params)
        except Exception as _e:
            logger.debug("pre_action_hook error: %s", _e)

    entry: dict[str, Any] = {
        "ts": time.time(),
        "ts_human": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "params": params,
        "inverse_action": inverse_action,
        "inverse_params": inverse_params,
        "description": description,
        "reversible": reversible,
        "undone": False,
    }
    with _lock:
        _load()
        _entries.insert(0, entry)
        if len(_entries) > _MAX_ENTRIES:
            del _entries[_MAX_ENTRIES:]
        _save()


def get_recent_reversible(minutes: int = 60) -> list[dict[str, Any]]:
    """Return reversible (not-yet-undone) entries from the last N minutes."""
    cutoff = time.time() - (minutes * 60)
    with _lock:
        _load()
        return [
            e for e in _entries
            if e["ts"] >= cutoff and e["reversible"] and not e["undone"] and e["inverse_action"]
        ]


def _execute_inverse(entry: dict[str, Any]) -> str:
    """Run the compensating inverse of an entry. Returns status message."""
    from nora import command_engine
    inv_action = entry["inverse_action"]
    inv_params = entry.get("inverse_params", {})
    handler = command_engine._registry.get(inv_action)
    if handler is None:
        return f"No handler for inverse action '{inv_action}'."
    try:
        result = handler(**inv_params)
        entry["undone"] = True
        _save()
        return result if isinstance(result, str) else f"Undid: {entry['description']}"
    except Exception as e:
        logger.error("Inverse action %s failed: %s", inv_action, e)
        return f"Failed to undo '{entry['description']}': {e}"


# ── Voice Commands ─────────────────────────────────────────────────────────


@register(
    "undo_last_action",
    sig="undo_last_action()",
    description="Reverse the most recent reversible action NORA took",
    category="memory",
    risk="medium",
)
def undo_last_action() -> str:
    """Undo the last reversible action NORA performed."""
    with _lock:
        _load()
        candidates = [e for e in _entries if e["reversible"] and not e["undone"] and e["inverse_action"]]
    if not candidates:
        return "There's nothing reversible to undo."
    entry = candidates[0]
    return _execute_inverse(entry)


@register(
    "undo_actions_since",
    sig="undo_actions_since(minutes: int = 60)",
    description="Reverse all reversible actions NORA took in the last N minutes",
    category="memory",
    risk="high",
    requires_confirmation=True,
)
def undo_actions_since(minutes: int = 60) -> str:
    """Reverse all reversible actions from the last N minutes (newest first)."""
    targets = get_recent_reversible(minutes)
    if not targets:
        return f"No reversible actions in the last {minutes} minutes."
    results: list[str] = []
    for entry in targets:
        msg = _execute_inverse(entry)
        results.append(msg)
    return f"Undid {len(results)} action{'s' if len(results) != 1 else ''}. " + ". ".join(results[:5])


@register(
    "list_reversible_actions",
    sig="list_reversible_actions(minutes: int = 60)",
    description="List recent reversible actions that can be undone",
    category="memory",
)
def list_reversible_actions(minutes: int = 60) -> str:
    """List reversible actions from the last N minutes."""
    entries = get_recent_reversible(minutes)
    if not entries:
        return f"No reversible actions in the last {minutes} minutes."
    lines = [f"{e['ts_human']}: {e['description']}" for e in entries[:8]]
    return f"{len(entries)} reversible action{'s' if len(entries) != 1 else ''}: " + ". ".join(lines)

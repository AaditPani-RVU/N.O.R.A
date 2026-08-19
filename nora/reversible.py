"""Reversible Actions — Sprint 4 Deliverable #22 (P1).

Every state-changing action records a compensating inverse so NORA can
undo its own work on voice command.

Storage: nora_reversible_log.json  (list, newest first, capped at 500 entries)

Voice commands:
  undo_last_action()          — reverse the most recent reversible action
  undo_actions_since(minutes) — reverse all reversible actions since N minutes ago
  undo_bundle(tag)            — reverse every entry sharing a bundle tag
  preview_undo()               — describe what the next undo would do, without doing it

Built-in inverse pairs (registered from here; file commands call record_action):
  move_file(src, dst)   → move_file(src=dst, dst=src)
  delete_file(path)     → note only (recycle-bin path expected from caller)
  patch_file(path, ...)  → restore from pre-patch content snapshot
  type_text(text)       → noted only (cannot un-type)
  git_smart_commit()    → git_revert (SHA provided by caller)

Reversal-tier UX (CODEX_INTEGRATION.md 5.1):
  - bundle_tag on record_action groups related actions (e.g. a multi-file
    rename) so they can be undone as one unit via undo_bundle().
  - format_undo_announcement() gives callers a time-boxed "say undo within
    N seconds" line for the ambient wake path (reversible_actions.announce_window_sec).
  - preview_undo() lets the user see the inverse before committing to it.
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
from nora.config import get_config

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
    bundle_tag: str | None = None,
) -> dict[str, Any]:
    """Record a state-changing action and its compensating inverse.

    inverse_action=None marks an action as irreversible (logged but can't be undone).
    bundle_tag groups related actions (e.g. renaming 12 files in one operation)
    so they can be undone together via undo_bundle(). Returns the stored entry
    so callers can build a time-boxed announcement with format_undo_announcement().
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
        "bundle_tag": bundle_tag,
    }
    with _lock:
        _load()
        _entries.insert(0, entry)
        if len(_entries) > _MAX_ENTRIES:
            del _entries[_MAX_ENTRIES:]
        _save()
    return entry


def get_recent_reversible(minutes: int = 60) -> list[dict[str, Any]]:
    """Return reversible (not-yet-undone) entries from the last N minutes."""
    cutoff = time.time() - (minutes * 60)
    with _lock:
        _load()
        return [
            e for e in _entries
            if e.get("ts", 0) >= cutoff and e.get("reversible") and not e.get("undone") and e.get("inverse_action")
        ]


def format_undo_announcement(entry: dict[str, Any]) -> str:
    """Time-boxed undo prompt for an action just taken (CODEX_INTEGRATION.md 5.1).

    Returns "" for irreversible entries — nothing to announce.
    """
    if not entry.get("inverse_action"):
        return ""
    window = int(get_config().get("reversible_actions", {}).get("announce_window_sec", 30))
    return f"{entry['description']}. Say 'undo' in the next {window} seconds to revert."


def _describe_inverse(entry: dict[str, Any]) -> str:
    """Human-readable preview of what running the inverse would do."""
    inv_action = entry.get("inverse_action")
    if not inv_action:
        return f"'{entry['description']}' is irreversible — nothing to preview."
    inv_params = entry.get("inverse_params", {})
    if inv_params:
        args = ", ".join(f"{k}={v}" for k, v in inv_params.items())
        return f"Undoing '{entry['description']}' would run {inv_action}({args})."
    return f"Undoing '{entry['description']}' would run {inv_action}()."


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
        # Additive observer hook — an undo is the strongest negative signal
        # for tool trust and autonomy demotion (CODEX_INTEGRATION.md 2.4/5.12)
        try:
            from nora import autonomy, consent_memory, tool_trust
            tool_trust.record_reversal(entry.get("action", ""))
            autonomy.note_reversal(entry.get("action", ""))
            if time.time() - entry.get("ts", 0) < 600:
                consent_memory.record_false_yes(entry.get("action", ""))
        except Exception as _e:
            logger.debug("reversal observer hook error: %s", _e)
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
        candidates = [e for e in _entries if e.get("reversible") and not e.get("undone") and e.get("inverse_action")]
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
    "undo_bundle",
    sig='undo_bundle(tag="rename-batch-1")',
    description="Reverse every reversible action recorded under the same bundle tag",
    category="memory",
    risk="high",
    requires_confirmation=True,
)
def undo_bundle(tag: str) -> str:
    """Reverse every entry sharing a bundle tag, newest first."""
    with _lock:
        _load()
        candidates = [
            e for e in _entries
            if e.get("bundle_tag") == tag and e.get("reversible")
            and not e.get("undone") and e.get("inverse_action")
        ]
    if not candidates:
        return f"No reversible actions found under '{tag}'."
    results = [_execute_inverse(entry) for entry in candidates]
    return f"Undid {len(results)} action{'s' if len(results) != 1 else ''} from '{tag}'. " + ". ".join(results[:5])


@register(
    "preview_undo",
    sig="preview_undo()",
    description="Describe what the next undo would revert, without executing it",
    category="memory",
    risk="low",
)
def preview_undo() -> str:
    """Show the inverse of the most recent undoable action without running it."""
    with _lock:
        _load()
        candidates = [e for e in _entries if e.get("reversible") and not e.get("undone") and e.get("inverse_action")]
    if not candidates:
        return "There's nothing reversible to preview."
    return _describe_inverse(candidates[0])


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

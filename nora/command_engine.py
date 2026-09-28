from __future__ import annotations

import asyncio
import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable

from nora.config import get_config
from nora.schemas import IntentResponse, StepResult

logger = logging.getLogger("nora.command_engine")

# Global registry: action_name -> handler function
_registry: dict[str, Callable] = {}
# Metadata registry: action_name -> CommandMeta
_meta: dict[str, "CommandMeta"] = {}


# Keyword names the intent parser reaches for instead of the one in the
# registered signature, mapped to the parameter they mean. Bound only onto a
# parameter the handler actually has and has not already been given, so a
# handler with its own `subject` or `time` keeps it.
#
# This exists because the failure it prevents is total and user-visible: an
# unexpected keyword is a TypeError, the pipeline speaks the exception, and a
# perfectly well-understood request dies as "Failed: add_calendar_event() got
# an unexpected keyword argument 'title'". The parser is not wrong to say
# `title` — every calendar UI calls it that — and it is not wrong to say `when`
# either, because `check_calendar(when=...)` is a real signature sitting next
# to it in the same prompt. Fixing the model's word choice one prompt at a time
# does not converge; accepting the synonym does.
_PARAM_ALIASES: dict[str, str] = {
    # what a thing is called
    "title": "summary", "name": "summary", "event": "summary",
    "event_name": "summary", "label": "summary",
    # when it happens
    "when": "date", "day": "date", "datetime": "date", "date_time": "date",
    "on": "date", "date_str": "date",
    "at": "time", "start": "time", "start_time": "time", "clock": "time",
    # free text
    "message": "text", "content": "text", "body": "text",
    # lookups
    "q": "query", "search": "query", "term": "query", "search_query": "query",
}


def bind_params(handler: Callable, params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Fit `params` to what `handler` actually accepts.

    Returns the keyword arguments to call with, plus the names that could not
    be placed. A handler taking **kwargs is handed everything untouched — it
    has already said it wants whatever arrives.

    Unplaceable keys are dropped rather than raised on, because the handler's
    own defaults are a better answer than a spoken TypeError, and the dropped
    names are logged so a recurring one can earn a place in _PARAM_ALIASES.
    """
    import inspect

    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return dict(params), []

    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return dict(params), []

    accepted = set(sig.parameters)
    bound: dict[str, Any] = {k: v for k, v in params.items() if k in accepted}

    unplaced: list[str] = []
    for key, value in params.items():
        if key in accepted:
            continue
        target = _PARAM_ALIASES.get(key)
        if target in accepted and target not in bound:
            bound[target] = value
        else:
            unplaced.append(key)
    return bound, unplaced


@dataclass
class CommandMeta:
    sig: str = ""                      # full signature for the LLM prompt
    description: str = ""              # inline hint shown after the signature
    risk: str = "low"                  # "low" | "medium" | "high"
    requires_confirmation: bool = False
    category: str = ""                 # groups commands in the generated prompt block


def register(
    action_name: str,
    *,
    sig: str = "",
    description: str = "",
    risk: str = "low",
    requires_confirmation: bool = False,
    category: str = "",
):
    """Decorator to register a command handler with optional manifest metadata."""
    def decorator(func: Callable) -> Callable:
        _registry[action_name] = func
        _meta[action_name] = CommandMeta(
            sig=sig or f"{action_name}()",
            description=description,
            risk=risk,
            requires_confirmation=requires_confirmation,
            category=category,
        )
        logger.debug(f"Registered command: {action_name}")
        return func
    return decorator


def get_available_actions() -> list[str]:
    """Return all registered action names."""
    return sorted(_registry.keys())


def get_action_meta(action_name: str) -> CommandMeta | None:
    return _meta.get(action_name)


# Category ordering for prompt generation
_MAIN_CATEGORIES = ("app", "file", "web", "system", "tts", "ptt", "music", "memory", "tasks", "notification", "workflow", "")
_SCREEN_CATEGORY = "screen"
_OPTIONAL_CATEGORIES = (
    ("dev", "Developer Tools:"),
    ("observe", "System Observability (Linux):"),
    ("time", "Time-Travel & Sessions (Linux):"),
    ("focus", "Focus & Ambient (Linux):"),
    ("vision", "Vision & Camera:"),
    ("mcp", "MCP Tools:"),
)


def _fmt(name: str, meta: CommandMeta) -> str:
    sig = meta.sig or f"{name}()"
    if meta.description:
        return f"- {sig:<52} {meta.description}"
    return f"- {sig}"


def get_action_signatures() -> str:
    """Build the action signatures block for the system prompt from registered metadata."""
    lines: list[str] = []

    for cat in _MAIN_CATEGORIES:
        for name, m in sorted(_meta.items()):
            if m.category == cat:
                lines.append(_fmt(name, m))

    screen = [(n, m) for n, m in sorted(_meta.items()) if m.category == _SCREEN_CATEGORY]
    if screen:
        lines.append("Screen Intelligence:")
        for name, m in screen:
            lines.append(_fmt(name, m))

    for cat, header in _OPTIONAL_CATEGORIES:
        cat_actions = [(n, m) for n, m in sorted(_meta.items()) if m.category == cat]
        if cat_actions:
            lines.append(f"\n{header}")
            for name, m in cat_actions:
                lines.append(_fmt(name, m))

    return "\n".join(lines)


def discover_commands() -> None:
    """Auto-discover built-in command modules and user plugins."""
    import sys
    import nora.commands as commands_pkg

    for _, modname, _ in pkgutil.iter_modules(commands_pkg.__path__):
        full_name = f"nora.commands.{modname}"
        try:
            importlib.import_module(full_name)
            logger.debug(f"Loaded command module: {full_name}")
        except Exception as e:
            logger.error(f"Failed to load command module {full_name}: {e}")

    # User plugins
    cfg = get_config().get("plugins", {})
    if not cfg.get("enabled", True):
        return
    from pathlib import Path
    plugin_dir = Path(cfg.get("dir", "~/.nora/plugins")).expanduser()
    if not plugin_dir.is_dir():
        return
    if str(plugin_dir) not in sys.path:
        sys.path.insert(0, str(plugin_dir))
    for plugin_file in sorted(plugin_dir.glob("*.py")):
        try:
            importlib.import_module(plugin_file.stem)
            logger.info(f"Loaded plugin: {plugin_file.name}")
        except Exception as e:
            logger.error(f"Failed to load plugin {plugin_file.name}: {e}")


async def execute(intent: IntentResponse) -> list[StepResult]:
    """Execute all steps in an IntentResponse. Returns results per step."""
    from nora.security import guest_blocks, guest_decline_message, is_blocked
    from nora import context

    results: list[StepResult] = []
    timeout = float(get_config().get("timeouts", {}).get("command_sec", 15))
    loop = asyncio.get_event_loop()

    for step in intent.steps:
        if context.is_cancelled():
            logger.info("Cancellation signal received — stopping execution.")
            break

        action = step.action
        params = step.parameters

        if is_blocked(action):
            msg = f"Action '{action}' is blocked by security policy."
            logger.warning(msg)
            results.append(StepResult(action=action, success=False, message=msg))
            break

        # Guest mode: a non-owner is in front of the camera, so anything that
        # would read private content into the room declines instead. Restricts
        # only — recognition never grants. See nora/security.py.
        if guest_blocks(action):
            msg = guest_decline_message(action)
            logger.info("Guest mode withheld '%s'", action)
            results.append(
                StepResult(action=action, success=False, message=msg, withheld=True)
            )
            break

        handler = _registry.get(action)
        if handler is None:
            result = StepResult(action=action, success=False, message=f"Unknown action: {action}")
            results.append(result)
            logger.warning(f"Unknown action: {action}")
            break

        params, unplaced = bind_params(handler, params)
        if unplaced:
            logger.warning(
                "Dropped %d parameter(s) %s did not accept: %s",
                len(unplaced), action, ", ".join(sorted(unplaced)),
            )

        try:
            logger.info(f"Executing: {action}({params})")
            if asyncio.iscoroutinefunction(handler):
                coro = handler(**params)
            else:
                coro = loop.run_in_executor(None, lambda h=handler, p=params: h(**p))
            output = await asyncio.wait_for(coro, timeout=timeout)
            if isinstance(output, StepResult):
                result = output
                msg = output.message
            else:
                msg = output if isinstance(output, str) else "Done."
                result = StepResult(action=action, success=True, message=msg)
            results.append(result)
            _log_audit(action, params, msg, True, intent)
        except asyncio.TimeoutError:
            msg = f"Action '{action}' timed out after {timeout:.0f}s."
            logger.error(msg)
            result = StepResult(action=action, success=False, message=msg)
            results.append(result)
            _log_audit(action, params, msg, False, intent)
            break
        except Exception as e:
            logger.error(f"Action {action} failed: {e}")
            msg = str(e)
            result = StepResult(action=action, success=False, message=msg)
            results.append(result)
            _log_audit(action, params, msg, False, intent)
            break

    return results


def _log_audit(
    action: str,
    params: dict,
    result_msg: str,
    success: bool,
    intent: "IntentResponse",
) -> None:
    """Fire-and-forget audit log write; never raises."""
    try:
        from nora import audit_log
        user_text = getattr(intent, "_user_text", "")
        audit_log.record(
            action=action,
            params=params,
            result=result_msg,
            success=success,
            user_text=user_text,
        )
    except Exception:
        pass

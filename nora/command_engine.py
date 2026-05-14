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
_MAIN_CATEGORIES = ("app", "file", "web", "system", "tts", "ptt", "music", "apple_music", "memory", "tasks", "notification", "workflow", "")
_SCREEN_CATEGORY = "screen"
_OPTIONAL_CATEGORIES = (("dev", "Developer Tools:"), ("focus", "Focus & Productivity:"))


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
    from nora.security import is_blocked
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

        handler = _registry.get(action)
        if handler is None:
            result = StepResult(action=action, success=False, message=f"Unknown action: {action}")
            results.append(result)
            logger.warning(f"Unknown action: {action}")
            break

        try:
            logger.info(f"Executing: {action}({params})")
            if asyncio.iscoroutinefunction(handler):
                coro = handler(**params)
            else:
                coro = loop.run_in_executor(None, lambda h=handler, p=params: h(**p))
            output = await asyncio.wait_for(coro, timeout=timeout)
            msg = output if isinstance(output, str) else "Done."
            results.append(StepResult(action=action, success=True, message=msg))
        except asyncio.TimeoutError:
            msg = f"Action '{action}' timed out after {timeout:.0f}s."
            logger.error(msg)
            results.append(StepResult(action=action, success=False, message=msg))
            break
        except Exception as e:
            logger.error(f"Action {action} failed: {e}")
            results.append(StepResult(action=action, success=False, message=str(e)))
            break

    return results

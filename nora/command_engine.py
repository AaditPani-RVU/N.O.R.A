from __future__ import annotations

import asyncio
import importlib
import logging
import pkgutil
from typing import Any, Callable

from nora.config import get_config
from nora.schemas import IntentResponse, StepResult

logger = logging.getLogger("nora.command_engine")

# Global registry: action_name -> handler function
_registry: dict[str, Callable] = {}


def register(action_name: str):
    """Decorator to register a command handler."""
    def decorator(func: Callable) -> Callable:
        _registry[action_name] = func
        logger.debug(f"Registered command: {action_name}")
        return func
    return decorator


def get_available_actions() -> list[str]:
    """Return all registered action names."""
    return sorted(_registry.keys())


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

    results: list[StepResult] = []

    for step in intent.steps:
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
                output = await handler(**params)
            else:
                output = handler(**params)
            msg = output if isinstance(output, str) else "Done."
            results.append(StepResult(action=action, success=True, message=msg))
        except Exception as e:
            logger.error(f"Action {action} failed: {e}")
            results.append(StepResult(action=action, success=False, message=str(e)))
            break

    return results

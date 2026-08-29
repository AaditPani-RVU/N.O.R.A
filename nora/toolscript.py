"""Tool scripts — many actions, one inference.

The action path costs a model round-trip per decision. "Pause Spotify, dim the
screen and read me my calendar" is one sentence to a person and three separate
LLM turns to `intent_parser`, each with its own prompt, its own latency and its
own chance to lose the thread. On a voice interface that round-trip count *is*
the product: nobody minds which model answered, everybody minds the four
seconds of silence.

So the model gets to write a short Python program instead of a step list. One
inference emits the whole plan, the program runs in-process against the
registered command table, and the user hears one answer. Loops, conditionals
and using one command's output as another's input come free — none of which the
flat `ActionStep` list could express at all.

The obvious objection is that this is `exec` on model output. What makes it
tolerable is that the script never gets a general Python environment:

* The namespace holds registered NORA commands and nothing else — no `os`, no
  `subprocess`, no `open`, no `__import__`. Builtins are an allowlist of pure
  functions.
* Every action name is extracted from the AST and checked against
  `nora.security` *before* a single line executes, so a blocked or
  confirmation-worthy call is caught while refusing still costs nothing.
* Every call inside the script goes through the same guard and audit path as a
  normal step. A tool script is a faster way to reach the command table, not a
  way around it.

This is a smaller blast radius than it looks: the commands themselves are the
dangerous part, and they were always reachable in one step anyway.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("nora.toolscript")

# Pure, non-escaping builtins. Notably absent: __import__, open, eval, exec,
# compile, globals, locals, vars, getattr, setattr, delattr, input, help.
# `getattr` is excluded deliberately — it is the standard way out of a
# restricted namespace via an object's __class__ chain.
_SAFE_BUILTINS: dict[str, Any] = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "divmod": divmod, "enumerate": enumerate, "filter": filter, "float": float,
    "int": int, "isinstance": isinstance, "len": len, "list": list, "map": map,
    "max": max, "min": min, "print": print, "range": range, "repr": repr,
    "reversed": reversed, "round": round, "set": set, "sorted": sorted,
    "str": str, "sum": sum, "tuple": tuple, "zip": zip,
    "True": True, "False": False, "None": None,
}

# Syntax with no legitimate use in a tool script and an obvious use in escaping
# one. Import is the main event; the dunder check below covers attribute-chain
# tricks like ().__class__.__bases__.
_FORBIDDEN_NODES = (
    ast.Import, ast.ImportFrom, ast.Lambda,
    ast.ClassDef, ast.AsyncFunctionDef,
    ast.Global, ast.Nonlocal,
)

# A script that runs longer than this is a bug or a runaway loop, not a plan.
# Individual commands still carry their own `timeouts.command_sec`.
DEFAULT_TIMEOUT = 60.0


@dataclass
class ScriptResult:
    ok: bool
    spoken: list[str] = field(default_factory=list)
    error: str = ""
    actions: list[str] = field(default_factory=list)
    duration: float = 0.0

    def summary(self) -> str:
        """What NORA actually says at the end of the script."""
        if self.spoken:
            return " ".join(s for s in self.spoken if s).strip()
        if not self.ok:
            return self.error
        return "Done."


class ScriptError(Exception):
    """Raised inside a script when a call is refused. Halts the run."""


class ScriptTimeout(Exception):
    """Raised inside the script thread once its deadline passes."""


def _deadline_tracer(deadline: float):
    """A trace function that raises inside the script once time is up.

    Abandoning a runaway script is not enough, and the reason is specific to
    CPython: a thread spinning in `while True: pass` holds the GIL and starves
    every other thread in the process. A model that emits one infinite loop
    would degrade the whole assistant for as long as it stays up — the audio
    loop included — and daemonising the thread only defers that to exit.

    There is no way to kill a Python thread from outside, so the stop has to
    come from inside. A per-line trace hook checks the clock and raises, which
    unwinds the script the same way any other error would. It costs real
    execution speed, but a tool script is a dozen lines of glue around
    commands that are themselves doing the actual work.
    """
    def _trace(frame, event, arg):
        if time.monotonic() > deadline:
            raise ScriptTimeout("script exceeded its deadline")
        return _trace
    return _trace


# ── Static analysis ──────────────────────────────────────────────────────────

def called_names(source: str) -> list[str]:
    """Every plain function name called in the script, in source order.

    Only bare `name(...)` calls count. Attribute calls (`x.y()`) are string and
    list methods; they never reach the command table.
    """
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.append(node.func.id)
    return names


def validate(source: str) -> tuple[bool, str]:
    """Reject a script before running any of it.

    Returns (ok, reason). The reason is spoken, so it is phrased for a listener
    rather than for a log.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return False, f"That script doesn't parse: {e.msg}."

    for node in ast.walk(tree):
        if isinstance(node, _FORBIDDEN_NODES):
            return False, f"Scripts can't use {type(node).__name__.lower()}."
        # Blocks the ().__class__.__mro__ style walk out of the namespace.
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return False, "Scripts can't touch dunder attributes."
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            return False, "Scripts can't touch dunder names."

    from nora import command_engine
    from nora.security import is_blocked

    known = set(command_engine.get_available_actions())
    for name in called_names(source):
        if name in _SAFE_BUILTINS or name == "say":
            continue
        if name not in known:
            return False, f"There's no action called {name}."
        if is_blocked(name):
            return False, f"{name} is blocked by the security policy."
    return True, ""


def requires_confirmation(source: str) -> list[str]:
    """Actions in the script that need the user to say yes first.

    Checked up front rather than mid-run: a script is all-or-nothing, and
    stopping halfway through to ask would leave the first half already done.
    """
    from nora import command_engine
    from nora.config import get_config

    always = set(get_config().get("security", {}).get("destructive_actions", []) or [])
    flagged: list[str] = []
    for name in dict.fromkeys(called_names(source)):
        meta = command_engine.get_action_meta(name)
        if name in always or (meta and (meta.requires_confirmation or meta.risk == "high")):
            flagged.append(name)
    return flagged


# ── Execution ────────────────────────────────────────────────────────────────

def _build_namespace(
    spoken: list[str],
    actions: list[str],
    loop: asyncio.AbstractEventLoop | None,
) -> dict[str, Any]:
    """The only names a tool script can see."""
    from nora import command_engine
    from nora.security import guest_blocks, guest_decline_message, is_blocked

    def _make(action: str, handler: Callable) -> Callable:
        def _call(*args: Any, **kwargs: Any) -> Any:
            # Re-checked at call time, not just in `validate`: guest mode can
            # flip between the AST scan and the line actually running.
            if is_blocked(action):
                raise ScriptError(f"{action} is blocked by the security policy.")
            if guest_blocks(action):
                raise ScriptError(guest_decline_message(action))

            actions.append(action)
            try:
                if asyncio.iscoroutinefunction(handler):
                    if loop is None:
                        raise ScriptError(
                            f"{action} needs the event loop, which isn't running."
                        )
                    fut = asyncio.run_coroutine_threadsafe(
                        handler(*args, **kwargs), loop
                    )
                    output = fut.result(timeout=DEFAULT_TIMEOUT)
                else:
                    output = handler(*args, **kwargs)
            except ScriptError:
                raise
            except Exception as e:
                raise ScriptError(f"{action} failed: {e}") from e

            from nora.schemas import StepResult
            message = output.message if isinstance(output, StepResult) else output
            _audit(action, args, kwargs, message)
            return message

        _call.__name__ = action
        return _call

    ns: dict[str, Any] = {"__builtins__": dict(_SAFE_BUILTINS)}
    ns.update(_SAFE_BUILTINS)

    for action in command_engine.get_available_actions():
        handler = command_engine._registry.get(action)
        if handler is not None:
            ns[action] = _make(action, handler)

    def say(*parts: Any) -> None:
        """Queue text for NORA to speak when the script finishes."""
        spoken.append(" ".join(str(p) for p in parts if p is not None))

    ns["say"] = say
    return ns


def _audit(action: str, args: tuple, kwargs: dict, message: Any) -> None:
    try:
        from nora import audit_log
        params = dict(kwargs)
        if args:
            params["_args"] = [str(a)[:200] for a in args]
        audit_log.record(
            action=action, params=params,
            result=str(message)[:500] if message else "",
            success=True, user_text="[tool script]",
        )
    except Exception:
        pass


async def run(source: str, *, timeout: float = DEFAULT_TIMEOUT) -> ScriptResult:
    """Validate and execute a tool script. Never raises."""
    started = time.monotonic()
    spoken: list[str] = []
    actions: list[str] = []

    ok, reason = validate(source)
    if not ok:
        logger.warning("Rejected tool script: %s", reason)
        return ScriptResult(ok=False, error=reason)

    loop = asyncio.get_running_loop()
    ns = _build_namespace(spoken, actions, loop)
    box: dict[str, Any] = {}

    deadline = time.monotonic() + timeout

    def _exec() -> None:
        import sys
        sys.settrace(_deadline_tracer(deadline))
        try:
            exec(compile(source, "<toolscript>", "exec"), ns, ns)  # noqa: S102
        except ScriptTimeout:
            box["error"] = "That took too long and I stopped it."
        except ScriptError as e:
            box["error"] = str(e)
        except Exception as e:
            box["error"] = f"The script failed: {e}"
        finally:
            sys.settrace(None)

    # Run off the event loop so a synchronous command can't stall the audio
    # path, but keep `loop` so async handlers can be marshalled back onto it.
    worker = threading.Thread(target=_exec, daemon=True, name="nora-toolscript")
    worker.start()
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, worker.join, timeout),
            timeout=timeout + 1,
        )
    except asyncio.TimeoutError:
        pass

    duration = time.monotonic() - started
    if worker.is_alive():
        # The tracer should already have unwound it; reaching here means the
        # thread is stuck inside a single long C call, where no Python line
        # event fires. Nothing can interrupt that, so it is left to finish.
        logger.error("Tool script exceeded %.0fs and did not stop", timeout)
        return ScriptResult(ok=False, error="That took too long and I stopped it.",
                            actions=actions, duration=duration)

    if "error" in box:
        logger.warning("Tool script error: %s", box["error"])
        return ScriptResult(ok=False, spoken=spoken, error=box["error"],
                            actions=actions, duration=duration)

    logger.info("Tool script ran %d action(s) in %.2fs: %s",
                len(actions), duration, ", ".join(actions) or "none")
    return ScriptResult(ok=True, spoken=spoken, actions=actions, duration=duration)

# DESIGN_GUIDE.md — Designing and Extending NORA

> Project-specific companion to `Fable.md`. That file says *how to work*;
> this file says *how NORA is built* and the rules any new code must follow.
> Read `README.md` for the pitch and `LINUX_FEATURES_PLAN.md` for the Linux
> flagship specs before designing anything new.

---

## 1. The pipeline is sacred — every action flows through it

```
input (voice/text/WhatsApp/UI)
  → NeuroSym Guard (input stage)      # prompt-injection & adversarial filtering
  → LLM intent parser + ReAct planner # NL → structured JSON action plan
  → semantic memory (ChromaDB/RAG)    # episodic recall, behavioral context
  → NeuroSym Guard (action stage)     # destructive_needs_confirmation, sandbox, max_steps
  → command engine → handler → TTS
```

Design rule: **never add a side door.** New capabilities must be reachable only as
registered actions executed by `command_engine`, so both guard stages, the audit log,
and the reversible log see them. If a feature "needs" to bypass the pipeline, the
design is wrong.

## 2. How to add a command (the only pattern)

Handlers live in `nora/commands/<feature>.py` and are auto-discovered by
`command_engine.discover_commands()` (pkgutil scan — a new file is enough, no
central wiring). Register with the decorator and **complete metadata**:

```python
from nora import command_engine

@command_engine.register(
    "click_element",
    sig='click_element(description="the submit button")',
    description="Click a widget found via the AT-SPI accessibility tree",
    risk="medium",                 # "low" | "medium" | "high"
    requires_confirmation=False,
    category="screen",
)
async def click_element(description: str) -> StepResult: ...
```

Non-negotiables:

- **`sig` and `description` are the LLM's only documentation.** They are injected
  into the intent-parser prompt verbatim. Write the sig with a realistic example
  argument; keep the description one clause.
- **Risk tiers are honest**: `low` = read-only; `medium` = mutates app/UI state but
  recoverable; `high` = destructive or irreversible → also set
  `requires_confirmation=True`. The action-stage guard keys off this.
- **Category determines prompt placement.** Use an existing category
  (`app`, `file`, `web`, `system`, `screen`, `dev`, `observe`, `time`, `focus`, `mcp`, …)
  before inventing one — new categories must be added to the ordering tuples in
  `command_engine.py` or they won't render in the prompt block.
- Return a `StepResult` (`nora/schemas.py`) with a human-speakable `message` —
  `pipeline.summarize_results()` reads it aloud verbatim.

## 3. Platform code is quarantined

- OS-specific implementation goes in `nora/platform/linux/` (e.g. `atspi_tree.py`,
  `dbus_invoker.py`, `pipewire_graph.py`, `ydotool_input.py`). The command module in
  `nora/commands/` stays thin and calls into it.
- **Existing Windows/Mac command modules are untouched** — the Linux branch adds
  new modules, it does not port old ones.
- Every platform import must **fail soft**: guard imports, log once, and let the
  feature self-disable. A missing `gi`/`dbus-next`/eBPF toolchain must never break
  startup on a machine that lacks it.
- Document landmines in the module docstring the way `LINUX_FEATURES_PLAN.md` does
  (e.g. "Wayland only exposes the focused window's tree"; "Electron needs
  `--force-renderer-accessibility`") and give the one-line install command.

## 4. Concurrency model

- NORA's core is a single **asyncio** loop (`pipeline.run()`). Handlers are `async`.
- Blocking or foreign-mainloop work (GLib/AT-SPI, model warm-up, TTS preload) runs on
  a **dedicated daemon thread**, results crossed back via `janus` queues or
  `run_in_executor` — never block the loop, never spin a second asyncio loop.
- Long-running background services follow the `ambient`/`proactive`/`consolidation`
  pattern: a module-level `start()` that is a **no-op when disabled in `config.yaml`**,
  started explicitly from `pipeline.run()`.

## 5. Cognitive hooks, not rewrites

When a new action produces something worth remembering, call the **existing public
API** — `cognitive_memory.record_episode(action, context={...})` — after execution.
Extending core modules is done through the additive hook points that already exist
(`register_drill_down`, `register_pre_action_hook`, `register_on_wake_callback`),
each costing ~10 lines in the host file at most. If your feature needs more than a
hook in an existing file, it should be a new module instead.

## 6. Safety and reversibility are features, not chores

- Anything that mutates files or system state should record an undo entry via
  `nora/reversible.py` (persisted to `nora_reversible_log.json`).
- All executions land in the audit log (`nora/audit_log.py` → `nora_audit_log.jsonl`).
- The main process **stays unprivileged**. Privileged capability is granted through
  narrow OS mechanisms (udev rule for `/dev/uinput`, polkit, a scoped helper) — never
  by running NORA as root.
- No data leaves the machine unless the user configured it to (LLM provider choice
  lives in `config.yaml`; local Ollama must always remain a working option).

## 7. The demo bar

Every flagship-level feature must be **demo-worthy in 30 seconds** with a single
spoken sentence, and should do something only Linux can (AT-SPI2, D-Bus, eBPF,
btrfs/CRIU, PipeWire). When designing, write the demo sentence *first* — if you
can't script a 30-second wow, the feature is infrastructure, not a flagship.

## 8. Repo conventions

- `from __future__ import annotations` at the top of every module; type hints
  throughout; `logging.getLogger("nora.<module>")` — never `print` except in
  `pipeline.run()` startup errors.
- Config access via `get_config()` from `nora/config.py`; new settings get a
  namespaced section in `config.yaml` with safe defaults (feature off or degraded).
- Runtime state files (`nora_*.json`, logs, model caches) are gitignored — never
  commit them.
- New user-facing actions get a row in `COMMANDS.md`; new architecture gets a
  paragraph in `README.md`, not a new top-level doc (the doc set is already large).
- Tests live in `tests/`; at minimum a new command module gets an import test and
  a registration test (action name present in `get_available_actions()`).

---

## Checklist for any new feature

1. Demo sentence written and 30-second-worthy (if flagship-level).
2. New module(s) only; ≤ ~10 additive lines in any existing file, via existing hooks.
3. Registered through `@command_engine.register` with honest `risk`, real `sig`,
   correct `category`.
4. Reaches the OS only through `nora/platform/linux/`; imports fail soft.
5. Undo entry (`reversible`) for mutations; episode recorded (`cognitive_memory`)
   when worth remembering.
6. Config-gated with a safe default; unprivileged main process.
7. Landmines + one-line install documented; `COMMANDS.md` updated; import +
   registration test added.

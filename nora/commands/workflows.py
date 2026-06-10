"""Multi-step workflow macros — one voice command triggers a full dev setup.

Hardcoded workflows call registered command functions directly.
Dynamic workflow recording & replay (Sprint 4 Task 7) adds:
  save_workflow(name)    — snapshot the last N session turns as a named macro
  run_workflow(name)     — replay a saved macro through command_engine
  list_workflows()       — voice summary of all saved macros
  delete_workflow(name)  — remove a saved macro

Storage: nora_workflows.json at project root.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from nora.command_engine import register

_ROOT = Path(__file__).resolve().parent.parent.parent
_WORKFLOWS_PATH = _ROOT / "nora_workflows.json"

logger = logging.getLogger("nora.commands.workflows")


@register("coding_session", sig="coding_session()",
           description="Open VS Code, Chrome, and play focus music", category="workflow")
def coding_session() -> str:
    """Open VS Code, Chrome, and play focus music to kick off a coding session."""
    from nora.commands.app_launcher import open_app
    from nora.commands.music import play_music

    opened: list[str] = []

    try:
        open_app("vscode")
        opened.append("VS Code")
    except Exception as e:
        logger.warning("Could not open VS Code: %s", e)

    try:
        open_app("chrome")
        opened.append("Chrome")
    except Exception as e:
        logger.warning("Could not open Chrome: %s", e)

    try:
        play_music("", "")
        opened.append("music")
    except Exception as e:
        logger.warning("Could not start music: %s", e)

    if opened:
        return f"Coding session started. Opened {', '.join(opened)}. Let's get to work, sir."
    return "Coding session ready."


@register("research_session", sig='research_session(topic: str = "")',
           description="Open Chrome and search a topic", category="workflow")
def research_session(topic: str = "") -> str:
    """Open Chrome and search for a topic to kick off a research session."""
    from nora.commands.app_launcher import open_app
    from nora.commands.web_search import web_search

    open_app("chrome")
    if topic:
        web_search(topic)
        return f"Research session started. Searching for {topic} in Chrome."
    return "Research session started. Chrome is open and ready."


@register("setup_project", sig='setup_project(project_type: str = "python", project_name: str = "new_project")',
           description="Scaffold project dir + venv, open in VS Code. Types: python | ml | cybersecurity",
           category="workflow")
def setup_project(project_type: str = "python", project_name: str = "new_project") -> str:
    """Scaffold a new project with folder structure, venv, and open in VS Code.

    project_type: python | ml | cybersecurity
    project_name: used as directory name (spaces replaced with underscores)
    """
    project_name = project_name.strip().replace(" ", "_").lower()
    base_dir = Path.home() / "Projects"
    project_dir = base_dir / project_name

    if project_dir.exists():
        return f"Project '{project_name}' already exists at {project_dir}."

    try:
        project_dir.mkdir(parents=True, exist_ok=True)

        ptype = project_type.lower()
        if ptype in ("ml", "machine_learning", "ai", "deep_learning"):
            _scaffold_ml(project_dir, project_name)
            label = "Machine learning"
        elif ptype in ("cybersecurity", "security", "ctf", "pentest", "hacking"):
            _scaffold_security(project_dir, project_name)
            label = "Security"
        else:
            _scaffold_python(project_dir, project_name)
            label = "Python"

        subprocess.Popen(["code", str(project_dir)], shell=True)
        return f"{label} project '{project_name}' created at {project_dir} and opened in VS Code."
    except Exception as e:
        logger.error("setup_project failed: %s", e)
        return f"Failed to set up project: {e}"


def _scaffold_python(d: Path, name: str) -> None:
    (d / "src").mkdir()
    (d / "tests").mkdir()
    (d / "src" / "__init__.py").write_text("")
    (d / "tests" / "__init__.py").write_text("")
    (d / "main.py").write_text(
        f'"""Entry point for {name}."""\n\n\ndef main() -> None:\n    pass\n\n\nif __name__ == "__main__":\n    main()\n'
    )
    (d / "requirements.txt").write_text("")
    (d / ".gitignore").write_text("__pycache__/\n*.pyc\n.venv/\n.env\n")
    subprocess.run(["python", "-m", "venv", str(d / ".venv")], shell=True, capture_output=True)


def _scaffold_ml(d: Path, name: str) -> None:
    for folder in ("data/raw", "data/processed", "notebooks", "src", "models", "outputs"):
        (d / folder).mkdir(parents=True)
    (d / "src" / "__init__.py").write_text("")
    (d / "notebooks" / "exploration.ipynb").write_text(
        '{"cells":[],"metadata":{"kernelspec":{"display_name":"Python 3","language":"python","name":"python3"}},"nbformat":4,"nbformat_minor":4}'
    )
    (d / "requirements.txt").write_text("numpy\npandas\nmatplotlib\nscikit-learn\njupyter\ntorch\n")
    (d / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.venv/\ndata/raw/\nmodels/*.pkl\nmodels/*.pt\noutputs/\n"
    )
    subprocess.run(["python", "-m", "venv", str(d / ".venv")], shell=True, capture_output=True)


def _scaffold_security(d: Path, name: str) -> None:
    for folder in ("recon", "exploits", "loot", "notes", "tools", "reports"):
        (d / folder).mkdir(parents=True)
    (d / "notes" / "README.md").write_text(
        f"# {name}\n\n## Scope\n\n## Findings\n\n## Timeline\n"
    )
    (d / "requirements.txt").write_text("requests\nbeautifulsoup4\nscapy\nimpacket\n")
    (d / ".gitignore").write_text("loot/\n*.log\n__pycache__/\n.venv/\ncreds.txt\n")
    subprocess.run(["python", "-m", "venv", str(d / ".venv")], shell=True, capture_output=True)


# ── Dynamic Workflow Recording & Replay (Sprint 4 Task 7) ─────────────────


def _load_workflows() -> dict[str, Any]:
    if _WORKFLOWS_PATH.exists():
        try:
            return json.loads(_WORKFLOWS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_workflows(data: dict[str, Any]) -> None:
    _WORKFLOWS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


@register(
    "save_workflow",
    sig='save_workflow(name: str, steps: int = 5)',
    description="Save the last N session turns as a named replayable workflow macro",
    category="workflow",
)
def save_workflow(name: str, steps: int = 5) -> str:
    """Snapshot the last N session turns and save as a named macro."""
    from nora import context

    turns = context.get_session_turns(steps)
    if not turns:
        return "No recent commands to save as a workflow."

    steps_data = []
    for turn in reversed(turns):
        for action in turn.actions:
            steps_data.append({"action": action, "parameters": {}})

    if not steps_data:
        return "No actionable steps found in recent session to save."

    slug = name.lower().strip().replace(" ", "-")
    workflow: dict[str, Any] = {
        "name": name,
        "slug": slug,
        "steps": steps_data,
        "created": datetime.now().isoformat(),
        "run_count": 0,
    }

    data = _load_workflows()
    data[slug] = workflow
    _save_workflows(data)

    step_labels = " → ".join(s["action"].replace("_", " ") for s in steps_data[:6])
    return (
        f"Workflow '{name}' saved with {len(steps_data)} step{'s' if len(steps_data) != 1 else ''}: "
        f"{step_labels}."
    )


@register(
    "run_workflow",
    sig='run_workflow(name: str)',
    description="Replay a saved workflow macro by name",
    category="workflow",
)
def run_workflow(name: str) -> str:
    """Execute a previously saved workflow macro."""
    import asyncio
    from nora import command_engine
    from nora.schemas import ActionStep, IntentResponse

    slug = name.lower().strip().replace(" ", "-")
    data = _load_workflows()

    if slug not in data:
        # Fuzzy match: look for partial name
        matches = [k for k in data if name.lower() in k or k in name.lower()]
        if not matches:
            available = ", ".join(data.keys()) if data else "none"
            return f"No workflow named '{name}'. Available: {available}"
        slug = matches[0]

    workflow = data[slug]
    steps_data = workflow.get("steps", [])
    if not steps_data:
        return f"Workflow '{name}' has no steps."

    intent = IntentResponse(
        intent=f"run workflow: {workflow['name']}",
        steps=[ActionStep(action=s["action"], parameters=s.get("parameters", {})) for s in steps_data],
    )

    # Execute synchronously (run_workflow itself is called from executor)
    try:
        loop = asyncio.get_event_loop()
        results = loop.run_until_complete(command_engine.execute(intent))
    except RuntimeError:
        # If no running loop, create one
        results = asyncio.run(command_engine.execute(intent))

    ok = sum(1 for r in results if r.success)
    workflow["run_count"] = workflow.get("run_count", 0) + 1
    workflow["last_run"] = datetime.now().isoformat()
    data[slug] = workflow
    _save_workflows(data)

    return (
        f"Workflow '{workflow['name']}' completed: "
        f"{ok}/{len(results)} steps succeeded."
    )


@register(
    "list_workflows",
    sig="list_workflows()",
    description="List all saved workflow macros",
    category="workflow",
)
def list_workflows() -> str:
    """List all saved dynamic workflow macros."""
    data = _load_workflows()
    if not data:
        return "No saved workflows. Say 'save this as a workflow named X' to create one."
    lines = []
    for slug, wf in data.items():
        step_count = len(wf.get("steps", []))
        runs = wf.get("run_count", 0)
        lines.append(f"{wf['name']}: {step_count} steps, run {runs} time{'s' if runs != 1 else ''}")
    return f"{len(data)} saved workflow{'s' if len(data) != 1 else ''}: " + ". ".join(lines)


@register(
    "delete_workflow",
    sig="delete_workflow(name: str)",
    description="Delete a saved workflow macro by name",
    category="workflow",
    risk="medium",
)
def delete_workflow(name: str) -> str:
    """Remove a saved workflow macro."""
    slug = name.lower().strip().replace(" ", "-")
    data = _load_workflows()
    if slug not in data:
        return f"No workflow named '{name}' found."
    display_name = data[slug].get("name", name)
    del data[slug]
    _save_workflows(data)
    return f"Workflow '{display_name}' deleted."

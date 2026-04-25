"""Multi-step workflow macros â€” one voice command triggers a full dev setup.

Workflows call other registered command functions directly (no pipeline round-trip).
Project scaffolding creates directories, boilerplate files, and a venv, then opens
the folder in VS Code.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from nora.command_engine import register

logger = logging.getLogger("nora.commands.workflows")


@register("coding_session")
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


@register("research_session")
def research_session(topic: str = "") -> str:
    """Open Chrome and search for a topic to kick off a research session."""
    from nora.commands.app_launcher import open_app
    from nora.commands.web_search import web_search

    open_app("chrome")
    if topic:
        web_search(topic)
        return f"Research session started. Searching for {topic} in Chrome."
    return "Research session started. Chrome is open and ready."


@register("setup_project")
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

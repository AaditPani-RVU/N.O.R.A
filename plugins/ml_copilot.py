"""NORA plugin: ML Experiment Co-Pilot.

Voice surface for MLflow, TensorBoard, and Weights & Biases.
Scans for active experiment runs locally; reads metrics and narrates trends.

Commands:
  ml_run_status()                  — what's the current training run doing?
  ml_last_metrics()                — read the latest logged metrics aloud
  ml_compare_runs(run_a, run_b)    — compare two named runs
  ml_kill_run(run_name)            — kill a running MLflow / W&B run process
  ml_list_runs(n=5)                — list recent experiment runs
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from nora.command_engine import register

logger = logging.getLogger("plugins.ml_copilot")

_ROOT = Path(__file__).resolve().parent.parent
_MLFLOW_DEFAULT_PORT = 5000
_MLRUNS_DIRS = [_ROOT / "mlruns", Path.home() / "mlruns"]
_TB_DIRS = [_ROOT / "runs", _ROOT / "tensorboard", Path.home() / "runs"]


# ── Backend detection ─────────────────────────────────────────────────────────

def _mlflow_available() -> bool:
    try:
        import mlflow  # noqa: F401
        return True
    except ImportError:
        return False


def _wandb_available() -> bool:
    try:
        import wandb  # noqa: F401
        return True
    except ImportError:
        return False


def _tb_available() -> bool:
    try:
        from tensorboard.backend.event_processing import event_accumulator  # noqa: F401
        return True
    except ImportError:
        return False


def _find_mlruns() -> Path | None:
    for d in _MLRUNS_DIRS:
        if d.exists():
            return d
    return None


def _find_tb_runs() -> Path | None:
    for d in _TB_DIRS:
        if d.exists() and any(d.rglob("events.out.tfevents.*")):
            return d
    return None


# ── MLflow helpers ─────────────────────────────────────────────────────────────

def _mlflow_recent_runs(n: int = 5) -> list[dict[str, Any]]:
    """Return recent MLflow runs from local tracking store."""
    mlruns = _find_mlruns()
    if not mlruns:
        return []
    runs: list[dict[str, Any]] = []
    # Walk experiment dirs → run dirs → meta.yaml
    for exp_dir in sorted(mlruns.iterdir()):
        if not exp_dir.is_dir() or exp_dir.name.startswith("."):
            continue
        for run_dir in sorted(exp_dir.iterdir(), reverse=True):
            if not run_dir.is_dir():
                continue
            meta_file = run_dir / "meta.yaml"
            if not meta_file.exists():
                continue
            try:
                import yaml  # type: ignore
                meta = yaml.safe_load(meta_file.read_text())
            except Exception:
                # Minimal parse without yaml
                text = meta_file.read_text()
                meta = {}
                for line in text.splitlines():
                    if ":" in line:
                        k, _, v = line.partition(":")
                        meta[k.strip()] = v.strip()

            metrics: dict[str, float] = {}
            metrics_dir = run_dir / "metrics"
            if metrics_dir.exists():
                for mf in metrics_dir.iterdir():
                    try:
                        lines = mf.read_text().strip().splitlines()
                        if lines:
                            # format: timestamp value step
                            last = lines[-1].split()
                            metrics[mf.name] = float(last[1]) if len(last) >= 2 else float(last[0])
                    except Exception:
                        pass

            runs.append({
                "run_id": run_dir.name[:8],
                "status": meta.get("status", "UNKNOWN"),
                "name": meta.get("run_name") or meta.get("name") or run_dir.name[:8],
                "start_time": meta.get("start_time", 0),
                "metrics": metrics,
            })
            if len(runs) >= n:
                break
        if len(runs) >= n:
            break
    runs.sort(key=lambda r: r.get("start_time", 0), reverse=True)
    return runs[:n]


# ── TensorBoard helpers ────────────────────────────────────────────────────────

def _tb_latest_scalars(logdir: Path, n: int = 3) -> dict[str, float]:
    """Return the latest scalar values from the most recent TensorBoard event file."""
    try:
        from tensorboard.backend.event_processing.event_accumulator import EventAccumulator
        # Find most recent event file
        event_files = sorted(logdir.rglob("events.out.tfevents.*"))
        if not event_files:
            return {}
        ea = EventAccumulator(str(event_files[-1].parent))
        ea.Reload()
        scalars: dict[str, float] = {}
        for tag in ea.Tags().get("scalars", [])[:n]:
            events = ea.Scalars(tag)
            if events:
                scalars[tag] = events[-1].value
        return scalars
    except Exception:
        return {}


# ── W&B helpers ───────────────────────────────────────────────────────────────

def _wandb_recent_runs(n: int = 3) -> list[dict[str, Any]]:
    try:
        import wandb
        api = wandb.Api()
        runs = api.runs(per_page=n)
        result = []
        for run in list(runs)[:n]:
            result.append({
                "name": run.name,
                "state": run.state,
                "summary": dict(run.summary._json_dict) if run.summary else {},
            })
        return result
    except Exception:
        return []


# ── Voice commands ─────────────────────────────────────────────────────────────

@register("ml_list_runs", sig="ml_list_runs(n: int = 5)",
           description="List recent ML experiment runs (MLflow / W&B)", category="dev")
def ml_list_runs(n: int = 5) -> str:
    backend = "none"
    runs: list[dict] = []

    if _mlflow_available():
        runs = _mlflow_recent_runs(n)
        backend = "MLflow"
    elif _wandb_available():
        runs = _wandb_recent_runs(n)
        backend = "W&B"
    else:
        mlruns = _find_mlruns()
        if mlruns:
            runs = _mlflow_recent_runs(n)
            backend = "MLflow (local)"

    if not runs:
        return "No experiment runs found. Start a training job first."

    lines = []
    for r in runs[:5]:
        status = r.get("status") or r.get("state") or "unknown"
        name = r.get("name", r.get("run_id", "?"))
        metrics = r.get("metrics") or r.get("summary") or {}
        metric_str = ""
        if metrics:
            sample = list(metrics.items())[:2]
            metric_str = ", ".join(f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}"
                                   for k, v in sample)
        lines.append(f"{name} [{status}]{': ' + metric_str if metric_str else ''}")

    return f"{backend} — {len(runs)} run{'s' if len(runs) != 1 else ''}: " + ". ".join(lines) + "."


@register("ml_run_status", sig="ml_run_status()",
           description="Speak the status of the most recent ML training run",
           category="dev")
def ml_run_status() -> str:
    # Try GPU/process status first as a quick check
    try:
        import psutil
        training_procs = []
        for proc in psutil.process_iter(["pid", "name", "cmdline", "cpu_percent", "memory_info"]):
            try:
                cmdline = " ".join(proc.info["cmdline"] or []).lower()
                if any(k in cmdline for k in ("train", "fit", "torch", "tensorflow", "keras")):
                    mem_gb = proc.info["memory_info"].rss / 1e9
                    training_procs.append(
                        f"PID {proc.info['pid']} ({proc.info['name']}, {mem_gb:.1f} GB RAM)"
                    )
            except Exception:
                pass
        if training_procs:
            proc_str = "; ".join(training_procs[:2])
            prefix = f"Active training processes: {proc_str}. "
        else:
            prefix = "No active training processes detected. "
    except ImportError:
        prefix = ""

    # Most recent MLflow run
    runs = _mlflow_recent_runs(1)
    if runs:
        r = runs[0]
        status = r.get("status", "UNKNOWN")
        name = r.get("name", r.get("run_id", "?"))
        metrics = r.get("metrics", {})
        metric_str = ""
        if metrics:
            sample = list(metrics.items())[:3]
            metric_str = " ".join(f"{k}={v:.4f}" for k, v in sample if isinstance(v, float))
        return f"{prefix}Last run: {name} [{status}]. {metric_str}".strip()

    tb_dir = _find_tb_runs()
    if tb_dir:
        scalars = _tb_latest_scalars(tb_dir)
        if scalars:
            metric_str = ", ".join(f"{k}={v:.4f}" for k, v in list(scalars.items())[:3])
            return f"{prefix}TensorBoard latest scalars: {metric_str}."

    return prefix + "No run data found. Is MLflow / TensorBoard tracking active?"


@register("ml_last_metrics", sig="ml_last_metrics(run_name: str = '')",
           description="Read the latest metrics from a training run aloud",
           category="dev")
def ml_last_metrics(run_name: str = "") -> str:
    runs = _mlflow_recent_runs(5)
    if run_name:
        runs = [r for r in runs if run_name.lower() in r.get("name", "").lower()] or runs

    if not runs:
        # Try TensorBoard
        tb_dir = _find_tb_runs()
        if tb_dir:
            scalars = _tb_latest_scalars(tb_dir, n=5)
            if scalars:
                parts = [f"{k}: {v:.6f}" for k, v in scalars.items()]
                return "TensorBoard latest: " + ", ".join(parts) + "."
        return "No metrics found. Check MLflow or TensorBoard is running."

    r = runs[0]
    metrics = r.get("metrics", {})
    if not metrics:
        return f"Run {r.get('name', '?')} has no logged metrics yet."

    parts = []
    for k, v in list(metrics.items())[:6]:
        if isinstance(v, float):
            parts.append(f"{k}: {v:.6f}" if v < 0.01 else f"{k}: {v:.4f}")
        else:
            parts.append(f"{k}: {v}")
    name = r.get("name", r.get("run_id", "?"))
    return f"Run {name}: " + ", ".join(parts) + "."


@register("ml_compare_runs",
          sig="ml_compare_runs(run_a: str, run_b: str)",
          description="Compare metrics of two named MLflow runs",
          category="dev")
def ml_compare_runs(run_a: str, run_b: str) -> str:
    runs = _mlflow_recent_runs(20)
    def _find(name: str) -> dict | None:
        for r in runs:
            if name.lower() in r.get("name", "").lower() or name in r.get("run_id", ""):
                return r
        return None

    a = _find(run_a)
    b = _find(run_b)

    if not a:
        return f"Run '{run_a}' not found."
    if not b:
        return f"Run '{run_b}' not found."

    metrics_a = a.get("metrics", {})
    metrics_b = b.get("metrics", {})
    shared = set(metrics_a) & set(metrics_b)

    if not shared:
        return f"Runs {run_a} and {run_b} have no common metrics to compare."

    parts: list[str] = []
    for key in list(shared)[:4]:
        va = metrics_a[key]
        vb = metrics_b[key]
        if isinstance(va, float) and isinstance(vb, float):
            delta = vb - va
            direction = "up" if delta > 0 else "down"
            parts.append(f"{key}: {va:.4f} vs {vb:.4f} ({direction} {abs(delta):.4f})")
        else:
            parts.append(f"{key}: {va} vs {vb}")

    return f"Comparing {run_a} vs {run_b}: " + "; ".join(parts) + "."


@register("ml_kill_run",
          sig="ml_kill_run(run_name: str = '')",
          description="Kill an active ML training process by name or PID",
          risk="medium", category="dev")
def ml_kill_run(run_name: str = "") -> str:
    try:
        import psutil
        killed = []
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = " ".join(proc.info["cmdline"] or [])
                is_training = any(k in cmdline.lower() for k in ("train", "fit", "torch", "tensorflow"))
                name_match = not run_name or run_name.lower() in cmdline.lower()
                if is_training and name_match:
                    proc.terminate()
                    killed.append(f"PID {proc.info['pid']}")
            except Exception:
                pass
        if killed:
            return f"Terminated training process{'es' if len(killed) > 1 else ''}: {', '.join(killed)}."
        return "No matching training process found."
    except ImportError:
        return "psutil not installed — cannot kill processes by name."

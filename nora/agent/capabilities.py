"""What the travel laptop lets NORA do on it (distributed plan Phase 8e).

Small on purpose: tell the user something, open a link, put text on the
clipboard, and the files in one folder the user chose to share. Nothing
here runs a command NORA names, reads the screen, or reaches outside the
shared folder. Names are `laptop.*`, so they never collide with the phone's.

Each capability is a manifest entry (protocol §5) plus a function that runs
it. Platform specifics (Linux, macOS, Windows) live in the small helpers at
the bottom; a missing tool is a clear failure, not a crash.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from nora.hub import protocol

MAX_READ = 200_000          # bytes of a shared file handed to the core
MAX_WRITE = 200_000
MAX_LIST = 100


class Failed(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


@dataclass
class Capability:
    entry: dict
    run: Callable[[dict], dict]

    @property
    def name(self) -> str:
        return self.entry["name"]


def build(shared_dir: Path) -> list[Capability]:
    shared = shared_dir.expanduser()

    def status(_p: dict) -> dict:
        info = battery()
        words = []
        if info.get("battery_percent") is not None:
            words.append(f"Laptop battery is at {info['battery_percent']}%"
                         + (", charging" if info.get("charging") else ""))
        words.append(f"{platform.system()} {platform.node()}".strip())
        return {**info, "host": platform.node(), "os": platform.system(),
                "message": ". ".join(words) + "."}

    def notify_(p: dict) -> dict:
        notify(p.get("title") or "NORA", p["body"])
        return {"message": "Shown on the laptop."}

    def open_url(p: dict) -> dict:
        url = p["url"].strip()
        if not url.lower().startswith(("https://", "http://")):
            raise Failed(protocol.INVALID_PARAMS, "Only web links open on the laptop.")
        if not webbrowser.open(url, new=2):
            raise Failed(protocol.EXECUTION_FAILED, "The laptop has no browser to open it in.")
        return {"message": "Opened on the laptop."}

    def clip(p: dict) -> dict:
        copy(p["text"])
        return {"message": "Copied to the laptop's clipboard."}

    def listing(p: dict) -> dict:
        root = _root(shared)
        files = []
        for f in sorted(root.rglob("*")):
            if f.is_file() and not _hidden(f, root) and _inside(f, root):
                files.append({"path": str(f.relative_to(root)), "bytes": f.stat().st_size})
                if len(files) >= MAX_LIST:
                    break
        names = ", ".join(x["path"] for x in files[:15]) or "nothing"
        return {"files": files, "message": f"In the shared folder: {names}."}

    def read(p: dict) -> dict:
        f = _resolve(shared, p["path"])
        if not f.is_file():
            raise Failed(protocol.INVALID_PARAMS, f"There's no file {p['path']} in the shared folder.")
        data = f.read_bytes()[:MAX_READ]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise Failed(protocol.INVALID_PARAMS, f"{p['path']} isn't a text file.")
        cut = f.stat().st_size > MAX_READ
        return {"path": p["path"], "text": text, "truncated": cut, "untrusted": True,
                "message": text[:4000] + ("\n[cut off]" if cut or len(text) > 4000 else "")}

    def save(p: dict) -> dict:
        f = _resolve(shared, p["path"])
        if f.exists() and not p.get("overwrite"):
            raise Failed(protocol.INVALID_PARAMS, f"{p['path']} already exists in the shared folder.")
        body = p["text"].encode("utf-8")
        if len(body) > MAX_WRITE:
            raise Failed(protocol.INVALID_PARAMS, "That's too long to save.")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(body)
        return {"path": p["path"], "message": f"Saved {p['path']} in the laptop's shared folder."}

    path_prop = {"type": "string", "minLength": 1, "maxLength": 200}
    return [
        Capability(_entry("laptop.status", 0, "The travel laptop: battery, charging, which machine",
                          {}), status),
        Capability(_entry("laptop.notify", 0, "Show a notification on the travel laptop",
                          {"title": {"type": "string", "maxLength": 80},
                           "body": {"type": "string", "maxLength": 500}}, ["body"]), notify_),
        Capability(_entry("laptop.open_url", 1, "Open a web link in the travel laptop's browser",
                          {"url": {"type": "string", "maxLength": 2000}}, ["url"]), open_url),
        Capability(_entry("laptop.clipboard", 1, "Put text on the travel laptop's clipboard",
                          {"text": {"type": "string", "maxLength": 20000}}, ["text"]), clip),
        Capability(_entry("laptop.shared_files", 0,
                          "List the files in the folder the user shares from the travel laptop", {}), listing),
        Capability(_entry("laptop.read_file", 0,
                          "Read a text file from the travel laptop's shared folder (path relative to it)",
                          {"path": path_prop}, ["path"], untrusted=True), read),
        Capability(_entry("laptop.save_file", 1,
                          "Save text as a file in the travel laptop's shared folder",
                          {"path": path_prop, "text": {"type": "string", "maxLength": MAX_WRITE},
                           "overwrite": {"type": "boolean"}}, ["path", "text"]), save),
    ]


def _entry(name: str, tier: int, description: str, props: dict, required: list[str] | None = None,
           *, untrusted: bool = False) -> dict:
    schema: dict = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    entry = {"name": name, "version": 1, "tier": tier, "description": description,
             "params_schema": schema}
    if untrusted:
        entry["untrusted_output"] = True
    return entry


# ── the shared folder ────────────────────────────────────────────────────────

def _root(shared: Path) -> Path:
    if not shared.is_dir():
        raise Failed(protocol.CAPABILITY_UNAVAILABLE,
                     f"The laptop isn't sharing a folder ({shared} doesn't exist).")
    return shared.resolve()


def _hidden(f: Path, root: Path) -> bool:
    return any(part.startswith(".") for part in f.relative_to(root).parts)


def _inside(f: Path, root: Path) -> bool:
    try:
        return f.resolve().is_relative_to(root)
    except OSError:
        return False


def _resolve(shared: Path, rel: str) -> Path:
    """A path inside the shared folder, or a refusal. Absolute paths, `..`,
    hidden files and symlinks out of the folder are all refused."""
    root = _root(shared)
    rel = rel.strip().replace("\\", "/")
    if not rel or rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        raise Failed(protocol.POLICY_BLOCKED, "Only files inside the shared folder, by their name there.")
    parts = [x for x in rel.split("/") if x not in ("", ".")]
    if any(x == ".." or x.startswith(".") for x in parts):
        raise Failed(protocol.POLICY_BLOCKED, "Only files inside the shared folder, by their name there.")
    f = root.joinpath(*parts)
    if not _inside(f, root):
        raise Failed(protocol.POLICY_BLOCKED, "That points outside the shared folder.")
    return f


# ── the platform ─────────────────────────────────────────────────────────────

def _run(cmd: list[str], *, data: str | None = None) -> bool:
    if shutil.which(cmd[0]) is None:
        return False
    try:
        subprocess.run(cmd, input=data, text=True, capture_output=True, timeout=5, check=True)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def notify(title: str, body: str) -> None:
    if sys.platform.startswith("linux"):
        if _run(["notify-send", "--app-name=NORA", title, body]):
            return
    elif sys.platform == "darwin":
        script = f"display notification {_osa(body)} with title {_osa(title)}"
        if _run(["osascript", "-e", script]):
            return
    elif sys.platform == "win32":
        ps = ("[void][Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms');"
              "$n=New-Object System.Windows.Forms.NotifyIcon;$n.Icon=[System.Drawing.SystemIcons]::Information;"
              "$n.Visible=$true;$n.ShowBalloonTip(8000,$env:NORA_T,$env:NORA_B,'Info');Start-Sleep 9;$n.Dispose()")
        try:
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                             env={**os.environ, "NORA_T": title, "NORA_B": body})
            return
        except OSError:
            pass
    raise Failed(protocol.EXECUTION_FAILED, "The laptop has no way to show notifications.")


def _osa(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def copy(text: str) -> None:
    candidates: list[list[str]]
    if sys.platform == "darwin":
        candidates = [["pbcopy"]]
    elif sys.platform == "win32":
        candidates = [["clip"]]
    else:
        candidates = ([["wl-copy"]] if os.environ.get("WAYLAND_DISPLAY") else []) + \
            [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"], ["wl-copy"]]
    for cmd in candidates:
        if _run(cmd, data=text):
            return
    raise Failed(protocol.EXECUTION_FAILED, "The laptop has no clipboard tool NORA can use.")


def battery() -> dict:
    try:
        import psutil
        b = psutil.sensors_battery()
        if b is not None:
            return {"battery_percent": int(b.percent), "charging": bool(b.power_plugged)}
    except Exception:
        pass
    for supply in sorted(Path("/sys/class/power_supply").glob("BAT*")):
        try:
            level = int((supply / "capacity").read_text().strip())
            state = (supply / "status").read_text().strip()
            return {"battery_percent": level, "charging": state in ("Charging", "Full")}
        except (OSError, ValueError):
            continue
    return {"battery_percent": None, "charging": None}

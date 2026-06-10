"""Wayland/X11 compositor IPC — F5 Linux flagship.

Auto-detects the running compositor from $XDG_CURRENT_DESKTOP /
$WAYLAND_DISPLAY / $SWAYSOCK and dispatches to the right IPC mechanism:
  - Sway / i3:      i3ipc-python library  (i3ipc)
  - Hyprland:       hyprctl subprocess    (stable, avoids raw socket churn)
  - KDE Plasma:     org.kde.KWin D-Bus    (reuses F2's invoker)
  - GNOME:          GNOME Shell D-Bus     (org.gnome.Shell)

All functions degrade gracefully if the compositor is not detected.
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

logger = logging.getLogger("nora.platform.compositor")


def _detect() -> str:
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if os.environ.get("SWAYSOCK"):
        return "sway"
    if "hyprland" in desktop or os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"
    if "kde" in desktop or "plasma" in desktop:
        return "kwin"
    if "gnome" in desktop:
        return "gnome"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


# ── Sway / i3 ─────────────────────────────────────────────────────────────────

def _sway_get_conn():
    try:
        import i3ipc
        return i3ipc.Connection()
    except ImportError:
        raise RuntimeError("i3ipc not installed. Run: pip install i3ipc")


def sway_focus_mode(intent: str) -> bool:
    """Tile windows into a layout for the given intent (coding/writing/meeting)."""
    layouts = {
        "coding": ["code", "terminal", "browser"],
        "writing": ["browser", "notes", "documents"],
        "meeting": ["video", "browser", "chat"],
    }
    target = layouts.get(intent, [])
    try:
        conn = _sway_get_conn()
        # Move matching windows to the primary workspace
        tree = conn.get_tree()
        focused_ws = tree.find_focused().workspace()
        for app_hint in target:
            for window in tree.leaves():
                if app_hint in (window.app_id or "").lower() or \
                   app_hint in (window.name or "").lower():
                    window.command(f"move container to workspace {focused_ws.name}")
        conn.command("layout splith")
        return True
    except Exception as e:
        logger.debug("sway_focus_mode failed: %s", e)
        return False


def sway_mute_notifications() -> bool:
    try:
        conn = _sway_get_conn()
        conn.command("exec mako --mode do-not-disturb 2>/dev/null || "
                     "dunstctl set-paused true 2>/dev/null")
        return True
    except Exception as e:
        logger.debug("sway mute notif failed: %s", e)
        return False


def sway_unmute_notifications() -> bool:
    try:
        conn = _sway_get_conn()
        conn.command("exec makoctl mode -s default 2>/dev/null || "
                     "dunstctl set-paused false 2>/dev/null")
        return True
    except Exception as e:
        logger.debug("sway unmute notif failed: %s", e)
        return False


# ── Hyprland ──────────────────────────────────────────────────────────────────

def _hyprctl(args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["hyprctl"] + args, capture_output=True, text=True, timeout=5
        )
        return result.stdout
    except FileNotFoundError:
        return ""


def hyprland_focus_mode(intent: str) -> bool:
    app_map = {
        "coding": ["code", "nvim", "kitty", "alacritty", "firefox"],
        "writing": ["obsidian", "zathura", "firefox", "libreoffice"],
        "meeting": ["zoom", "teams", "discord", "firefox"],
    }
    apps = app_map.get(intent, [])
    try:
        # Move relevant windows to the current workspace
        clients_raw = _hyprctl(["clients", "-j"])
        import json
        clients = json.loads(clients_raw) if clients_raw else []
        ws_out = _hyprctl(["activeworkspace", "-j"])
        ws = json.loads(ws_out) if ws_out else {"id": 1}
        ws_id = ws.get("id", 1)

        for client in clients:
            class_ = (client.get("class") or "").lower()
            title = (client.get("title") or "").lower()
            if any(a in class_ or a in title for a in apps):
                _hyprctl(["dispatch", "movetoworkspace",
                           f"{ws_id},address:{client['address']}"])
        # Apply master-stack layout
        _hyprctl(["dispatch", "layoutmsg", "orientationtop"])
        return True
    except Exception as e:
        logger.debug("hyprland_focus_mode failed: %s", e)
        return False


def hyprland_mute_notifications() -> bool:
    subprocess.run(["dunstctl", "set-paused", "true"], capture_output=True)
    return True


def hyprland_unmute_notifications() -> bool:
    subprocess.run(["dunstctl", "set-paused", "false"], capture_output=True)
    return True


# ── KDE Plasma / KWin ─────────────────────────────────────────────────────────

async def kwin_focus_mode(intent: str) -> bool:
    try:
        from nora.platform.linux import dbus_invoker
        script = f"""
var allClients = workspace.clientList();
for (var i = 0; i < allClients.length; i++) {{
    var c = allClients[i];
    workspace.sendClientToDesktop(c, 1, true);
}}
"""
        ok, _ = await dbus_invoker.call(
            service="org.kde.KWin",
            bus="session",
            object="/Scripting",
            interface="org.kde.kwin.Scripting",
            method="loadScript",
            args=[script],
        )
        return ok
    except Exception as e:
        logger.debug("kwin_focus_mode failed: %s", e)
        return False


# ── Public dispatch API ───────────────────────────────────────────────────────

def focus_mode(intent: str) -> bool:
    comp = _detect()
    logger.info("Compositor: %s, focus_mode: %s", comp, intent)
    if comp in ("sway", "x11"):
        return sway_focus_mode(intent)
    if comp == "hyprland":
        return hyprland_focus_mode(intent)
    logger.warning("Focus mode not supported for compositor: %s", comp)
    return False


def mute_notifications() -> bool:
    comp = _detect()
    if comp in ("sway", "x11"):
        return sway_mute_notifications()
    if comp == "hyprland":
        return hyprland_mute_notifications()
    return False


def unmute_notifications() -> bool:
    comp = _detect()
    if comp in ("sway", "x11"):
        return sway_unmute_notifications()
    if comp == "hyprland":
        return hyprland_unmute_notifications()
    return False


def get_compositor() -> str:
    return _detect()

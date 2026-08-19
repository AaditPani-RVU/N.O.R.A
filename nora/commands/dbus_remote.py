"""D-Bus Universal Voice Remote — F2 Linux flagship.

Auto-discovers every service on the session and system D-Bus, introspects their
XML, and synthesizes voice commands. Blessed wrappers are curated at startup;
the umbrella dbus_call() handles the long tail; discover_dbus() does semantic
search so the LLM can find anything without seeing the full catalog.

Install: pip install dbus-next rapidfuzz
"""
from __future__ import annotations

import asyncio
import logging

from nora import endpoint_trust
from nora.command_engine import register

logger = logging.getLogger("nora.commands.dbus_remote")


def _catalog():
    from nora.platform.linux import dbus_catalog
    dbus_catalog.load()
    return dbus_catalog


def _invoker():
    from nora.platform.linux import dbus_invoker
    return dbus_invoker


# ── Blessed MPRIS wrappers ────────────────────────────────────────────────────

async def _mpris_call(method: str, args: list | None = None) -> str:
    """Call an MPRIS method on the first active media player found."""
    from nora.platform.linux import dbus_catalog as cat_mod
    cat_mod.load()
    all_entries = cat_mod.get_all()
    players = sorted({
        e["service"] for e in all_entries
        if e["service"].startswith("org.mpris.MediaPlayer2.")
        and e["method"] == method
    })
    if not players:
        return f"No MPRIS media player found for {method}. Is Spotify / VLC / Rhythmbox running?"

    inv = _invoker()
    ok, reply = await inv.call(
        service=players[0],
        bus="session",
        object="/org/mpris/MediaPlayer2",
        interface="org.mpris.MediaPlayer2.Player",
        method=method,
        args=args or [],
    )
    if ok:
        return f"{method} sent to {players[0].split('.')[-1]}."
    return f"MPRIS {method} failed: {reply}"


@register(
    "media_play_pause",
    sig="media_play_pause()",
    description="Toggle play/pause on the active media player (Spotify, VLC, …) via MPRIS.",
    risk="low",
    category="system",
)
async def media_play_pause() -> str:
    return await _mpris_call("PlayPause")


@register(
    "media_next",
    sig="media_next()",
    description="Skip to next track in the active media player via MPRIS.",
    risk="low",
    category="system",
)
async def media_next() -> str:
    return await _mpris_call("Next")


@register(
    "media_previous",
    sig="media_previous()",
    description="Go to previous track in the active media player via MPRIS.",
    risk="low",
    category="system",
)
async def media_previous() -> str:
    return await _mpris_call("Previous")


@register(
    "media_stop",
    sig="media_stop()",
    description="Stop playback in the active media player via MPRIS.",
    risk="low",
    category="system",
)
async def media_stop() -> str:
    return await _mpris_call("Stop")


# ── NetworkManager wrappers ───────────────────────────────────────────────────

@register(
    "wifi_connect",
    sig="wifi_connect(ssid: str)",
    description="Connect to a Wi-Fi network by SSID via NetworkManager.",
    risk="medium",
    requires_confirmation=False,
    category="system",
)
async def wifi_connect(ssid: str) -> str:
    inv = _invoker()
    # Get available connections and find the one matching ssid
    ok, connections = await inv.call(
        service="org.freedesktop.NetworkManager",
        bus="system",
        object="/org/freedesktop/NetworkManager/Settings",
        interface="org.freedesktop.NetworkManager.Settings",
        method="ListConnections",
        args=[],
    )
    if not ok:
        return f"Cannot list connections: {connections}"

    # Activate the matching connection
    conn_paths = connections if isinstance(connections, list) else []
    for conn_path in conn_paths:
        try:
            ok2, settings = await inv.call(
                service="org.freedesktop.NetworkManager",
                bus="system",
                object=conn_path,
                interface="org.freedesktop.NetworkManager.Settings.Connection",
                method="GetSettings",
                args=[],
            )
            if ok2 and isinstance(settings, dict):
                conn_ssid = settings.get("802-11-wireless", {}).get("ssid", b"")
                if isinstance(conn_ssid, bytes):
                    conn_ssid = conn_ssid.decode("utf-8", errors="replace")
                if ssid.lower() in conn_ssid.lower():
                    ok3, _ = await inv.call(
                        service="org.freedesktop.NetworkManager",
                        bus="system",
                        object="/org/freedesktop/NetworkManager",
                        interface="org.freedesktop.NetworkManager",
                        method="ActivateConnection",
                        args=[conn_path, "/", "/"],
                    )
                    if ok3:
                        return f"Connecting to '{ssid}'…"
        except Exception:
            continue

    return f"No saved connection found for '{ssid}'. Add it first via nm-applet or nmcli."


@register(
    "wifi_list",
    sig="wifi_list()",
    description="List known Wi-Fi networks via NetworkManager.",
    risk="low",
    category="system",
)
async def wifi_list() -> str:
    inv = _invoker()
    ok, devices = await inv.call(
        service="org.freedesktop.NetworkManager",
        bus="system",
        object="/org/freedesktop/NetworkManager",
        interface="org.freedesktop.NetworkManager",
        method="GetDevices",
        args=[],
    )
    if not ok:
        return f"Cannot get network devices: {devices}"
    return f"Found {len(devices) if isinstance(devices, list) else '?'} network device(s). Use wifi_connect(ssid) to connect."


# ── BlueZ wrappers ────────────────────────────────────────────────────────────

@register(
    "bluetooth_list_devices",
    sig="bluetooth_list_devices()",
    description="List paired Bluetooth devices via BlueZ.",
    risk="low",
    category="system",
)
async def bluetooth_list_devices() -> str:
    try:
        import subprocess
        result = subprocess.run(
            ["bluetoothctl", "devices"], capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.strip().splitlines()
        if not lines:
            return "No Bluetooth devices found."
        return f"{len(lines)} device(s): " + "; ".join(lines[:8])
    except FileNotFoundError:
        return "bluetoothctl not found. Install bluez."
    except Exception as e:
        return f"Bluetooth list failed: {e}"


@register(
    "bluetooth_connect",
    sig="bluetooth_connect(mac: str)",
    description="Connect to a Bluetooth device by MAC address via BlueZ.",
    risk="medium",
    category="system",
)
async def bluetooth_connect(mac: str) -> str:
    inv = _invoker()
    device_path = f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"
    ok, reply = await inv.call(
        service="org.bluez",
        bus="system",
        object=device_path,
        interface="org.bluez.Device1",
        method="Connect",
        args=[],
    )
    if ok:
        return f"Connecting to Bluetooth device {mac}…"
    return f"Bluetooth connect to {mac} failed: {reply}. Is the device paired?"


@register(
    "bluetooth_pair",
    sig="bluetooth_pair(mac: str)",
    description="Pair with a Bluetooth device by MAC address via BlueZ.",
    risk="medium",
    category="system",
)
async def bluetooth_pair(mac: str) -> str:
    inv = _invoker()
    device_path = f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"
    ok, reply = await inv.call(
        service="org.bluez",
        bus="system",
        object=device_path,
        interface="org.bluez.Device1",
        method="Pair",
        args=[],
    )
    if ok:
        return f"Pairing with {mac} initiated."
    return f"Bluetooth pair with {mac} failed: {reply}"


# ── GNOME / desktop wrappers ──────────────────────────────────────────────────

@register(
    "night_light_toggle",
    sig="night_light_toggle()",
    description="Toggle GNOME night light (blue light filter) on or off.",
    risk="low",
    category="system",
)
async def night_light_toggle() -> str:
    try:
        import subprocess
        result = subprocess.run(
            ["gsettings", "get", "org.gnome.settings-daemon.plugins.color", "night-light-enabled"],
            capture_output=True, text=True, timeout=3
        )
        current = result.stdout.strip()
        new_val = "false" if current == "true" else "true"
        subprocess.run(
            ["gsettings", "set", "org.gnome.settings-daemon.plugins.color",
             "night-light-enabled", new_val],
            timeout=3, check=True
        )
        return f"Night light {'enabled' if new_val == 'true' else 'disabled'}."
    except Exception as e:
        return f"Night light toggle failed: {e}"


@register(
    "send_notification",
    sig="send_notification(title: str, body: str)",
    description="Send a desktop notification via libnotify / D-Bus.",
    risk="low",
    category="system",
)
async def send_notification(title: str, body: str) -> str:
    inv = _invoker()
    ok, reply = await inv.call(
        service="org.freedesktop.Notifications",
        bus="session",
        object="/org/freedesktop/Notifications",
        interface="org.freedesktop.Notifications",
        method="Notify",
        args=["NORA", 0, "", title, body, [], {}, 5000],
    )
    if ok:
        return f"Notification sent: {title}"
    return f"Notification failed: {reply}"


# ── Generic umbrella ──────────────────────────────────────────────────────────

@register(
    "dbus_call",
    sig="dbus_call(service: str, object: str, interface: str, method: str, args: list, bus: str = 'session')",
    description="Call any D-Bus method directly. Use discover_dbus() first to find the right service/interface/method.",
    risk="medium",
    category="system",
)
async def dbus_call(
    service: str,
    object: str,
    interface: str,
    method: str,
    args: list | None = None,
    bus: str = "session",
) -> str:
    inv = _invoker()
    ok, reply = await inv.call(
        service=service,
        bus=bus,
        object=object,
        interface=interface,
        method=method,
        args=args or [],
    )
    endpoint_trust.record(service, ok)  # per-service trust graph (CODEX_INTEGRATION.md 5.7)
    if ok:
        reply_str = str(reply)[:300] if reply else "(no return value)"
        return f"D-Bus call {service}.{method} succeeded: {reply_str}"
    return f"D-Bus call failed: {reply}"


@register(
    "discover_dbus",
    sig="discover_dbus(query: str)",
    description="Search the D-Bus catalog for services/methods matching a natural-language query. Use before dbus_call().",
    risk="low",
    category="system",
)
async def discover_dbus(query: str) -> str:
    cat = _catalog()
    hits = cat.search(query, limit=8)
    if not hits:
        return f"No D-Bus methods found matching '{query}'."
    lines = [
        f"{h['service']} → {h['interface']}.{h['method']}(in={h['in_sig'] or 'none'})"
        for h in hits
    ]
    return f"D-Bus matches for '{query}':\n" + "\n".join(lines)

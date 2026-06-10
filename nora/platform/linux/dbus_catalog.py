"""D-Bus catalog: introspect session + system bus at startup.

Pickled to ~/.nora/dbus_catalog.pkl with 24h TTL. On cache miss or expiry,
re-introspects all bus names. The catalog is intentionally flat so the LLM
never sees the full 10k-method firehose — only blessed wrappers and a
semantic search action are exposed.

Catalog entry schema:
  {"service": str, "bus": "session"|"system", "object": str,
   "interface": str, "method": str, "in_sig": str, "out_sig": str}
"""
from __future__ import annotations

import asyncio
import logging
import os
import pickle
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("nora.platform.dbus_catalog")

_CACHE_PATH = Path.home() / ".nora" / "dbus_catalog.pkl"
_CACHE_TTL = 86400  # 24h

_catalog: list[dict[str, str]] = []
_blessed: list[dict[str, str]] = []  # curated fast-path entries
_loaded = False

# High-value MPRIS / NetworkManager / BlueZ / GNOME services we always include
_PRIORITY_SERVICES = {
    "session": [
        "org.mpris.MediaPlayer2",  # prefix — matched by startswith
        "org.gnome.SettingsDaemon",
        "org.gnome.Shell",
        "org.freedesktop.Notifications",
        "org.kde.StatusNotifierWatcher",
        "org.mozilla.firefox",
        "com.brave.Browser",
        "com.google.Chrome",
    ],
    "system": [
        "org.freedesktop.NetworkManager",
        "org.bluez",
        "org.freedesktop.UPower",
        "org.freedesktop.login1",
        "org.freedesktop.systemd1",
    ],
}

_INTERFACE_BLOCKLIST = {
    "org.freedesktop.DBus.Introspectable",
    "org.freedesktop.DBus.Properties",
    "org.freedesktop.DBus.Peer",
    "org.freedesktop.DBus",
}


async def _introspect_one(
    bus: Any, service: str, object_path: str, bus_label: str
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    try:
        introspection = await bus.introspect(service, object_path)
    except Exception as e:
        logger.debug("Introspect %s %s failed: %s", service, object_path, e)
        return entries

    for iface in introspection.interfaces:
        if iface.name in _INTERFACE_BLOCKLIST:
            continue
        for method in iface.methods:
            in_sig = "".join(a.signature for a in method.in_args)
            out_sig = "".join(a.signature for a in method.out_args)
            entries.append({
                "service": service,
                "bus": bus_label,
                "object": object_path,
                "interface": iface.name,
                "method": method.name,
                "in_sig": in_sig,
                "out_sig": out_sig,
            })
    return entries


async def _build_catalog_async() -> list[dict[str, str]]:
    try:
        from dbus_next.aio import MessageBus
        from dbus_next import BusType
    except ImportError:
        logger.warning("dbus-next not installed. Run: pip install dbus-next")
        return []

    entries: list[dict[str, str]] = []

    async def _scan_bus(bus_type: Any, bus_label: str) -> None:
        try:
            bus = await MessageBus(bus_type=bus_type).connect()
            reply = await bus.call_message(
                bus._make_method_call_message(
                    "org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    "ListNames",
                )
            )
            names: list[str] = reply.body[0] if reply.body else []
        except Exception as e:
            logger.warning("Cannot list %s bus names: %s", bus_label, e)
            return

        priority = _PRIORITY_SERVICES.get(bus_label, [])
        selected: list[str] = []
        for name in names:
            if name.startswith(":"):
                continue
            if any(name.startswith(p) for p in priority):
                selected.insert(0, name)
            elif len(selected) < 40:
                selected.append(name)

        for service in selected[:60]:
            try:
                new = await _introspect_one(bus, service, "/", bus_label)
                if not new:
                    new = await _introspect_one(
                        bus, service, f"/{service.replace('.', '/')}", bus_label
                    )
                entries.extend(new)
            except Exception as e:
                logger.debug("Service %s failed: %s", service, e)

        bus.disconnect()

    await _scan_bus(BusType.SESSION, "session")
    try:
        await _scan_bus(BusType.SYSTEM, "system")
    except Exception:
        pass

    return entries


def _build_catalog() -> list[dict[str, str]]:
    try:
        return asyncio.run(_build_catalog_async())
    except RuntimeError:
        # already inside an event loop
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(_build_catalog_async())
        loop.close()
        return result


def load() -> None:
    """Load catalog from pickle cache or rebuild if stale/missing."""
    global _catalog, _blessed, _loaded
    if _loaded:
        return

    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

    if _CACHE_PATH.exists():
        age = time.time() - _CACHE_PATH.stat().st_mtime
        if age < _CACHE_TTL:
            try:
                with _CACHE_PATH.open("rb") as f:
                    data = pickle.load(f)
                _catalog = data.get("catalog", [])
                _blessed = data.get("blessed", [])
                _loaded = True
                logger.info(
                    "D-Bus catalog loaded from cache (%d entries, %.0fh old)",
                    len(_catalog),
                    age / 3600,
                )
                return
            except Exception as e:
                logger.warning("Catalog cache load failed: %s", e)

    logger.info("Building D-Bus catalog (first run or cache expired)…")
    _catalog = _build_catalog()
    _blessed = _build_blessed(_catalog)
    try:
        with _CACHE_PATH.open("wb") as f:
            pickle.dump({"catalog": _catalog, "blessed": _blessed, "ts": time.time()}, f)
    except Exception as e:
        logger.warning("Catalog cache write failed: %s", e)

    _loaded = True
    logger.info("D-Bus catalog built: %d entries, %d blessed", len(_catalog), len(_blessed))


# ── Blessed wrapper generation ────────────────────────────────────────────────

_VERB_WHITELIST = {
    # MPRIS
    "Play", "Pause", "PlayPause", "Stop", "Next", "Previous", "Seek",
    "OpenUri", "SetPosition",
    # NetworkManager
    "ActivateConnection", "AddAndActivateConnection", "GetDevices",
    "ListConnections",
    # BlueZ
    "StartDiscovery", "StopDiscovery", "Pair", "Connect", "Disconnect",
    # GNOME Shell / settings
    "Toggle", "Enable", "Disable", "Activate", "Notify",
    # Systemd
    "StartUnit", "StopUnit", "RestartUnit", "ListUnits",
    # Generic
    "Get", "Set", "List", "Open", "Close", "Show", "Hide",
}


def _build_blessed(catalog: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple] = set()
    result: list[dict[str, str]] = []
    for e in catalog:
        if e["method"] in _VERB_WHITELIST:
            key = (e["service"], e["interface"], e["method"])
            if key not in seen:
                seen.add(key)
                result.append(e)
    return result[:120]


def search(query: str, limit: int = 10) -> list[dict[str, str]]:
    """Semantic search over the catalog using rapidfuzz."""
    if not _loaded:
        load()
    try:
        from rapidfuzz import fuzz, process as rfprocess
    except ImportError:
        return [e for e in _catalog if query.lower() in str(e).lower()][:limit]

    keys = [
        f"{e['service']} {e['interface']} {e['method']}".lower() for e in _catalog
    ]
    hits = rfprocess.extract(query.lower(), keys, scorer=fuzz.WRatio, limit=limit)
    return [_catalog[h[2]] for h in hits if h[1] > 40]


def get_blessed() -> list[dict[str, str]]:
    if not _loaded:
        load()
    return _blessed


def get_all() -> list[dict[str, str]]:
    if not _loaded:
        load()
    return _catalog


def invalidate() -> None:
    """Force catalog rebuild on next load()."""
    global _loaded
    _loaded = False
    _CACHE_PATH.unlink(missing_ok=True)

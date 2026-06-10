"""D-Bus method invoker with typed-arg coercion.

Wraps dbus-next's async MessageBus. Converts Python primitives (str, int,
float, bool, list, dict) to the correct dbus-next Variant types using the
in_sig from the catalog entry.

Usage:
    result = await call(service="org.mpris.MediaPlayer2.spotify",
                        bus="session",
                        object="/org/mpris/MediaPlayer2",
                        interface="org.mpris.MediaPlayer2.Player",
                        method="PlayPause",
                        args=[])
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("nora.platform.dbus_invoker")

# Reuse one bus connection per bus type per event loop
_session_bus: Any = None
_system_bus: Any = None


async def _get_bus(bus_type: str) -> Any:
    global _session_bus, _system_bus
    try:
        from dbus_next.aio import MessageBus
        from dbus_next import BusType
    except ImportError:
        raise RuntimeError("dbus-next not installed. Run: pip install dbus-next")

    if bus_type == "session":
        if _session_bus is None or not _session_bus.connected:
            _session_bus = await MessageBus(bus_type=BusType.SESSION).connect()
        return _session_bus
    else:
        if _system_bus is None or not _system_bus.connected:
            _system_bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        return _system_bus


def _coerce(value: Any, sig: str) -> Any:
    """Best-effort coercion of a Python value to a dbus-next Variant."""
    try:
        from dbus_next import Variant
        type_map: dict[str, type] = {
            "s": str, "b": bool, "i": int, "u": int, "x": int, "t": int,
            "d": float, "y": int, "n": int, "q": int,
        }
        if sig in type_map:
            return Variant(sig, type_map[sig](value))
        if sig == "v":
            return Variant("s", str(value))
        if sig.startswith("a"):
            if isinstance(value, list):
                return Variant(sig, [_coerce(v, sig[1:]) for v in value])
            return Variant(sig, [])
        return Variant("s", str(value))
    except Exception:
        return value


async def call(
    service: str,
    bus: str,
    object: str,
    interface: str,
    method: str,
    args: list[Any],
    in_sig: str = "",
) -> tuple[bool, Any]:
    """Invoke a D-Bus method. Returns (success, reply_body)."""
    try:
        dbus = await _get_bus(bus)
        proxy = await dbus.get_proxy_object(service, object, await dbus.introspect(service, object))
        iface = proxy.get_interface(interface)
        fn = getattr(iface, f"call_{_to_snake(method)}", None)
        if fn is None:
            # fallback: direct message call
            return await _raw_call(dbus, service, object, interface, method, args, in_sig)
        result = await fn(*args)
        return True, result
    except Exception as e:
        logger.error("D-Bus call %s.%s failed: %s", interface, method, e)
        return False, str(e)


async def _raw_call(
    bus: Any, service: str, object: str, interface: str, method: str,
    args: list[Any], in_sig: str,
) -> tuple[bool, Any]:
    try:
        from dbus_next.message import Message
        from dbus_next import MessageType, MessageFlag
        coerced = [_coerce(a, in_sig[i] if i < len(in_sig) else "s") for i, a in enumerate(args)]
        msg = Message(
            destination=service,
            path=object,
            interface=interface,
            member=method,
            body=coerced,
            signature=in_sig,
        )
        reply = await bus.call(msg)
        if reply.message_type.value == 2:  # ERROR
            return False, str(reply.body)
        return True, reply.body
    except Exception as e:
        return False, str(e)


def _to_snake(name: str) -> str:
    """Convert CamelCase D-Bus method name to snake_case for dbus-next proxy."""
    import re
    s = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s).lower()


def call_sync(
    service: str,
    bus: str,
    object: str,
    interface: str,
    method: str,
    args: list[Any],
    in_sig: str = "",
) -> tuple[bool, Any]:
    """Synchronous wrapper — runs in the calling thread's event loop."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're already inside an asyncio loop — caller must await call() directly
            raise RuntimeError("Use await dbus_invoker.call() inside an async context")
        return loop.run_until_complete(
            call(service, bus, object, interface, method, args, in_sig)
        )
    except RuntimeError as e:
        if "no running event loop" in str(e).lower():
            return asyncio.run(call(service, bus, object, interface, method, args, in_sig))
        raise

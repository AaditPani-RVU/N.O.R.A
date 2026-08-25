"""Voice commands for the orb takeovers — processes and memory.

Same shape as the globe's show_location: push the visual to the dashboard
first so the morph starts immediately, then produce the spoken line. The
picture is the answer; the sentence is the caption.
"""
from __future__ import annotations

import logging

from nora import ui_server, visuals
from nora.command_engine import register

logger = logging.getLogger("nora.commands.visuals")


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


@register(
    "show_processes",
    sig="show_processes()",
    description=(
        'User wants to see what is running on the machine — morphs the orb '
        'into a live process constellation they can tap to inspect or kill. '
        'Use for "show processes", "what\'s running", "what\'s eating my CPU", '
        '"why is my machine slow".'
    ),
    category="system",
)
def show_processes() -> str:
    ui_server.notify_takeover("processes")

    snap = visuals.process_snapshot(limit=6)
    rows = snap.get("procs") or []
    if not rows:
        return "I can't read the process table right now."

    total = snap.get("total", 0)
    load = snap.get("cpu_total", 0.0)
    top = rows[0]

    # Only name a process as the culprit if it is actually doing something;
    # otherwise "firefox is using 0.4%" is a confident non-answer.
    if top.get("cpu", 0) >= 8:
        lead = f"{top['name']} is the busiest at {top['cpu']:.0f} percent"
    else:
        heaviest = max(rows, key=lambda r: r.get("rss", 0))
        gb = heaviest.get("rss", 0) / 1e9
        lead = (f"nothing much is running hot — {heaviest['name']} is the "
                f"heaviest at {gb:.1f} gigabytes")

    return (f"{_plural(total, 'process', 'processes')} running, "
            f"CPU at {load:.0f} percent. Right now {lead}. "
            "They're on screen — tap one to look at it.")


@register(
    "show_memory",
    sig="show_memory(topic: str = '')",
    description=(
        'User wants to see what NORA remembers — morphs the orb into a '
        'constellation of her memory, where each star is one memory placed '
        'by meaning, so related memories sit together. Optional topic '
        'highlights the matching stars. Use for "show your memory", '
        '"what do you remember", "show me what you know about <topic>".'
    ),
    category="memory",
)
def show_memory(topic: str = "") -> str:
    focus = visuals.memory_focus(topic) if topic.strip() else []
    ui_server.notify_takeover("memory", focus=focus)

    graph = visuals.memory_graph()
    if graph.get("error"):
        return "I can't reach my memory store right now."

    counts = graph.get("counts") or {}
    known = counts.get("knowledge", 0)
    lived = counts.get("episodes", 0)

    base = (f"I'm holding {_plural(known, 'fact', 'facts')} and "
            f"{_plural(lived, 'episode', 'episodes')}. "
            "Each star is one memory, placed by meaning — the ones near each "
            "other are about the same thing.")

    if topic.strip():
        if focus:
            return (f"{base} I've lit up the {_plural(len(focus), 'one', 'ones')} "
                    f"closest to {topic}.")
        return f"{base} Nothing of mine matches {topic} closely, though."
    return base


@register(
    "show_desktop",
    sig="show_desktop()",
    description=(
        'User wants to see and control the focused window from the dashboard '
        '— puts every button, tab and field of the active window on screen as '
        'a tappable map, so they can press things by hand (especially from a '
        'phone) instead of naming them. Use for "show my desktop", "remote '
        'control", "let me tap the window", "put the window on screen".'
    ),
    category="screen",
)
def show_desktop() -> str:
    from nora import desktop

    if not desktop.available():
        return "I can't read the accessibility tree on this machine."

    # The constellations push the visual first so the morph starts while the
    # sentence is still being written. This one reads the window first: the
    # walk is fast, and opening a remote onto a window that turned out to be
    # unmappable puts an error panel on screen instead of an answer.
    snap = desktop.window_map()
    if not snap.get("ok"):
        return f"I can't map the window right now — {snap.get('error', 'nothing is focused')}."

    elements = snap.get("elements") or []
    if not elements:
        return f"{snap.get('app') or 'That window'} exposes nothing I can press."

    ui_server.notify_takeover("desktop")

    app = snap.get("app") or snap.get("title") or "the window"
    # Fields are the thing voice handles worst, so they are worth calling out
    # separately rather than folding into one count.
    fields = sum(1 for e in elements if e.get("kind") == "input")
    tail = f", {_plural(fields, 'text field', 'text fields')}" if fields else ""
    return (f"{app} is on screen — {_plural(len(elements), 'control', 'controls')}"
            f"{tail}. Tap one and I'll press it.")


@register(
    "hide_takeover",
    sig="hide_takeover()",
    description=(
        'User wants to dismiss whatever the dashboard is currently showing '
        '— a constellation, or the desktop remote — and go back to the plain '
        'orb. Use for "close that", "hide that", "go back", "dismiss".'
    ),
    category="system",
)
def hide_takeover() -> str:
    ui_server.notify_takeover("off")
    return "Closed."

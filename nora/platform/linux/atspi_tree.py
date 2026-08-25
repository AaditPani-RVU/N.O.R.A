"""AT-SPI2 accessibility tree walker for Linux.

Runs a GLib main loop on a daemon thread. Results are crossed into asyncio
via janus queues so callers stay fully async.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib
from rapidfuzz import fuzz, process

logger = logging.getLogger("nora.platform.atspi")

_glib_loop: GLib.MainLoop | None = None
_glib_thread: threading.Thread | None = None


def _ensure_glib_loop() -> None:
    global _glib_loop, _glib_thread
    if _glib_loop is not None:
        return
    _glib_loop = GLib.MainLoop()
    _glib_thread = threading.Thread(
        target=_glib_loop.run, daemon=True, name="nora-atspi-glib"
    )
    _glib_thread.start()
    logger.debug("GLib main loop started for AT-SPI2")


@dataclass
class Widget:
    name: str
    role: str
    description: str
    accessible: Any  # Atspi.Accessible
    parent_chain: list[str]
    # Screen geometry and interaction state, populated only by
    # collect_interactive(). Every read here is an IPC round trip to the
    # owning application, so the voice path — which fuzzy-matches on text
    # and never needs to know where a thing is — does not pay for them.
    rect: tuple[int, int, int, int] | None = None
    enabled: bool = True
    focused: bool = False
    checked: bool = False
    value: str = ""
    kind: str = ""

    @property
    def search_text(self) -> str:
        parts = self.parent_chain + [self.role, self.name, self.description]
        return " ".join(p for p in parts if p)


# Role name -> coarse bucket. AT-SPI role spellings vary by toolkit and by
# at-spi2-core version: GTK3 says "button" where the older vocabulary said
# "push button", GTK says "page tab" where Qt says "tab". Both spellings map
# to the same bucket rather than one of them silently falling through — that
# is what made list_buttons_in_window() report "No buttons found" on a
# desktop full of buttons.
_KIND = {
    "button": "button", "push button": "button",
    "toggle button": "toggle", "check box": "toggle", "radio button": "toggle",
    "link": "link",
    "menu item": "menu", "check menu item": "menu", "radio menu item": "menu",
    "menu": "menu",
    "tab": "tab", "page tab": "tab",
    "list item": "item", "tree item": "item", "table cell": "item",
    "combo box": "combo",
    "slider": "slider", "spin button": "slider",
    "text": "input", "entry": "input", "password text": "input",
    "document text": "input",
}

# Roles worth offering as a target. Derived from _KIND so the voice path and
# the map cannot drift apart on what counts as interactive. Hoisted to module
# scope: this was a set literal rebuilt at every node of every walk.
ACTIONABLE = set(_KIND)


def kind_of(role: str) -> str:
    """The coarse bucket for an AT-SPI *role*, or "" if it is not interactive."""
    return _KIND.get(role, "")


def _collect_widgets(acc: Any, depth: int = 0, parent_chain: list[str] | None = None) -> list[Widget]:
    if parent_chain is None:
        parent_chain = []
    widgets: list[Widget] = []
    if acc is None or depth > 15:
        return widgets
    try:
        name = acc.get_name() or ""
        role = acc.get_role_name() or ""
        desc = acc.get_description() or ""
        chain = parent_chain + ([name] if name else [])

        if role in ACTIONABLE or name:
            widgets.append(Widget(name=name, role=role, description=desc,
                                  accessible=acc, parent_chain=parent_chain))

        for i in range(acc.get_child_count()):
            child = acc.get_child_at_index(i)
            widgets.extend(_collect_widgets(child, depth + 1, chain))
    except Exception:
        pass
    return widgets


# Window-manager helpers that mirror real windows for decoration purposes.
# They carry the same titles as the applications they frame and expose no
# usable tree or geometry, so picking one means mapping an empty window that
# looks like the right one.
_PROXY_APPS = {"mutter-x11-frames", "gnome-shell"}


def _x11_active_pid() -> int:
    """PID of the X11 `_NET_ACTIVE_WINDOW`, or 0.

    AT-SPI's own ACTIVE state is the right signal and the one to try first,
    but plenty of toolkits never set it — on GNOME/X11 no application frame
    reports ACTIVE at all, which left every caller of this module looking at
    an empty window list. The window manager always knows, so ask it and
    match back by process.
    """
    import re
    import subprocess

    try:
        root = subprocess.run(["xprop", "-root", "_NET_ACTIVE_WINDOW"],
                              capture_output=True, timeout=2, text=True)
        m = re.search(r"(0x[0-9a-fA-F]+)", root.stdout or "")
        if not m:
            return 0
        prop = subprocess.run(["xprop", "-id", m.group(1), "_NET_WM_PID"],
                              capture_output=True, timeout=2, text=True)
        m2 = re.search(r"=\s*(\d+)", prop.stdout or "")
        return int(m2.group(1)) if m2 else 0
    except Exception:
        return 0


def _windows_of(app: Any) -> list[Any]:
    """Showing top-level windows of *app*."""
    out = []
    try:
        for j in range(app.get_child_count()):
            win = app.get_child_at_index(j)
            if win is not None and is_showing(win):
                out.append(win)
    except Exception:
        pass
    return out


def focused_window() -> tuple[str, Any] | None:
    """The active window and its application name, or None if nothing is active."""
    _ensure_glib_loop()
    try:
        desktop = Atspi.get_desktop(0)
        apps = []
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            if app is None:
                continue
            name = app.get_name() or ""
            if name in _PROXY_APPS:
                continue
            apps.append((name, app))
    except Exception as e:
        logger.error("AT-SPI walk failed: %s", e)
        return None

    # 1. The toolkit says so.
    for name, app in apps:
        try:
            for win in _windows_of(app):
                if win.get_state_set().contains(Atspi.StateType.ACTIVE):
                    return (name, win)
        except Exception:
            continue

    # 2. The window manager says so.
    pid = _x11_active_pid()
    if pid:
        for name, app in apps:
            try:
                if app.get_process_id() != pid:
                    continue
            except Exception:
                continue
            wins = _windows_of(app)
            if wins:
                return (name, wins[0])

    # 3. Something in the app holds keyboard focus.
    for name, app in apps:
        try:
            for win in _windows_of(app):
                if win.get_state_set().contains(Atspi.StateType.FOCUSED):
                    return (name, win)
        except Exception:
            continue
    return None


def get_focused_app_widgets() -> list[Widget]:
    """Return all widgets in the currently focused application."""
    found = focused_window()
    return _collect_widgets(found[1]) if found else []


# ══════════════════════════════════════════════════════════════════
#  GEOMETRY — the tree as a map rather than as a list of names
# ══════════════════════════════════════════════════════════════════
#
# Everything above treats the tree as text: find_element() fuzzy-matches a
# spoken description and clicks whatever wins. That is the right shape for
# "click the save button" and the wrong one for a remote control, where the
# operator is looking at the window and wants to press the thing they can
# see. For that the tree needs coordinates, which AT-SPI exposes through the
# Component interface — one IPC call per node, which is why only this path
# asks for them.

def _extents(acc: Any) -> tuple[int, int, int, int] | None:
    """Screen-space (x, y, w, h) of *acc*, or None if it has no geometry."""
    try:
        comp = acc.get_component_iface()
    except Exception:
        comp = None
    if comp is None:
        return None
    try:
        r = comp.get_extents(Atspi.CoordType.SCREEN)
    except Exception:
        return None
    if r is None or r.width <= 0 or r.height <= 0:
        return None
    return (int(r.x), int(r.y), int(r.width), int(r.height))


def _flags(acc: Any) -> tuple[bool, bool, bool, bool, bool]:
    """(showing, enabled, focused, checked, editable) for *acc*."""
    try:
        st = acc.get_state_set()
        return (
            st.contains(Atspi.StateType.SHOWING),
            st.contains(Atspi.StateType.SENSITIVE),
            st.contains(Atspi.StateType.FOCUSED),
            st.contains(Atspi.StateType.CHECKED),
            st.contains(Atspi.StateType.EDITABLE),
        )
    except Exception:
        return (True, True, False, False, False)


def _walk_interactive(acc: Any, bounds: tuple[int, int, int, int],
                      out: list[Widget], limit: int,
                      depth: int = 0, chain: list[str] | None = None) -> None:
    """Depth-first collect of on-screen, hittable widgets under *acc*."""
    if acc is None or depth > 15 or len(out) >= limit:
        return
    chain = chain or []
    try:
        role = acc.get_role_name() or ""
        name = acc.get_name() or ""
        desc = acc.get_description() or ""
    except Exception:
        return

    kind = _KIND.get(role)
    if kind is not None:
        showing, enabled, focused, checked, editable = _flags(acc)
        # An element that is not SHOWING is in a collapsed menu or a
        # scrolled-off region. It has stale extents and pressing it does
        # nothing useful, so it never becomes a target.
        if showing:
            rect = _extents(acc)
            # Inputs are worth a target even unnamed — that is the normal
            # state of a text field. Everything else needs something to
            # label the button with, or the operator is pressing a blank.
            labelled = bool(name or desc) or kind == "input"
            if rect and labelled and _intersects(rect, bounds):
                value = ""
                if kind == "input":
                    if not editable:
                        kind = "text"
                    value = (get_text_of(acc) or "")[:120]
                out.append(Widget(
                    name=name, role=role, description=desc,
                    accessible=acc, parent_chain=list(chain),
                    rect=rect, enabled=enabled, focused=focused,
                    checked=checked, value=value, kind=kind,
                ))

    try:
        n = acc.get_child_count()
    except Exception:
        return
    sub = chain + ([name] if name else [])
    for i in range(n):
        if len(out) >= limit:
            return
        try:
            child = acc.get_child_at_index(i)
        except Exception:
            continue
        _walk_interactive(child, bounds, out, limit, depth + 1, sub)


def _intersects(rect: tuple[int, int, int, int],
                bounds: tuple[int, int, int, int]) -> bool:
    """True when *rect* overlaps the window at all.

    Toolkits happily report extents for widgets scrolled outside their own
    window, and a target drawn a thousand pixels off the map is worse than
    no target at all.
    """
    x, y, w, h = rect
    bx, by, bw, bh = bounds
    return x < bx + bw and x + w > bx and y < by + bh and y + h > by


def get_text_of(acc: Any) -> str | None:
    """Text content of an accessible, or None if it exposes no text at all.

    The distinction matters: an empty editable field reads as "" and must
    not be reported as its own label, while a widget with no text interface
    has nothing to say and lets the caller fall back.
    """
    try:
        iface = acc.get_text_iface()
        if iface is not None:
            return iface.get_text(0, -1) or ""
    except Exception:
        pass
    try:
        return acc.get_text(0, -1) or ""
    except Exception:
        return None


def collect_interactive(limit: int = 220, win: Any = None,
                       app_name: str = "") -> dict[str, Any]:
    """A window as a map: its bounds plus every widget you can press.

    Maps the active window unless *win* is given, which lets a caller keep
    following the window it already mapped instead of whatever has focus now.

    *limit* caps the walk because a browser tab can carry thousands of
    accessible nodes, each one an IPC round trip; past a couple hundred
    targets the map is unreadable anyway.
    """
    if win is None:
        found = focused_window()
        if not found:
            return {"ok": False, "error": "no active window"}
        app_name, win = found

    try:
        title = win.get_name() or ""
    except Exception:
        title = ""
    bounds = _extents(win) or (0, 0, 1920, 1080)

    widgets: list[Widget] = []
    _walk_interactive(win, bounds, widgets, limit)
    return {
        "ok": True,
        "app": app_name,
        "window": win,
        "title": title,
        "bounds": bounds,
        "widgets": widgets,
        "truncated": len(widgets) >= limit,
    }


def find_element(description: str, threshold: int = 55) -> Widget | None:
    """Fuzzy-find the best-matching widget for a natural-language description."""
    widgets = get_focused_app_widgets()
    if not widgets:
        return None
    candidates = [w.search_text for w in widgets]
    match = process.extractOne(description, candidates, scorer=fuzz.WRatio)
    if match is None or match[1] < threshold:
        return None
    # match[2] is the index returned by rapidfuzz — avoids wrong widget when two share identical search_text
    try:
        return widgets[match[2]]
    except (IndexError, TypeError):
        return widgets[candidates.index(match[0])]


def is_showing(acc: Any) -> bool:
    """True when *acc* is currently rendered on screen."""
    try:
        return bool(acc.get_state_set().contains(Atspi.StateType.SHOWING))
    except Exception:
        return False


def get_action_count(widget: Widget) -> int:
    try:
        return widget.accessible.get_n_actions()
    except Exception:
        return 0


def do_action(widget: Widget, action_index: int = 0) -> bool:
    """Trigger the default action on a widget (usually 'click')."""
    try:
        return bool(widget.accessible.do_action(action_index))
    except Exception as e:
        logger.error("AT-SPI do_action failed: %s", e)
        return False


def get_text(widget: Widget) -> str:
    """Return the full text content of a text widget."""
    text = get_text_of(widget.accessible)
    return widget.name if text is None else text


def set_text(widget: Widget, text: str) -> bool:
    """Replace the content of an editable text widget."""
    try:
        edit_iface = widget.accessible.get_editable_text_iface()
        if edit_iface is None:
            return False
        return bool(edit_iface.set_text_contents(text))
    except Exception as e:
        logger.error("AT-SPI set_text failed: %s", e)
        return False


def list_actionable(widgets: list[Widget]) -> list[dict]:
    return [
        {"name": w.name, "role": w.role, "description": w.description}
        for w in widgets
        if get_action_count(w) > 0 or w.role in ("text", "entry")
    ]

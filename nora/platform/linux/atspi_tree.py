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

    @property
    def search_text(self) -> str:
        parts = self.parent_chain + [self.role, self.name, self.description]
        return " ".join(p for p in parts if p)


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

        ACTIONABLE = {
            "push button", "toggle button", "check box", "radio button",
            "menu item", "text", "entry", "link", "combo box", "list item",
            "tab", "tree item", "spin button", "slider",
        }
        if role in ACTIONABLE or name:
            widgets.append(Widget(name=name, role=role, description=desc,
                                  accessible=acc, parent_chain=parent_chain))

        for i in range(acc.get_child_count()):
            child = acc.get_child_at_index(i)
            widgets.extend(_collect_widgets(child, depth + 1, chain))
    except Exception:
        pass
    return widgets


def get_focused_app_widgets() -> list[Widget]:
    """Return all widgets in the currently focused application."""
    _ensure_glib_loop()
    try:
        desktop = Atspi.get_desktop(0)
        # find the focused app
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            try:
                if app is None:
                    continue
                for j in range(app.get_child_count()):
                    win = app.get_child_at_index(j)
                    if win and win.get_state_set().contains(Atspi.StateType.ACTIVE):
                        return _collect_widgets(win)
            except Exception:
                continue
    except Exception as e:
        logger.error("AT-SPI walk failed: %s", e)
    return []


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
    try:
        text_iface = widget.accessible.get_text_iface()
        if text_iface is not None:
            return text_iface.get_text(0, -1) or ""
    except Exception:
        pass
    try:
        return widget.accessible.get_text(0, -1) or ""
    except Exception:
        return widget.name


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

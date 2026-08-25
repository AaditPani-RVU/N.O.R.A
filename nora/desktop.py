"""The desktop remote — the accessibility tree as a control surface.

`nora.commands.atspi_actions` already drives the desktop through AT-SPI, but
only ever by description: you say "click save" and a fuzzy match decides what
that meant. `list_buttons_in_window()` is the sharpest illustration — it walks
the whole tree of the focused window and then flattens it into one spoken
sentence, "Found 34 interactive elements: 'Save' (push button), ...", throwing
away the geometry that would let you simply press the one you can see.

This module keeps the geometry. It hands the dashboard the active window as a
map — bounds, plus every showing, hittable widget at its true screen rect —
and takes back a tap. On a phone that turns the dashboard into a real remote
for the machine, and it works for the case voice cannot: an element you can
point at but cannot name.

Two things it has to get right:

  Identity across requests. The dashboard taps element 17 of a snapshot it
  fetched a second ago, and 17 has to still mean the same widget. AT-SPI
  accessibles are live object references, so the snapshot holds them and a
  tap resolves through it — but the window may have moved on, so every act()
  re-checks the target before touching it.

  Not being the input path. Where a widget exposes a real AT-SPI action, we
  call it: it is deterministic, it needs no input synthesis, and it does not
  steal the pointer out from under the person at the keyboard. Synthetic
  clicks through ydotool are the fallback for widgets that expose nothing.

Platform: Linux + AT-SPI2 only. Everywhere else available() is False and the
routes report that rather than failing an import.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

logger = logging.getLogger("nora.desktop")

_lock = threading.Lock()

# Only the newest snapshot is kept. A tap always follows a fetch, and holding
# a history of live accessible references would pin objects for windows that
# closed minutes ago.
_snap_id: str = ""
_snap_widgets: list[Any] = []
_snap_at: float = 0.0
# The window this view is of. Refreshing follows *this* window rather than
# re-reading focus, because the moment the operator touches the dashboard on
# the same machine the focused window becomes the browser showing the
# dashboard, and the remote would spend the rest of the session mapping
# itself.
_snap_win: Any = None
_snap_app: str = ""

# Past this a snapshot is refused rather than acted on. The window has almost
# certainly changed, and pressing whatever is at index 17 now is exactly the
# kind of surprise a remote control must not produce.
_SNAP_TTL = 45.0


def _tree() -> Any:
    """The AT-SPI walker, or None where it cannot be imported.

    atspi_tree does `gi.require_version` at module scope, so importing it on
    a machine without pygobject raises at import time, not call time — hence
    the guard here rather than a top-level import.
    """
    try:
        from nora.platform.linux import atspi_tree
        return atspi_tree
    except Exception as exc:
        logger.debug("AT-SPI unavailable: %s", exc)
        return None


def available() -> bool:
    return _tree() is not None


def window_map(limit: int = 220, follow: bool = False) -> dict[str, Any]:
    """A window as a JSON-safe map of pressable targets.

    *follow* re-maps the window this view is already showing instead of
    whichever window currently has focus — what a refresh from the dashboard
    wants. It falls back to the focused window when the followed one has
    closed.
    """
    global _snap_id, _snap_widgets, _snap_at, _snap_win, _snap_app

    tree = _tree()
    if tree is None:
        return {"ok": False, "error": "AT-SPI not available on this machine"}

    win, app = None, ""
    if follow:
        with _lock:
            win, app = _snap_win, _snap_app
        if win is not None and not tree.is_showing(win):
            win, app = None, ""

    try:
        got = tree.collect_interactive(limit=limit, win=win, app_name=app)
    except Exception as exc:
        logger.exception("desktop map failed")
        return {"ok": False, "error": str(exc)}

    if not got.get("ok"):
        return got

    widgets = got.get("widgets") or []
    bx, by, bw, bh = got["bounds"]

    elements = []
    for i, w in enumerate(widgets):
        x, y, ew, eh = w.rect
        elements.append({
            "id": i,
            "name": w.name[:60],
            "role": w.role,
            "kind": w.kind,
            "desc": w.description[:80],
            # Window-relative, so the dashboard can lay the map out without
            # knowing anything about monitor origins or multi-head offsets.
            "x": x - bx, "y": y - by, "w": ew, "h": eh,
            "enabled": w.enabled,
            "focused": w.focused,
            "checked": w.checked,
            "value": w.value,
        })

    # Some toolkits — GTK4's accessibility backend among them — register a
    # full tree but never fill in Component extents, so every control comes
    # back at the window origin. Drawing that gives you a hundred targets
    # stacked in one corner, which is worse than not drawing a map at all,
    # so say so and let the dashboard fall back to a plain list.
    positional = _looks_positional(elements)

    with _lock:
        _snap_id = uuid.uuid4().hex[:12]
        _snap_widgets = widgets
        _snap_win = got.get("window")
        _snap_app = got.get("app", "")
        _snap_at = time.time()
        snap = _snap_id

    return {
        "ok": True,
        "snap": snap,
        "app": got.get("app", ""),
        "title": got.get("title", ""),
        "bounds": {"x": bx, "y": by, "w": bw, "h": bh},
        "elements": elements,
        "positional": positional,
        "truncated": got.get("truncated", False),
        "ts": _snap_at,
    }


def _looks_positional(elements: list[dict[str, Any]]) -> bool:
    """True when the reported rects actually describe a layout.

    Two or three controls legitimately sharing an origin is a stacked
    container; nearly all of them sharing one is a toolkit that does not
    implement the Component interface.
    """
    if len(elements) < 3:
        return True
    origins = {(e["x"], e["y"]) for e in elements}
    return len(origins) > max(2, len(elements) * 0.35)


def _resolve(snap: str, eid: int) -> tuple[Any, str]:
    """The live widget for (*snap*, *eid*), or (None, reason)."""
    with _lock:
        if not _snap_id or snap != _snap_id:
            return None, "stale view — refresh and try again"
        if time.time() - _snap_at > _SNAP_TTL:
            return None, "view expired — refresh and try again"
        widgets = _snap_widgets
    if not isinstance(eid, int) or not (0 <= eid < len(widgets)):
        return None, f"no element {eid}"
    return widgets[eid], ""


def _still_there(tree: Any, widget: Any) -> bool:
    """True when the widget is the same one we mapped and is still on screen.

    Cheap re-read of name/role plus SHOWING. It cannot catch every case of a
    window rebuilding its tree under us, but it catches the common one — a
    dialog closed between the fetch and the tap — and that is the case where
    acting blind would press something the operator never saw.
    """
    try:
        acc = widget.accessible
        if (acc.get_name() or "") != widget.name:
            return False
        if (acc.get_role_name() or "") != widget.role:
            return False
        return tree.is_showing(acc)
    except Exception:
        return False


def act(snap: str, eid: int, action: str = "click", text: str = "") -> dict[str, Any]:
    """Press, focus, or fill the element *eid* of snapshot *snap*."""
    tree = _tree()
    if tree is None:
        return {"ok": False, "error": "AT-SPI not available on this machine"}

    widget, why = _resolve(snap, eid)
    if widget is None:
        return {"ok": False, "error": why, "stale": True}

    if not _still_there(tree, widget):
        return {"ok": False, "error": "that element is gone — refresh",
                "stale": True}

    label = widget.name or widget.description or widget.role

    if action == "click":
        return _do_click(tree, widget, label)
    if action == "focus":
        return {"ok": _grab_focus(widget), "action": "focus", "label": label}
    if action == "type":
        return _do_type(tree, widget, label, text)
    return {"ok": False, "error": f"unknown action {action!r}"}


def _do_click(tree: Any, widget: Any, label: str) -> dict[str, Any]:
    # A real AT-SPI action is always preferable: it is what the application
    # itself calls, it works identically under Wayland and X11, and it does
    # not yank the pointer away from whoever is using the machine.
    try:
        if tree.get_action_count(widget) > 0 and tree.do_action(widget, 0):
            return {"ok": True, "action": "click", "via": "atspi", "label": label}
    except Exception:
        logger.debug("do_action failed for %r", label, exc_info=True)

    rect = widget.rect
    if not rect:
        return {"ok": False, "error": f"{label} exposes no action and has no position"}
    try:
        from nora.platform.linux import ydotool_input
    except Exception as exc:
        return {"ok": False, "error": f"no fallback input available: {exc}"}

    x, y, w, h = rect
    ok = ydotool_input.click(x + w // 2, y + h // 2)
    return {"ok": bool(ok), "action": "click", "via": "pointer", "label": label,
            **({} if ok else {"error": "pointer click failed"})}


def _do_type(tree: Any, widget: Any, label: str, text: str) -> dict[str, Any]:
    if widget.kind not in ("input", "combo", "text"):
        return {"ok": False, "error": f"{label} is not a text field"}
    try:
        if tree.set_text(widget, text):
            return {"ok": True, "action": "type", "via": "atspi", "label": label}
    except Exception:
        logger.debug("set_text failed for %r", label, exc_info=True)

    # Some toolkits expose the editable interface but refuse set_text (web
    # inputs especially). Focusing and synthesising the keystrokes is what a
    # person would do next.
    if not _grab_focus(widget):
        return {"ok": False, "error": f"could not focus {label}"}
    try:
        from nora.platform.linux import ydotool_input
    except Exception as exc:
        return {"ok": False, "error": f"no fallback input available: {exc}"}
    ok = ydotool_input.type_text(text)
    return {"ok": bool(ok), "action": "type", "via": "keyboard", "label": label,
            **({} if ok else {"error": "keyboard synthesis failed"})}


def _grab_focus(widget: Any) -> bool:
    try:
        comp = widget.accessible.get_component_iface()
        if comp is not None:
            return bool(comp.grab_focus())
    except Exception:
        logger.debug("grab_focus failed", exc_info=True)
    return False

"""Voice commands for AT-SPI2 semantic screen control (Linux).

Uses the accessibility tree instead of OCR — deterministic, fast, works
under both Wayland and X11. Falls back to a log warning (not a crash) if
AT-SPI or ydotool are unavailable.
"""
from __future__ import annotations

import asyncio
import logging

from nora.command_engine import register
from nora.schemas import StepResult

logger = logging.getLogger("nora.commands.atspi")


def _tree():
    from nora.platform.linux import atspi_tree
    return atspi_tree


def _input():
    from nora.platform.linux import ydotool_input
    return ydotool_input


@register(
    "click_element",
    sig="click_element(description: str)",
    description="Find a UI element by description and click it using the accessibility tree (Linux, Wayland/X11).",
    risk="medium",
    requires_confirmation=False,
    category="screen",
)
async def click_element(description: str) -> StepResult:
    loop = asyncio.get_event_loop()
    tree = _tree()

    widget = await loop.run_in_executor(None, tree.find_element, description)
    if widget is None:
        return StepResult(
            success=False,
            message=f"Could not find a UI element matching '{description}'.",
        )

    if tree.get_action_count(widget) > 0:
        ok = await loop.run_in_executor(None, tree.do_action, widget, 0)
        if ok:
            return StepResult(success=True, message=f"Clicked '{widget.name}' ({widget.role}).")
        return StepResult(success=False, message=f"AT-SPI action failed for '{widget.name}'.")

    # fallback: try ydotool coordinate click via bounding box
    try:
        bbox = widget.accessible.get_extents(Atspi.CoordType.SCREEN)
        cx = bbox.x + bbox.width // 2
        cy = bbox.y + bbox.height // 2
        inp = _input()
        ok = await loop.run_in_executor(None, inp.click, cx, cy)
        if ok:
            return StepResult(success=True, message=f"Clicked '{widget.name}' at ({cx},{cy}).")
    except Exception as e:
        logger.debug("Bounding box click fallback failed: %s", e)

    return StepResult(success=False, message=f"Could not interact with '{widget.name}'.")


@register(
    "read_focused_field",
    sig="read_focused_field()",
    description="Read the text content of the currently focused input field.",
    risk="low",
    requires_confirmation=False,
    category="screen",
)
async def read_focused_field() -> StepResult:
    loop = asyncio.get_event_loop()
    tree = _tree()

    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
        focused = await loop.run_in_executor(None, Atspi.get_desktop(0).get_child_at_index, 0)
    except Exception:
        pass

    # walk and find the widget with STATE_FOCUSED
    widgets = await loop.run_in_executor(None, tree.get_focused_app_widgets)
    for w in widgets:
        try:
            if w.accessible.get_state_set().contains(__import__('gi').repository.Atspi.StateType.FOCUSED):
                text = await loop.run_in_executor(None, tree.get_text, w)
                return StepResult(success=True, message=f"Focused field '{w.name}' contains: {text}")
        except Exception:
            continue
    return StepResult(success=False, message="No focused text field found.")


@register(
    "list_buttons_in_window",
    sig="list_buttons_in_window()",
    description="List all clickable buttons and links in the active window.",
    risk="low",
    requires_confirmation=False,
    category="screen",
)
async def list_buttons_in_window() -> StepResult:
    loop = asyncio.get_event_loop()
    tree = _tree()
    widgets = await loop.run_in_executor(None, tree.get_focused_app_widgets)
    buttons = [
        w for w in widgets
        if w.role in ("push button", "toggle button", "link", "menu item", "tab")
        and w.name
    ]
    if not buttons:
        return StepResult(success=False, message="No buttons found in the active window.")
    names = ", ".join(f"'{b.name}' ({b.role})" for b in buttons[:20])
    return StepResult(success=True, message=f"Found {len(buttons)} interactive elements: {names}.")


@register(
    "fill_field",
    sig="fill_field(label: str, text: str)",
    description="Find a text input by its label and type the given text into it.",
    risk="medium",
    requires_confirmation=False,
    category="screen",
)
async def fill_field(label: str, text: str) -> StepResult:
    loop = asyncio.get_event_loop()
    tree = _tree()

    query = f"text entry input field {label}"
    widget = await loop.run_in_executor(None, tree.find_element, query)
    if widget is None:
        return StepResult(success=False, message=f"Could not find input field '{label}'.")

    ok = await loop.run_in_executor(None, tree.set_text, widget, text)
    if ok:
        return StepResult(success=True, message=f"Filled '{label}' with '{text}'.")
    # fallback: click + type
    try:
        inp = _input()
        bbox = widget.accessible.get_extents(__import__('gi').repository.Atspi.CoordType.SCREEN)
        await loop.run_in_executor(None, inp.click, bbox.x + bbox.width // 2, bbox.y + bbox.height // 2)
        await asyncio.sleep(0.1)
        await loop.run_in_executor(None, inp.key, "ctrl+a")
        await loop.run_in_executor(None, inp.type_text, text)
        return StepResult(success=True, message=f"Filled '{label}' via keyboard input.")
    except Exception as e:
        return StepResult(success=False, message=f"Failed to fill '{label}': {e}")


@register(
    "read_dialog",
    sig="read_dialog()",
    description="Read and describe the content of the topmost dialog or alert in the active window.",
    risk="low",
    requires_confirmation=False,
    category="screen",
)
async def read_dialog() -> StepResult:
    loop = asyncio.get_event_loop()
    tree = _tree()
    widgets = await loop.run_in_executor(None, tree.get_focused_app_widgets)
    dialogs = [w for w in widgets if w.role in ("dialog", "alert", "frame", "window")]
    if not dialogs:
        return StepResult(success=False, message="No dialog found in the active window.")
    d = dialogs[0]
    children = [w for w in widgets if w.parent_chain and d.name in w.parent_chain]
    summary = f"Dialog '{d.name}': " + "; ".join(
        f"{w.role} '{w.name}'" for w in children[:10] if w.name
    )
    return StepResult(success=True, message=summary)


@register(
    "type_into_focused",
    sig="type_into_focused(text: str)",
    description="Type text into whatever field is currently focused on screen.",
    risk="medium",
    requires_confirmation=False,
    category="screen",
)
async def type_into_focused(text: str) -> StepResult:
    loop = asyncio.get_event_loop()
    inp = _input()
    ok = await loop.run_in_executor(None, inp.type_text, text)
    if ok:
        return StepResult(success=True, message=f"Typed: {text}")
    return StepResult(success=False, message="ydotool type failed — is ydotool installed?")


@register(
    "press_key",
    sig="press_key(keys: str)",
    description="Press a keyboard shortcut, e.g. 'Return', 'ctrl+l', 'alt+F4'.",
    risk="medium",
    requires_confirmation=False,
    category="screen",
)
async def press_key(keys: str) -> StepResult:
    loop = asyncio.get_event_loop()
    inp = _input()
    ok = await loop.run_in_executor(None, inp.key, keys)
    if ok:
        return StepResult(success=True, message=f"Pressed {keys}.")
    return StepResult(success=False, message=f"Key press failed for '{keys}'.")

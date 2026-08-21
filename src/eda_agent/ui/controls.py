# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Read and SET the controls inside an Altium dialog.

Buttons were the easy half: a dialog is only fully drivable when its
checkboxes, radios, fields and lists can be inspected and changed.

WHAT MAKES THIS POSSIBLE. Altium's VCL controls are opaque to plain
Win32 (a TXPCheckBox is not the stock "Button" class, so BM_GETCHECK
tells you nothing), but they ARE described accessibly. MEASURED on the
Update From Library wizard:

  TXPCheckBox    'Update To Latest Revision'  role=checkbox     value='Checked'
  TXPRadioButton 'Fully replace symbols ...'  role=radiobutton  value='Checked'
  TXPComboBox    'All Components'             role=combobox     value='All Components'
  TdxTreeList    ''                           role=list         value='Grid_Sheets'

So STATE is read from MSAA, which is reliable, and CHANGE is made by
clicking, which is the only thing these controls answer to. Every
setter then READS THE STATE BACK and reports whether it took, because a
click that silently did nothing is the failure mode that matters: it
would leave a caller believing a setting was applied.

WHAT IS DELIBERATELY NOT GUESSED. A control reporting 'unavailable' is
refused rather than clicked; Altium greys options that do not apply to
the current selection, and clicking one is at best a no-op. Grids are
readable and selectable by row text, but their cells are not editable
from here, which is stated rather than papered over.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import POINTER, byref, c_long, wintypes
from typing import Optional

from . import windows as win

try:                                            # pragma: no cover - platform
    import comtypes
    import comtypes.client
    from comtypes import COMError
    from comtypes.automation import VARIANT

    comtypes.client.GetModule("oleacc.dll")
    from comtypes.gen.Accessibility import IAccessible

    import win32con
    import win32gui
    _AVAILABLE = True
except Exception:                               # pragma: no cover - platform
    _AVAILABLE = False

_OBJID_CLIENT = 0xFFFFFFFC

#: MSAA roles, the ones that matter for driving a dialog.
ROLES = {
    0x2B: "button", 0x2C: "checkbox", 0x2D: "radiobutton",
    0x2E: "combobox", 0x2F: "droplist", 0x2A: "text",
    0x29: "statictext", 0x21: "list", 0x22: "listitem",
    0x23: "outline", 0x24: "outlineitem", 0x18: "table", 0x1D: "cell",
    0x33: "spinbutton", 0x28: "slider", 0x10: "pane", 0x0A: "client",
}

_STATE_UNAVAILABLE = 0x00000001
_STATE_CHECKED = 0x00000010
_STATE_SELECTED = 0x00000002

#: Roles a click toggles or selects. Anything else is not set this way.
SETTABLE = ("checkbox", "radiobutton")


def available() -> bool:
    return _AVAILABLE and win.available()


def _acc(hwnd):
    ptr = POINTER(IAccessible)()
    ctypes.oledll.oleacc.AccessibleObjectFromWindow(
        ctypes.c_void_p(hwnd), ctypes.c_ulong(_OBJID_CLIENT),
        byref(IAccessible._iid_), byref(ptr))
    return ptr.QueryInterface(IAccessible)


def _self(child: int = 0):
    var = VARIANT()
    var.vt = 3
    var.value = child
    return var


def _ask(node, attr, child=0):
    try:
        return getattr(node, attr)(_self(child))
    except COMError:
        return None


def read(hwnd: int) -> dict:
    """Everything known about one control: role, value, checked, enabled.

    ``checked`` is None for controls where the idea does not apply, so a
    caller can tell "not a checkbox" from "a checkbox that is off".
    """
    if not available():
        return {"ok": False, "reason": "accessibility is unavailable here"}
    try:
        node = _acc(hwnd)
    except Exception:                            # pragma: no cover - guard
        return {"ok": False, "reason": "no accessible object for that window"}

    role_id = _ask(node, "accRole")
    state = _ask(node, "accState") or 0
    value = _ask(node, "accValue")
    role = ROLES.get(role_id, role_id)
    checked = None
    if role in ("checkbox", "radiobutton"):
        checked = bool(state & _STATE_CHECKED)
    return {
        "ok": True,
        "role": role,
        "name": _ask(node, "accName") or "",
        "value": value,
        "checked": checked,
        "enabled": not bool(state & _STATE_UNAVAILABLE),
        "selected": bool(state & _STATE_SELECTED),
        "hwnd": hwnd,
    }


def describe_all(window) -> list:
    """Every control of a captured window, with its accessible state.

    Containers are dropped: a dialog has far more panels than controls
    and listing them buries the parts a caller can act on.
    """
    out = []
    for control in window.controls:
        info = read(control.hwnd)
        if not info.get("ok"):
            continue
        if info["role"] in ("pane", "client") and not control.text:
            continue
        info["class"] = control.class_name
        info["label"] = control.text
        out.append(info)
    return out


def find(window, label: str, role: Optional[str] = None):
    """One control by its visible label, ignoring & and case.

    Matching on the label rather than an index is the same choice made
    for menus, for the same reason: positions move, captions do not.
    """
    def flat(text):
        return str(text or "").replace("&", "").strip().lower()

    wanted = flat(label)
    for info in describe_all(window):
        if flat(info["label"]) == wanted or flat(info["name"]) == wanted:
            if role is None or info["role"] == role:
                return info
    return None


def _click(hwnd: int) -> None:
    """Press a VCL control the way Altium's controls accept.

    Posted button messages, addressed to the control's own handle, which
    is what TXPBitBtn was measured to answer. Nothing is synthesised
    globally and the cursor does not move.
    """
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN,
                         win32con.MK_LBUTTON, 0)
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, 0)


def set_checked(hwnd: int, want: bool, settle: float = 0.35) -> dict:
    """Set a checkbox, and VERIFY it took.

    Clicks only when the current state differs, so calling this twice is
    not a toggle, and reads the state back afterwards. A click that
    silently fails is the whole risk here: it would leave a caller
    believing an option was applied.
    """
    before = read(hwnd)
    if not before.get("ok"):
        return before
    if before["role"] not in SETTABLE:
        return {"ok": False, "reason": (
            f"{before['name']!r} is a {before['role']}, which is not set "
            f"by checking it")}
    if not before["enabled"]:
        return {"ok": False, "reason": (
            f"{before['name']!r} is disabled, so Altium does not accept "
            f"this setting in the current state. Refusing to click it")}
    if before["role"] == "radiobutton" and not want:
        return {"ok": False, "reason": (
            "a radio button cannot be switched off, only another one in "
            "its group switched on")}

    if before["checked"] == want:
        return {"ok": True, "changed": False, "checked": want,
                "note": "already in that state"}

    _click(hwnd)
    time.sleep(settle)
    after = read(hwnd)
    if after.get("checked") != want:
        return {"ok": False, "changed": False,
                "checked": after.get("checked"),
                "reason": (f"clicked {before['name']!r} but it is still "
                           f"{'checked' if after.get('checked') else 'unchecked'}"
                           f". The control did not accept the press")}
    return {"ok": True, "changed": True, "checked": want,
            "name": before["name"]}


def set_text(hwnd: int, text: str, settle: float = 0.2) -> dict:
    """Put text into an edit field, and read it back.

    WM_SETTEXT rather than synthesised typing: it is addressed to the
    control and cannot land somewhere else if focus moves.
    """
    before = read(hwnd)
    if not before.get("ok"):
        return before
    if not before["enabled"]:
        return {"ok": False, "reason": f"{before['name']!r} is disabled"}
    try:
        win32gui.SendMessage(hwnd, win32con.WM_SETTEXT, 0, str(text))
    except Exception as exc:                     # noqa: BLE001
        return {"ok": False, "reason": f"WM_SETTEXT failed: {exc}"}
    time.sleep(settle)
    got = win.read_text(hwnd)
    if got != str(text):
        return {"ok": False, "reason": (
            f"set {text!r} but the field reads {got!r}. Some VCL fields "
            f"ignore WM_SETTEXT and only accept typed input")}
    return {"ok": True, "text": got}


def _child_objects(node) -> list:
    """Child IAccessible OBJECTS, which is what a grid actually returns.

    The first version asked for simple child IDs (accName(1), accName(2)
    ...) and got nothing, and the empty result was reported as "this
    tree has no rows". MEASURED on Altium's TdxTreeList: accChildCount
    is 65 and every child arrives as a full object, not an id. The rows
    were always there; the question was wrong.
    """
    try:
        count = node.accChildCount
    except COMError:
        return []
    if not count:
        return []
    arr = (VARIANT * count)()
    got = c_long()
    try:
        ctypes.oledll.oleacc.AccessibleChildren(node, 0, count, arr,
                                                byref(got))
    except Exception:                            # pragma: no cover - guard
        return []
    out = []
    for i in range(got.value):
        if arr[i].vt == 9 and arr[i].value:
            try:
                out.append(arr[i].value.QueryInterface(IAccessible))
            except COMError:
                pass
    return out


def list_rows(hwnd: int) -> list:
    """Every row of a list, grid or tree, with its name and location.

    Scrollbars are dropped: a grid reports them as children and they
    are not rows.
    """
    if not available():
        return []
    try:
        node = _acc(hwnd)
    except Exception:                            # pragma: no cover - guard
        return []
    rows = []
    for child in _child_objects(node):
        name = _ask(child, "accName")
        if not name or not str(name).strip():
            continue
        if "scrollbar" in str(name).lower():
            continue
        rows.append({"name": str(name).strip(), "node": child})
    return rows


def list_items(hwnd: int) -> list:
    """Just the row labels."""
    return [row["name"] for row in list_rows(hwnd)]


def select_row(hwnd: int, name: str, settle: float = 0.6) -> dict:
    """Select a row by name, and VERIFY the selection took.

    Tried in order: the accessible selection call, then a click on the
    row's own rectangle. DevExpress exposes rows read-only in places,
    so the click is the fallback that actually lands, the same pattern
    the menus needed.
    """
    rows = list_rows(hwnd)
    if not rows:
        return {"ok": False, "reason": "that control exposes no rows"}

    def flat(text):
        return str(text or "").strip().lower()

    match = next((r for r in rows if flat(r["name"]) == flat(name)), None)
    if match is None:
        return {"ok": False,
                "reason": f"no row named {name!r}",
                "offered": [r["name"] for r in rows][:60]}

    node = match["node"]
    try:
        node.accSelect(3, _self())   # TAKEFOCUS | TAKESELECTION
        time.sleep(settle)
    except COMError:
        pass
    if _row_selected(node):
        return {"ok": True, "row": match["name"], "how": "accSelect"}

    try:
        x, y, width, height = node.accLocation(_self())
    except COMError:
        return {"ok": False, "reason": (
            f"{name!r} could not be selected and reports no location to "
            f"click")}
    if width <= 0 or x < -10000:
        return {"ok": False, "reason": (
            f"{name!r} is not on screen; the list may need scrolling, "
            f"which is not implemented")}
    # A real click on the row's own rectangle, and ONLY that. An earlier
    # version also posted a button message to the tree with lParam 0,
    # which is client coordinate (0,0): that lands on the FIRST row and
    # selected the wrong one before the real click ever happened.
    _real_click(x + width // 2, y + height // 2)
    time.sleep(settle)
    if _row_selected(node):
        return {"ok": True, "row": match["name"], "how": "click"}
    return {"ok": False, "row": match["name"],
            "reason": (f"clicked {match['name']!r} but it does not report "
                       f"as selected")}


def _row_selected(node) -> bool:
    state = _ask(node, "accState") or 0
    return bool(state & (_STATE_SELECTED | 0x00000004))   # selected|focused


def _real_click(x: int, y: int) -> None:
    """Real click, cursor restored. Needed where posted messages are
    ignored, which is the case for DevExpress grid rows."""
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    point = wintypes.POINT()
    user32.GetCursorPos(byref(point))
    origin = (point.x, point.y)
    user32.SetCursorPos(x, y)
    time.sleep(0.15)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.06)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.25)
    user32.SetCursorPos(*origin)

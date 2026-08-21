# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Invoke ANY Altium menu command, by path, when nothing scriptable will.

THIS IS THE ONE PLACE THAT SYNTHESISES REAL INPUT. Everywhere else in
this package addresses messages to a specific window handle, so nothing
depends on focus, z-order or where the pointer is. That property cannot
be kept here, and every step below exists because the safer one was
tried against a live Altium and did nothing:

  application.execute_menu     reports success and invokes nothing, for
                               MAPPED paths too. RunProcess silently
                               ignores ids it does not know and cannot
                               report failure (task #83)
  GetMenu on the main frame    returns 0. Altium draws menus with
                               DevExpress bars: no menu ids, no
                               WM_COMMAND route
  accDoDefaultAction           returns cleanly and raises no popup, on
                               bar items AND on submenu items. The bars
                               expose MSAA READ-ONLY: names and
                               rectangles yes, actions no
  posted WM_LBUTTONDOWN/UP     ignored by the bar, unlike Altium's
                               TXPBitBtn which accepts it
  a REAL click                 WORKS, at every level. The popup arrives
                               as a TxpBarSubMenuControl whose items ARE
                               exposed accessibly, so each level is
                               found BY NAME and clicked at its own
                               rectangle

Names, never positions: entries move with context and separators are
not exposed at all, so an index would select the wrong thing the first
time a menu changed.

BECAUSE IT STEALS FOCUS, IT IS NOT A DEFAULT ANYWHERE. A caller asks for
it explicitly, and should not while the user is typing.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import POINTER, byref, c_long, wintypes

from . import windows as win

try:                                            # pragma: no cover - platform
    import comtypes
    import comtypes.client
    from comtypes import COMError
    from comtypes.automation import VARIANT

    comtypes.client.GetModule("oleacc.dll")
    from comtypes.gen.Accessibility import IAccessible

    import win32gui
    _AVAILABLE = True
except Exception:                               # pragma: no cover - platform
    _AVAILABLE = False

_OBJID_CLIENT = 0xFFFFFFFC
_VK_ESC = 0x1B
_KEYUP = 2
_SW_RESTORE = 9
_MOUSE_DOWN, _MOUSE_UP = 0x0002, 0x0004
_FRAME_CLASS = "TDocumentForm"


class MenuBarUnavailable(RuntimeError):
    """The menu bar cannot be read in the editor's current state."""


def available() -> bool:
    """Whether menus can be driven at all on this host."""
    return _AVAILABLE and win.available()


def _u():
    return ctypes.windll.user32


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


def _children(node) -> list:
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


def _name(node) -> str:
    try:
        return (node.accName(_self()) or "").strip()
    except COMError:
        return ""


def _same(a: str, b: str) -> bool:
    """Compare captions ignoring the accelerator marker and an ellipsis.

    A caller should not have to know that Altium writes "&Rules..." when
    the menu shows "Rules".
    """
    def flat(text):
        return (str(text or "").replace("&", "").strip()
                .rstrip(".").strip().lower())
    return flat(a) == flat(b)


def _visible_rect(node):
    try:
        rect = node.accLocation(_self())
    except COMError:
        return None
    x, _y, width, height = rect
    if width <= 0 or height <= 0 or x < -10000:
        return None
    return rect


def _tap(vk: int) -> None:
    _u().keybd_event(vk, 0, 0, 0)
    time.sleep(0.08)
    _u().keybd_event(vk, 0, _KEYUP, 0)


def _click(x: int, y: int) -> None:
    """A real click, with the pointer returned to where the user left it."""
    user32 = _u()
    point = wintypes.POINT()
    user32.GetCursorPos(byref(point))
    origin = (point.x, point.y)
    user32.SetCursorPos(x, y)
    time.sleep(0.2)
    user32.mouse_event(_MOUSE_DOWN, 0, 0, 0, 0)
    time.sleep(0.08)
    user32.mouse_event(_MOUSE_UP, 0, 0, 0, 0)
    time.sleep(0.3)
    user32.SetCursorPos(*origin)


def _click_node(node) -> bool:
    rect = _visible_rect(node)
    if rect is None:
        return False
    x, y, width, height = rect
    _click(x + width // 2, y + height // 2)
    return True


def frame(pid: int):
    """Altium's main document frame, or None."""
    for window in win.enumerate_windows(pid=pid):
        if window.class_name == _FRAME_CLASS:
            return window
    return None


def _submenus(pid: int) -> list:
    return [x for x in win.enumerate_windows(pid=pid, visible_only=False)
            if "submenu" in x.class_name.lower()
            and win32gui.IsWindowVisible(x.hwnd)]


def close_open_menu(pid: int) -> None:
    """Escape any menu left dropped, so the next run starts clean."""
    for _ in range(3):
        if not _submenus(pid):
            return
        _tap(_VK_ESC)
        time.sleep(0.4)


def bar_items(pid: int, timeout: float = 6.0) -> dict:
    """Top-level menu names currently ON SCREEN, mapped to their node.

    Altium's menu bar is per editor context and only the active one is
    laid out: with a board focused the schematic bar still exists but
    every item reports a rectangle far off screen. Filtering on a
    visible rectangle is what picks the live bar.

    POLLED, not read once. The bar takes a moment to lay out after the
    window is restored or the focused document changes, and a single
    read during that window returns nothing, which is indistinguishable
    from an editor that has no menus. Measured: empty immediately after
    a restore, populated a moment later.
    """
    deadline = time.monotonic() + timeout
    while True:
        found = _bar_items_once(pid)
        if found:
            return found
        if time.monotonic() >= deadline:
            # Empty after polling is not "this editor has no menus".
            # MEASURED: Altium lays its bars out on ACTIVATION, not on
            # restore. Straight after a restore the bar windows exist,
            # have sensible rectangles, and are still IsWindowVisible
            # false, and Altium had even recreated them under new
            # handles. Reading returns nothing until the frame is
            # activated, which steals focus and is therefore not
            # something a read may do on its own.
            target = frame(pid)
            if target is not None and _u().GetForegroundWindow() != target.hwnd:
                raise MenuBarUnavailable(
                    "the menu bar is not laid out because Altium is not "
                    "the active window. Altium builds its bars on "
                    "activation, so the window has to be brought to the "
                    "front first, which steals focus")
            # Foreground and still nothing. Whatever the cause, a
            # running Altium HAS menus, so reporting an empty bar would
            # be a false clean of the kind this project keeps finding.
            raise MenuBarUnavailable(
                "no menu bar could be read even though Altium is the "
                "active window. Its bars exist but report no visible "
                "items, which usually clears after the editor finishes "
                "rebuilding them")
        time.sleep(0.25)


def _bar_items_once(pid: int) -> dict:
    target = frame(pid)
    if target is None:
        return {}
    if _u().IsIconic(target.hwnd):
        # A minimized frame parks every child at -32000, so the visible
        # rectangle test below rejects everything and this returns
        # nothing. Returning an empty menu bar for a running editor is a
        # lie, and it is the kind that reads as "this editor has no
        # menus" rather than "look again once it is on screen".
        raise MenuBarUnavailable(
            "the Altium window is minimized, so its menu bar is not laid "
            "out and no item can be located or clicked")
    bars: list = []

    def visit(hwnd, _):
        try:
            cls = win32gui.GetClassName(hwnd)
        except Exception:                        # pragma: no cover - races
            return True
        if "clientbarcontrol" in cls.lower() and win32gui.IsWindowVisible(hwnd):
            bars.append(hwnd)
        return True

    win32gui.EnumChildWindows(target.hwnd, visit, None)

    # Only the MENU bar, not every bar on screen. Altium's toolbars are
    # the same control class and expose their buttons the same way, so
    # scraping them all returned fifty-odd names including Copy, Cut and
    # Arc (Edge), and took fifteen seconds. The menu bar is identified
    # by carrying both File and Help, which no toolbar does.
    for hwnd in bars:
        try:
            node = _acc(hwnd)
        except Exception:                        # pragma: no cover - guard
            continue
        items = {}
        for item in _children(node):
            caption = _name(item)
            if caption and _visible_rect(item) is not None:
                items.setdefault(caption.replace("&", ""), item)
        lowered = {k.lower() for k in items}
        if "file" in lowered and "help" in lowered:
            return items
    return {}


def _open_items(pid: int, timeout: float = 6.0) -> list:
    """Items of whatever popups are open, WAITING for them to populate.

    A menu window exists before its entries do. A fixed sleep read it
    too early and reported an empty menu, which is indistinguishable
    from a menu that genuinely lacks the item: measured, the same path
    failed once and worked on the retry. Polling removes the race and
    is faster than a sleep long enough to be safe.
    """
    deadline = time.monotonic() + timeout
    while True:
        out = []
        for popup in _submenus(pid):
            try:
                out.extend(_children(_acc(popup.hwnd)))
            except Exception:                    # pragma: no cover - guard
                pass
        if out or time.monotonic() >= deadline:
            return out
        time.sleep(0.2)


def click_path(pid: int, path: str, settle: float = 1.2) -> dict:
    """Invoke a menu command by path, e.g. ``Design|Rules...``.

    Each level is matched by NAME against what is actually on screen and
    clicked at its own rectangle. Nested submenus work by repetition:
    every level after the first is looked for among whatever popups are
    open at that moment.

    Args:
        pid: the Altium process.
        path: pipe-separated, top level first. Accelerator markers and a
            trailing ellipsis are ignored, so "Design|Rules" and
            "&Design|Rules..." both work.
        settle: pause after each click for the next level to be built.

    Returns:
        Dict with ``ok``; on failure ``reason`` and ``offered``, which
        lists what that level actually contained. That list is what
        turns a wrong caption into a one-line correction.
    """
    if not available():
        return {"ok": False, "reason": (
            "menu driving needs pywin32 and comtypes on Windows")}

    # COM is per THREAD. comtypes initialises the thread that imports
    # it, and callers run this in an executor because it sleeps between
    # clicks. Without this every accessible call on the worker fails and
    # a real menu reads as EMPTY rather than as broken, which is exactly
    # how it first presented.
    try:
        comtypes.CoInitialize()
        owned = True
    except Exception:                            # pragma: no cover - guard
        owned = False
    try:
        return _click_path(pid, path, settle)
    finally:
        if owned:
            try:
                comtypes.CoUninitialize()
            except Exception:                    # pragma: no cover - guard
                pass


def bring_to_front(pid: int) -> bool:
    """Activate Altium so its menu bar gets laid out.

    Altium builds its DevExpress bars on ACTIVATION, not on restore, so
    reading the bar without this raises MenuBarUnavailable no matter how
    long you wait. Split out of _click_path because listing the menus
    needs exactly the same preparation as clicking one: the discovery
    call used to skip it and so could only succeed when Altium already
    happened to be foreground, which is the one case where you do not
    need to ask what the menus are.

    Returns False when there is no main window to activate.
    """
    target = frame(pid)
    if target is None:
        return False

    user32 = _u()
    if user32.IsIconic(target.hwnd):
        # A minimized frame parks every child at -32000, where nothing
        # can be located or clicked. Left restored afterwards: putting
        # it back would race whatever dialog the command opens.
        user32.ShowWindow(target.hwnd, _SW_RESTORE)
        time.sleep(1.2)

    close_open_menu(pid)
    user32.SetForegroundWindow(target.hwnd)
    time.sleep(0.8)
    return True


def _click_path(pid: int, path: str, settle: float) -> dict:
    levels = [p.strip() for p in str(path or "").split("|") if p.strip()]
    if not levels:
        return {"ok": False, "reason": "menu_path is empty"}

    if not bring_to_front(pid):
        return {"ok": False, "reason": "no Altium main window"}

    user32 = _u()
    try:
        top = bar_items(pid)
    except MenuBarUnavailable as exc:
        return {"ok": False, "reason": str(exc)}
    match = next((node for caption, node in top.items()
                  if _same(caption, levels[0])), None)
    if match is None:
        return {"ok": False,
                "reason": (f"{levels[0]!r} is not on the menu bar. Altium's "
                           f"bar is per editor, so the matching document "
                           f"kind has to be focused first"),
                "offered": sorted(top)}
    if not _click_node(match):
        return {"ok": False,
                "reason": f"{levels[0]!r} has no clickable rectangle"}
    time.sleep(settle)

    for depth, wanted in enumerate(levels[1:], start=1):
        entries = _open_items(pid)
        if not entries:
            close_open_menu(pid)
            return {"ok": False, "reason": (
                f"a menu opened but exposed no items, so {wanted!r} could "
                f"not be looked for. The accessible layer returned "
                f"nothing, which usually means COM is not initialised on "
                f"this thread"), "offered": []}

        hit = next((e for e in entries if _same(_name(e), wanted)), None)
        if hit is None:
            offered = [_name(e) for e in entries]
            close_open_menu(pid)
            return {"ok": False,
                    "reason": f"{wanted!r} is not in {levels[depth - 1]!r}",
                    "offered": [o for o in offered if o]}
        if not _click_node(hit):
            close_open_menu(pid)
            return {"ok": False,
                    "reason": f"{wanted!r} has no clickable rectangle"}
        time.sleep(settle)

    return {"ok": True, "path": "|".join(levels)}


def open_item(pid: int, menu_key: str, item_name: str) -> dict:
    """Back-compat shim: two-level path with the top level named by key.

    Superseded by ``click_path``, which takes the whole path by name and
    needs no accelerator letter.
    """
    top = {"t": "Tools", "d": "Design", "p": "Place", "r": "Reports"}
    return click_path(pid, f"{top.get(menu_key.lower(), menu_key)}|"
                           f"{item_name}")


def list_path(pid: int, path: str = "", settle: float = 1.2) -> dict:
    """List what a menu level contains, without invoking a command.

    ``list_path(pid)`` gives the menu bar. ``list_path(pid, "Tools")``
    gives the Tools menu, ``list_path(pid, "Tools|Annotation")`` the
    Annotation submenu. This exists because the only way to discover a
    submenu used to be to click a deliberately wrong leaf and read the
    ``offered`` list off the failure, which is not something a caller
    should have to invent.

    CAVEAT, and it is a real one: opening a submenu means clicking the
    entries that lead to it, so every level NAMED here must be a
    submenu. Naming a command runs it. The menu bar and one level down
    are always safe to ask for, and from there the returned names tell
    you what is a container before you descend.
    """
    if not available():
        return {"ok": False, "reason": (
            "menu driving needs pywin32 and comtypes on Windows")}

    try:
        comtypes.CoInitialize()
        owned = True
    except Exception:                            # pragma: no cover - guard
        owned = False
    try:
        return _list_path(pid, path, settle)
    finally:
        if owned:
            try:
                comtypes.CoUninitialize()
            except Exception:                    # pragma: no cover - guard
                pass


def _list_path(pid: int, path: str, settle: float) -> dict:
    levels = [p.strip() for p in str(path or "").split("|") if p.strip()]

    if not bring_to_front(pid):
        return {"ok": False, "reason": "no Altium main window"}

    try:
        top = bar_items(pid)
    except MenuBarUnavailable as exc:
        return {"ok": False, "reason": str(exc)}

    if not levels:
        return {"ok": True, "path": "", "items": sorted(top), "note": (
            "the menu bar is per editor: focus a schematic or a board "
            "to see that editor's menus")}

    match = next((node for caption, node in top.items()
                  if _same(caption, levels[0])), None)
    if match is None:
        return {"ok": False,
                "reason": f"{levels[0]!r} is not on the menu bar",
                "offered": sorted(top)}

    try:
        # Opening a submenu leaves the PARENT popup on screen too, and
        # the accessible layer reports every open popup together. So
        # each level is clicked with the current popup handles recorded
        # first, and only the popup that appeared is read back. Reading
        # them merged returned Tools' own entries alongside
        # Annotation's, which reads as one flat menu that does not exist.
        before = {w.hwnd for w in _submenus(pid)}
        if not _click_node(match):
            return {"ok": False,
                    "reason": f"{levels[0]!r} has no clickable rectangle"}
        time.sleep(settle)
        opened = _newest_popup(pid, before)

        for depth, wanted in enumerate(levels[1:], start=1):
            entries = _items_of(opened) if opened else _open_items(pid)
            hit = next((e for e in entries if _same(_name(e), wanted)), None)
            if hit is None:
                return {"ok": False,
                        "reason": (f"{wanted!r} is not in "
                                   f"{levels[depth - 1]!r}"),
                        "offered": [n for n in (_name(e) for e in entries)
                                    if n]}
            before = {w.hwnd for w in _submenus(pid)}
            if not _click_node(hit):
                return {"ok": False,
                        "reason": f"{wanted!r} has no clickable rectangle"}
            time.sleep(settle)
            opened = _newest_popup(pid, before) or opened

        entries = _items_of(opened) if opened else _open_items(pid)
        items = [n for n in (_name(e) for e in entries) if n]
        return {"ok": True, "path": "|".join(levels), "items": items}
    finally:
        # Always put the menu away. A popup left open swallows the next
        # click, so a discovery call must not change what the editor
        # does next.
        close_open_menu(pid)


def _items_of(hwnd: int) -> list:
    """Accessible children of ONE popup window."""
    try:
        return _children(_acc(hwnd))
    except Exception:                            # pragma: no cover - guard
        return []


def _newest_popup(pid: int, before: set, timeout: float = 6.0):
    """The popup handle that appeared since ``before`` was taken.

    Returns None when nothing new opened, which is how a caller learns
    that the entry it clicked was a command rather than a submenu.
    """
    deadline = time.monotonic() + timeout
    while True:
        fresh = [w.hwnd for w in _submenus(pid) if w.hwnd not in before]
        # A popup window exists before its items do, so an empty one is
        # not yet the answer.
        for hwnd in fresh:
            if _items_of(hwnd):
                return hwnd
        if time.monotonic() >= deadline:
            return fresh[0] if fresh else None
        time.sleep(0.2)

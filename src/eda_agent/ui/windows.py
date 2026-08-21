# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Thin Win32 window and control access. No Altium knowledge lives here.

Deliberately small and free of any dialog-specific logic, so it can be
exercised against any window on the machine rather than only against
Altium. What a dialog MEANS is decided in ``dialog_report``, and what to
press about it in ``dialog_driver``.

Two rules the callers depend on:

* Nothing here clicks anything unless asked by an explicit call. There
  is no "find and press" convenience, because the whole risk of this
  package is pressing the wrong thing.
* Matching is by window CLASS and caption together. A caption alone is
  not identifying: Altium reuses captions across dialogs, and other
  applications can hold a window with the same title.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

try:                                            # pragma: no cover - platform
    import win32api
    import win32con
    import win32gui
    import win32process
    _AVAILABLE = True
except ImportError:                             # pragma: no cover - platform
    _AVAILABLE = False


class WindowsUiUnavailable(RuntimeError):
    """pywin32 is not importable, so nothing here can run."""


def available() -> bool:
    """Whether this module can do anything at all on this machine."""
    return _AVAILABLE


def _require():
    if not _AVAILABLE:
        raise WindowsUiUnavailable(
            "pywin32 is not importable, so no dialog can be inspected or "
            "driven. It is a declared dependency on win32, so this usually "
            "means a non-Windows host or a broken install.")


#: Class-name fragments that mark a pressable pushbutton.
#:
#: MEASURED against Altium's Engineering Change Order dialog, not
#: guessed. Its buttons are TXPBitBtn, whose class carries "bitbtn" and
#: NOT "button". A "button"-only test therefore finds ZERO buttons on a
#: real Altium dialog, which is what made the driver abort at its first
#: step every time. Stock Win32 "Button" is kept for MessageBox-style
#: dialogs.
_BUTTON_CLASSES = ("button", "bitbtn", "btn")

#: Fragments that match the above but must never be pressed.
#:
#: Checkboxes and radios answer to BM_CLICK exactly like a pushbutton,
#: so pressing one silently TOGGLES a setting instead of doing nothing.
#: That is not hypothetical: the ECO dialog carries a TXPCheckBox
#: ("Only Show Errors") alongside its four TXPBitBtn buttons.
#:
#: docbar / popuppanel / panelsholder are Altium's document tabs and
#: panel toggles, measured on the main window. They contain "button"
#: and are matched by the old test, so it was simultaneously too narrow
#: for dialogs and too broad for the main window.
_NOT_BUTTON_CLASSES = ("check", "radio", "group", "docbar",
                       "popuppanel", "panelsholder")

#: Win32 button styles. Only meaningful for the stock "Button" class,
#: where a checkbox and a pushbutton share one class name and are told
#: apart by these bits alone.
_BS_TYPEMASK = 0x0000000F
_BS_PRESSABLE = (0x0, 0x1, 0x8, 0xB)   # push, default-push, user, ownerdraw


@dataclass
class Control:
    """One child control: what it is, what it says, where it sits."""

    hwnd: int
    class_name: str
    text: str
    rect: tuple = field(default=(0, 0, 0, 0))
    enabled: bool = True
    visible: bool = True
    #: Raw GWL_STYLE, used to tell a stock pushbutton from a checkbox.
    style: int = 0

    def is_pressable(self) -> bool:
        """Whether this is a button a plan may legitimately press.

        Class name first, because Altium's dialogs are VCL and their
        class is the only reliable signal. For the stock Win32 "Button"
        class the name says nothing (checkbox, radio, groupbox and
        pushbutton all share it), so the style bits decide.
        """
        if not self.text:
            return False
        name = self.class_name.lower()
        if any(bad in name for bad in _NOT_BUTTON_CLASSES):
            return False
        if not any(good in name for good in _BUTTON_CLASSES):
            return False
        if name == "button":
            return (self.style & _BS_TYPEMASK) in _BS_PRESSABLE
        return True

    def describe(self) -> str:
        state = "" if self.enabled else " (disabled)"
        return f"{self.class_name}:{self.text!r}{state}"


@dataclass
class Window:
    """One top-level window, with its controls read at capture time."""

    hwnd: int
    class_name: str
    title: str
    pid: int
    controls: list = field(default_factory=list)

    def buttons(self) -> list:
        """Controls a plan may legitimately press.

        Altium's dialogs are VCL: the ECO's buttons are TXPBitBtn, and
        stock Win32 "Button" appears only in message boxes. Both are
        accepted; checkboxes, radios and the main window's tab and panel
        chrome are not. See ``Control.is_pressable``.
        """
        return [c for c in self.controls if c.is_pressable()]

    def find_button(self, caption: str):
        """The button whose caption matches, ignoring case and & markers.

        Windows accelerators put an ampersand in the caption (&OK), and
        it is invisible to the user, so callers should not have to know
        about it.
        """
        want = _normalise(caption)
        for control in self.buttons():
            if _normalise(control.text) == want:
                return control
        return None

    def message_text(self) -> list:
        """What the dialog SAYS, as opposed to what it offers.

        Button captions are the actions; everything else with text is
        the message. Separating them matters because "Cannot compare a
        source document against its owner project" is the whole content
        of an error dialog, and burying it in a list that also contains
        "OK" makes it easy to miss.

        Grids and owner-drawn lists still contribute nothing: they hold
        their content internally and expose no window text at all, which
        is why an ECO's pending-change list cannot be read this way.
        """
        pressable = {c.hwnd for c in self.buttons()}
        seen, out = set(), []
        for control in self.controls:
            if control.hwnd in pressable:
                continue
            text = (control.text or "").strip()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
        return out

    def has_unreadable_content(self) -> bool:
        """True when this dialog is displaying text that cannot be read.

        The distinction that matters is "the dialog says nothing" versus
        "the dialog is saying something I cannot see", because only the
        second one means a human has to look at the screen.

        Two causes, both MEASURED on real Altium dialogs:

        * a grid or owner-drawn list, which holds its rows internally.
          The ECO's pending changes live in a TdxTreeList.
        * text painted straight onto a panel. Delphi's TLabel is a
          TGraphicControl with NO window handle, so it cannot be
          enumerated, and the panel it is painted on answers
          WM_GETTEXTLENGTH with zero. The "No Differences Detected"
          message sits in exactly that state, and MSAA exposes nothing
          for it either.

        The second case is inferred from a container that carries no
        text of its own, which is what an all-painted panel looks like
        from outside the process.
        """
        if any(any(k in c.class_name.lower() for k in _OPAQUE_CLASSES)
               for c in self.controls):
            return True
        pressable = {c.hwnd for c in self.buttons()}
        return any(not (c.text or "").strip() and c.hwnd not in pressable
                   and any(k in c.class_name.lower()
                           for k in _CONTAINER_CLASSES)
                   for c in self.controls)

    def describe(self) -> str:
        return (f"{self.class_name} {self.title!r} "
                f"({len(self.controls)} controls)")


#: Controls that render their content without exposing window text.
#: MEASURED: the ECO's pending changes live in a TdxTreeList, which is
#: why the change list cannot be read before it is executed.
_OPAQUE_CLASSES = ("treelist", "listview", "grid", "syslistview",
                   "virtualtree")

#: Containers a VCL dialog paints its message onto. A panel with no
#: window text of its own is the signature of a TLabel drawn on it.
_CONTAINER_CLASSES = ("panel", "groupbox", "static")


def _normalise(text: str) -> str:
    return re.sub(r"[&\s.]+", "", str(text or "")).lower()


def read_text(hwnd: int) -> str:
    """A control's text, asking the control rather than trusting a cache.

    ``GetWindowText`` does NOT send WM_GETTEXT across a process boundary:
    for a window owned by another process it returns Windows' cached
    caption, which for many VCL controls is empty even though the
    control is displaying text. Altium is another process, so the cheap
    call silently loses exactly the message body that says what a dialog
    is telling you.

    WM_GETTEXT is sent with a timeout because a control belonging to a
    blocked UI thread would otherwise hang the caller, and a dialog that
    has blocked its own thread is a state this code has to survive.
    """
    _require()
    try:
        cached = win32gui.GetWindowText(hwnd)
    except Exception:                            # pragma: no cover - races
        cached = ""
    if cached:
        return cached
    try:
        length = win32gui.SendMessageTimeout(
            hwnd, win32con.WM_GETTEXTLENGTH, 0, 0,
            win32con.SMTO_ABORTIFHUNG, 500)[1]
        if not length:
            return ""
        buffer = win32gui.PyMakeBuffer((length + 1) * 2)
        win32gui.SendMessageTimeout(
            hwnd, win32con.WM_GETTEXT, length + 1, buffer,
            win32con.SMTO_ABORTIFHUNG, 800)
        raw = bytes(buffer)[:length * 2]
        return raw.decode("utf-16-le", errors="replace").rstrip("\x00")
    except Exception:                            # pragma: no cover - platform
        return ""


def is_blocked(pid: int) -> bool:
    """Whether a modal is holding this process's main window.

    Windows disables the owner while a modal is up, so a DISABLED main
    frame is the signal that the application cannot be driven and, for
    Altium, that the scripting bridge will not answer until the dialog
    goes away. Far more reliable than inferring it from a silent bridge,
    which looks identical to a crash, a busy compile, or a stopped
    polling loop.
    """
    _require()
    for window in enumerate_windows(pid=pid):
        if _is_main_frame(window):
            try:
                return not win32gui.IsWindowEnabled(window.hwnd)
            except Exception:                    # pragma: no cover - races
                return False
    return False


#: Class of Altium's main document frame. Everything else with a caption
#: is either a dialog or a dockable panel.
_MAIN_FRAME_CLASSES = ("TDocumentForm",)

#: Top-level windows that carry a caption but are not dialogs.
#:
#: TScriptForm is the SCRIPTING SYSTEM's form, which is what this
#: project's own status window is built from. Reporting it as an Altium
#: dialog is not just noise: it is always present, so "a dialog is open"
#: would be permanently true, and its perf log is long enough to bury
#: the real dialog's message underneath it. Altium's own dialogs are
#: TChangeManagementForm, TXPForm and similar, never TScriptForm.
_NOT_A_DIALOG_CLASSES = ("TScriptForm",)


def _is_main_frame(window) -> bool:
    return window.class_name in _MAIN_FRAME_CLASSES


def _is_dialog(window) -> bool:
    return (not _is_main_frame(window)
            and window.class_name not in _NOT_A_DIALOG_CLASSES
            and bool((window.title or "").strip()))


def dialogs(pid: int) -> list:
    """Every dialog-like window on a process, captured with its text.

    Excludes the main frame and anything with no caption. Dockable
    panels are kept out by requiring the window to be OWNED, which is
    what makes a dialog a dialog: panels are children of the frame,
    dialogs are owned top-level windows.
    """
    _require()
    out = []
    for window in enumerate_windows(pid=pid):
        if not _is_dialog(window):
            continue
        full = capture(window.hwnd)
        if full is not None:
            out.append(full)
    return out


def enumerate_windows(pid: Optional[int] = None,
                      visible_only: bool = True) -> list:
    """Every top-level window, optionally limited to one process.

    Controls are NOT read here: enumerating children of every window on
    the machine is slow and mostly wasted. Use ``capture`` for that.
    """
    _require()
    out = []

    def visit(hwnd, _):
        if visible_only and not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            _tid, wpid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:                        # pragma: no cover - races
            return True
        if pid is not None and wpid != pid:
            return True
        try:
            out.append(Window(hwnd=hwnd,
                              class_name=win32gui.GetClassName(hwnd),
                              title=win32gui.GetWindowText(hwnd),
                              pid=wpid))
        except Exception:                        # pragma: no cover - races
            pass
        return True

    win32gui.EnumWindows(visit, None)
    return out


def capture(hwnd: int) -> Optional[Window]:
    """One window with its child controls read, or None if it is gone.

    A window can be destroyed between being found and being inspected,
    which is ordinary rather than exceptional when a wizard is
    advancing, so that case returns None instead of raising.
    """
    _require()
    if not win32gui.IsWindow(hwnd):
        return None
    try:
        window = Window(hwnd=hwnd,
                        class_name=win32gui.GetClassName(hwnd),
                        title=win32gui.GetWindowText(hwnd),
                        pid=win32process.GetWindowThreadProcessId(hwnd)[1])
    except Exception:
        return None

    def visit(child, _):
        try:
            try:
                style = win32api.GetWindowLong(child, win32con.GWL_STYLE)
            except Exception:                    # pragma: no cover - races
                style = 0
            window.controls.append(Control(
                hwnd=child,
                class_name=win32gui.GetClassName(child),
                # read_text, not GetWindowText: Altium is another
                # process, and the cheap call returns a cache that is
                # empty for many VCL controls, which is precisely how a
                # dialog's message body goes missing.
                text=read_text(child),
                rect=win32gui.GetWindowRect(child),
                enabled=win32gui.IsWindowEnabled(child),
                visible=win32gui.IsWindowVisible(child),
                style=style))
        except Exception:                        # pragma: no cover - races
            pass
        return True

    try:
        win32gui.EnumChildWindows(hwnd, visit, None)
    except Exception:
        # A window with no children raises rather than returning empty.
        pass
    return window


def wait_for_window(match: Callable, timeout: float,
                    pid: Optional[int] = None,
                    poll: float = 0.25) -> Optional[Window]:
    """Wait for a window satisfying ``match``, captured with controls.

    ``match`` receives a Window WITHOUT controls (the cheap form) and
    returns a bool. The winner is then re-captured with its controls,
    because reading children of every candidate on every poll is the
    slow part.
    """
    _require()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for window in enumerate_windows(pid=pid):
            try:
                hit = match(window)
            except Exception:                    # pragma: no cover - guard
                hit = False
            if hit:
                full = capture(window.hwnd)
                if full is not None:
                    return full
        time.sleep(poll)
    return None


def wait_for_close(hwnd: int, timeout: float, poll: float = 0.25) -> bool:
    """Wait for a window to disappear. True if it did."""
    _require()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not win32gui.IsWindow(hwnd):
            return True
        time.sleep(poll)
    return not win32gui.IsWindow(hwnd)


def click(control: Control) -> None:
    """Press one control, by messaging it rather than moving the mouse.

    Every message here is addressed to the control's OWN handle, so it
    reaches the intended control regardless of what is physically on
    top, what has focus, or where the pointer happens to be, and the
    physical cursor is never touched. A stray mouse move during a real
    click sequence is the classic way UI automation presses the wrong
    thing.

    Two mechanisms, because one does not cover both worlds. BM_CLICK is
    the correct press for the stock Win32 "Button" class used by message
    boxes. Altium's dialogs are VCL, and MEASURED on the Engineering
    Change Order dialog a TXPBitBtn IGNORES BM_CLICK completely: the
    press returned cleanly and the dialog stayed open. It responds to a
    posted button-down/up pair on its own handle, which is what the
    non-stock branch sends.
    """
    _require()
    if not control.enabled:
        raise RuntimeError(
            f"refusing to click {control.describe()}: it is disabled, which "
            f"usually means the dialog is not in the state expected")
    if control.class_name.lower() == "button":
        win32gui.SendMessage(control.hwnd, win32con.BM_CLICK, 0, 0)
        return
    # lParam is the pointer position in CLIENT coordinates, and it used
    # to be 0, which is the top-left CORNER of the control. A VCL
    # button tracks where the press landed and can treat the corner
    # pixel as a miss or as being on the border. The same mistake in
    # select_row put a click at a grid's origin and selected its first
    # row. Aim at the middle instead.
    lparam = 0
    try:
        left, top, right, bottom = win32gui.GetClientRect(control.hwnd)
        x, y = (right - left) // 2, (bottom - top) // 2
        lparam = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
    except Exception:                            # pragma: no cover - guard
        pass
    win32gui.PostMessage(control.hwnd, win32con.WM_LBUTTONDOWN,
                         win32con.MK_LBUTTON, lparam)
    win32gui.PostMessage(control.hwnd, win32con.WM_LBUTTONUP, 0, lparam)


def close_window(hwnd: int) -> None:
    """Ask a window to close, as clicking its X would."""
    _require()
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)


def window_text_dump(window: Window, limit: int = 60) -> list:
    """Readable text from a window's controls, for the record.

    Grids and owner-drawn lists return nothing here, which is exactly
    why an ECO's change list cannot be enumerated before it runs. What
    comes back is labels, static text and button captions, and that is
    stated rather than presented as the dialog's full contents.
    """
    seen = []
    for control in window.controls:
        text = (control.text or "").strip()
        if text and text not in seen:
            seen.append(text)
        if len(seen) >= limit:
            break
    return seen

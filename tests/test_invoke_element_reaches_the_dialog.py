# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A UI Automation press reaches the dialog's own button.

Two gaps left Tools > Silkscreen Preparation unpressable: its OK has no
window handle, so app_press_dialog_button fell back to Enter, which that
dialog does not take; and app_invoke_element with no dialog_title
searched the main window, where a WPF dialog's elements are not. A
label and a button sharing the caption also picked the label, which
offers no Invoke. Nothing here touches Altium.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from eda_agent.ui import uia


class _FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture
def tools(monkeypatch):
    from eda_agent.tools import uiauto
    monkeypatch.setattr(uiauto, "_altium_pid", lambda: (4242, None))
    mcp = _FakeMcp()
    uiauto.register_uiauto_tools(mcp)
    return mcp.tools, uiauto


def test_no_dialog_title_means_the_frontmost_dialog(tools, monkeypatch):
    registered, uiauto = tools
    seen = {}
    monkeypatch.setattr(uia, "available", lambda: True)
    monkeypatch.setattr(uiauto.windows, "dialogs",
                        lambda pid: [SimpleNamespace(hwnd=77, title="Silkscreen Preparation")])
    monkeypatch.setattr(uiauto.windows, "wait_for_close", lambda hwnd, timeout=5.0: True)

    def invoke(hwnd, name):
        seen["hwnd"] = hwnd
        return {"ok": True, "element": name, "how": "invoke"}

    monkeypatch.setattr(uia, "invoke", invoke)
    out = asyncio.run(registered["app_invoke_element"](name="OK"))
    assert seen["hwnd"] == 77, "the press went to the dialog, not the main window"
    assert out["ok"] and out["dialog_closed"] is True


def test_a_handleless_button_is_pressed_by_name_before_any_key(tools, monkeypatch):
    registered, uiauto = tools
    dialog = SimpleNamespace(hwnd=88, title="Silkscreen Preparation",
                             find_button=lambda caption: None, buttons=lambda: [])
    monkeypatch.setattr(uiauto.windows, "dialogs", lambda pid: [dialog])
    monkeypatch.setattr(uiauto.windows, "wait_for_close", lambda hwnd, timeout=5.0: True)
    keys = []
    monkeypatch.setattr(uiauto.windows, "press_key", lambda hwnd, key: keys.append(key))
    monkeypatch.setattr(uia, "available", lambda: True)
    monkeypatch.setattr(uia, "invoke", lambda hwnd, name: {"ok": True, "element": name})
    out = asyncio.run(registered["app_press_dialog_button"](
        button_caption="OK", dialog_title="Silkscreen", allow_irreversible=True))
    assert out["ok"] and out["method"] == "uia" and keys == []


def test_a_button_beats_a_label_of_the_same_caption(monkeypatch):
    label = SimpleNamespace(kind="text")
    button = SimpleNamespace(kind="button")
    root = SimpleNamespace(kind="root")
    info = {
        id(label): {"name": "OK", "automation_id": "", "type": "Text"},
        id(button): {"name": "OK", "automation_id": "okButton", "type": "Button"},
    }
    monkeypatch.setattr(uia, "_element", lambda hwnd: root)
    monkeypatch.setattr(uia, "_children", lambda node: [label, button] if node is root else [])
    monkeypatch.setattr(uia, "_describe", lambda node: info[id(node)])
    monkeypatch.setattr(uia, "_pattern", lambda element, pid, iface: None)
    element, found = uia._find_pressable(1, "OK")
    assert element is button and found["type"] == "Button"

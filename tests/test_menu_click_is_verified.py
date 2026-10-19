# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A menu click reports ok only for the command it actually ran.

``app_click_menu("Tools|Update From PCB Libraries")`` reported ok while
Signal Integrity's setup dialog opened. The release of a real click is
queued input, and the pointer was put back at once, so it could land on
whatever lay under the user's pointer; then nothing checked what opened.

Now: the item under the pointer must be the one asked for, the pointer
stays until the click has taken, the last level must close the menu
(a command ran), two entries of one name refuse, and with
``expect_dialog`` the reply fails when another dialog opens. These tests
drive the walk against a simulated menu: nothing here touches Altium.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from eda_agent.ui import menu


class _Menu:
    """A two-level menu: Tools, holding the entries given."""

    def __init__(self, entries, closes_on_final=True, under=None):
        self.entries = entries
        self.closes_on_final = closes_on_final
        self.under = under
        self.open = []
        self.pressed = []

    def install(self, monkeypatch):
        top = SimpleNamespace(name="Tools", rect=(0, 0, 40, 20))
        monkeypatch.setattr(menu, "bring_to_front", lambda pid: True)
        monkeypatch.setattr(menu, "bar_items", lambda pid: {"Tools": top})
        monkeypatch.setattr(menu, "close_open_menu", lambda pid: self.open.clear())
        monkeypatch.setattr(menu, "_submenus", lambda pid: list(self.open))
        monkeypatch.setattr(menu, "_newest_popup",
                            lambda pid, before, timeout=6.0: 1 if self.open else None)
        monkeypatch.setattr(menu, "_items_of", lambda hwnd: list(self.entries))
        monkeypatch.setattr(menu, "_name", lambda node: node.name)
        monkeypatch.setattr(menu, "_visible_rect", lambda node: node.rect)
        monkeypatch.setattr(menu.win, "wait_until", lambda check, timeout, poll=0.02: check())

        def click(pid, x, y, after=0.3, expect="", until=None, wait=1.0):
            under = self.under or expect
            if expect and under != expect:
                return f"the pointer is over {under!r}, not {expect!r}"
            self.pressed.append(expect)
            if expect == "Tools":
                self.open[:] = [SimpleNamespace(hwnd=1)]
            elif self.closes_on_final:
                self.open.clear()
            return ""

        monkeypatch.setattr(menu, "_click", click)


def _entry(name, y):
    return SimpleNamespace(name=name, rect=(0, y, 200, 20))


ENTRIES = [_entry("Signal Integrity...", 20), _entry("Update From PCB Libraries...", 40)]


def test_the_command_asked_for_is_the_one_pressed(monkeypatch):
    m = _Menu(ENTRIES)
    m.install(monkeypatch)
    out = menu._click_path(1, "Tools|Update From PCB Libraries", 0.1)
    assert out["ok"] is True
    assert m.pressed == ["Tools", "Update From PCB Libraries"]


def test_a_menu_still_open_after_the_last_click_ran_nothing(monkeypatch):
    m = _Menu(ENTRIES, closes_on_final=False)
    m.install(monkeypatch)
    out = menu._click_path(1, "Tools|Update From PCB Libraries", 0.1)
    assert out["ok"] is False and "no command ran" in out["reason"]


def test_a_pointer_over_another_entry_refuses_the_press(monkeypatch):
    m = _Menu(ENTRIES, under="Signal Integrity...")
    m.install(monkeypatch)
    out = menu._click_path(1, "Tools|Update From PCB Libraries", 0.1)
    assert out["ok"] is False and "Signal Integrity" in out["reason"]


def test_two_entries_of_one_name_refuse(monkeypatch):
    m = _Menu([_entry("Update From PCB Libraries...", 20), _entry("Update From PCB Libraries", 60)])
    m.install(monkeypatch)
    out = menu._click_path(1, "Tools|Update From PCB Libraries", 0.1)
    assert out["ok"] is False and "a guess" in out["reason"]


def _dialogs(monkeypatch, sequence):
    calls = iter(sequence)
    last = []

    def dialogs(pid):
        nonlocal last
        last = next(calls, last)
        return [SimpleNamespace(hwnd=h, title=t) for h, t in last]

    monkeypatch.setattr(menu.win, "dialogs", dialogs)


def test_another_dialog_than_the_one_expected_fails(monkeypatch):
    m = _Menu(ENTRIES)
    m.install(monkeypatch)
    _dialogs(monkeypatch, [[], [(5, "SI Setup Options")]])
    out = menu.click_path(1, "Tools|Update From PCB Libraries",
                          expect_dialog="Update From PCB Librar", expect_timeout=0.1)
    assert out["ok"] is False and out["opened_instead"] == ["SI Setup Options"]


def test_the_expected_dialog_passes_and_is_named(monkeypatch):
    m = _Menu(ENTRIES)
    m.install(monkeypatch)
    _dialogs(monkeypatch, [[], [(5, "Update From PCB Libraries - Options")]])
    out = menu.click_path(1, "Tools|Update From PCB Libraries",
                          expect_dialog="Update From PCB Librar", expect_timeout=0.1)
    assert out["ok"] is True and out["opened"] == ["Update From PCB Libraries - Options"]


@pytest.mark.parametrize("target", ["schematic", "pcb"])
def test_update_from_libraries_names_the_wizard_it_expects(target):
    from eda_agent.tools import uiauto
    assert target in uiauto._MENU_EXPECT
    wanted = menu._flat_title(uiauto._MENU_EXPECT[target])
    assert wanted in menu._flat_title(uiauto._MENU_PATHS[target])
    assert wanted not in menu._flat_title("SI Setup Options")

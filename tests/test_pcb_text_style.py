# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Silkscreen text size can be set on existing designators and comments.

Only the create path could set a text's height, so a fabrication spec
like 0.8 mm high with a 0.1 mm stroke could not be met on a placed board.
pcb_set_text_style sets it, and the script reads every text back.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from eda_agent.tools import pcb as pcb_module

PCB_PAS = Path(__file__).resolve().parents[1] / "scripts" / "altium" / "PCB.pas"


class _Bridge:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def send_command_async(self, command, params=None, timeout=None):
        self.calls.append((command, params or {}))
        return {"success": True, "matched": 1, "changed": 1, "failed": 0}


def _tool(monkeypatch, bridge, name):
    monkeypatch.setattr(pcb_module, "get_bridge", lambda: bridge)
    captured = {}

    class DummyMcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    pcb_module.register_pcb_tools(DummyMcp())
    return captured[name]


@pytest.mark.asyncio
async def test_height_and_stroke_are_sent_as_decimal_mils(monkeypatch):
    bridge = _Bridge()
    tool = _tool(monkeypatch, bridge, "pcb_set_text_style")

    await tool(height_mils=31.5, stroke_mils=3.94, designators=["U1", "R5"])

    command, params = bridge.calls[0]
    assert command == "pcb.set_text_style"
    assert params == {"which": "designator", "height_mils": "31.5",
                      "stroke_mils": "3.94", "designators": "U1|R5"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {},
    {"height_mils": 0},
    {"stroke_mils": -1},
    {"height_mils": 30, "which": "label"},
    {"height_mils": 30, "designators": ["U1|U2"]},
])
async def test_a_call_that_cannot_mean_anything_is_refused_before_sending(monkeypatch, kwargs):
    bridge = _Bridge()
    tool = _tool(monkeypatch, bridge, "pcb_set_text_style")

    out = await tool(**kwargs)

    assert out["ok"] is False
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_place_text_takes_a_decimal_height_and_a_stroke(monkeypatch):
    bridge = _Bridge()
    tool = _tool(monkeypatch, bridge, "pcb_place_text")

    await tool(text="REV A", x=100, y=200, height=31.5, stroke=3.94)

    params = bridge.calls[0][1]
    assert params["height"] == "31.5"
    assert params["stroke"] == "3.94"


def _handler(text: str, header: str) -> str:
    start = text.index(header)
    end = re.compile(r"^End;", re.M).search(text, start).end()
    return text[start:end]


def test_the_script_reads_every_text_back_and_is_dispatched():
    text = PCB_PAS.read_text(encoding="utf-8", errors="replace")
    body = _handler(text, "Function PCB_SetTextStyle(")
    assert "'set_text_style':" in text and "PCB_SetTextStyle(Params, RequestId)" in text
    # The write sits inside the component's and the text's modify brackets.
    write = body.index("Txt.Size := MilsToCoordF(H)")
    assert body.rindex("Comp.BeginModify", 0, write) < body.rindex("Txt.BeginModify", 0, write)
    assert body.index("Txt.EndModify", write) < body.index("Comp.EndModify", write)
    # A text whose value did not take is counted as failed, not changed.
    assert re.search(r"If SetH And \(Abs\(GotH - H\) > 0\.01\) Then Ok := False", body)
    assert re.search(r"If SetS And \(Abs\(GotS - S\) > 0\.01\) Then Ok := False", body)
    # Collected first: nothing is modified while the iterator is alive.
    assert body.index("BoardIterator_Destroy(") < write

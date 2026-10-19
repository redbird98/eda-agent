# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A drawn schematic gets Altium's own junctions, not manual ones.

The engine marks a junction wherever a same-net wire ends on another or
three ends meet, and Altium draws an AUTO junction at exactly those points
by itself: blue, as interactive wiring leaves them. Sending a junction
object places a MANUAL junction, drawn dark red with a lock marker on top
of the blue one. Checked live on AD 26.10.1.6 with wires placed through
this bridge, in both placement orders.

The canvas still computes the points, for the offline previews and the
counts. It just must not send them.
"""

from __future__ import annotations

from pathlib import Path

from eda_agent.design.canvas import (
    Junction, SchematicCanvas, Sheet, SymbolInstance, WireSegment)
from eda_agent.design.emitter import emit_canvas, emit_canvas_delta
from eda_agent.design.symbols import SymbolBBox, SymbolModel, SymbolPin

_JUNCTION_COMMANDS = {"generic.place_junction", "generic.place_junctions"}


class _Bridge:
    def __init__(self):
        self.sent: list[tuple] = []

    def send_command(self, command, params=None, **kw):
        self.sent.append((command, params or {}))
        return {"success": True, "placed": 99}

    def commands(self):
        return [c for c, _ in self.sent]


def _t_junction_canvas() -> SchematicCanvas:
    symbol = SymbolModel(
        lib_path="LIB.SchLib", lib_ref="R",
        pins=[SymbolPin(designator="1", name="A", x=0, y=100, orientation=1,
                        length=100, electrical_type="passive")],
        body_bbox=SymbolBBox(x_min=-50, y_min=-50, x_max=50, y_max=50))
    cv = SchematicCanvas()
    cv.add_sheet(Sheet(name="main"))
    cv.add_instance(SymbolInstance(refdes="R1", symbol=symbol, x=1000,
                                   y=1000, rotation=0, sheet="main"))
    cv.add_wires([
        WireSegment(x1=0, y1=0, x2=200, y2=0, sheet="main", net="N"),
        WireSegment(x1=100, y1=0, x2=100, y2=200, sheet="main", net="N"),
    ])
    cv.add_junctions([Junction(x=100, y=0, sheet="main")])
    return cv


def test_the_canvas_still_knows_where_the_junctions_are():
    """The previews and counts read these, so they must still exist."""
    assert _t_junction_canvas().junctions_on("main") == [Junction(100, 0, "main")]


def test_a_full_emit_sends_no_junction(tmp_path: Path):
    bridge = _Bridge()
    emit_canvas(_t_junction_canvas(), str(tmp_path / "p.PrjPcb"), bridge)
    assert "generic.place_wires" in bridge.commands(), (
        "the emit placed no wires, so this test checked nothing")
    assert not _JUNCTION_COMMANDS & set(bridge.commands()), (
        "the emitter placed junction objects. Altium draws those points as "
        "blue auto junctions already, and an explicit one is a dark red "
        "manual junction on top")


def test_a_delta_emit_sends_no_junction():
    bridge = _Bridge()
    emit_canvas_delta(_t_junction_canvas(), {"instances": [
        {"refdes": "R1", "lib_path": "LIB.SchLib", "lib_ref": "R",
         "x": 1000, "y": 1000, "rotation": 0}]}, "p.PrjPcb", bridge)
    assert "generic.place_wires" in bridge.commands()
    assert not _JUNCTION_COMMANDS & set(bridge.commands())


def test_a_delta_emit_still_clears_old_manual_junctions():
    """A sheet drawn by an older version carries manual ones. Re-running
    the plan must take them away, not leave them over the new wires."""
    bridge = _Bridge()
    emit_canvas_delta(_t_junction_canvas(), {"instances": [
        {"refdes": "R1", "lib_path": "LIB.SchLib", "lib_ref": "R",
         "x": 1000, "y": 1000, "rotation": 0}]}, "p.PrjPcb", bridge)
    cleared = {p.get("object_type") for c, p in bridge.sent
               if c == "generic.delete_objects"}
    assert "eJunction" in cleared


def test_the_legacy_executor_sends_no_junction():
    source = (Path(__file__).resolve().parents[1] / "src" / "eda_agent"
              / "design" / "executor.py").read_text(encoding="utf-8")
    for command in _JUNCTION_COMMANDS:
        assert f'"{command}"' not in source, (
            f"executor.py sends {command}; Altium already draws those "
            f"junctions itself")

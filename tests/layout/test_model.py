# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The board model survives a JSON round trip exactly.

A fixture read once from Altium is replayed offline for every benchmark
run. If saving and loading changed anything, the benchmark would be about
a board that does not exist.
"""

from __future__ import annotations

import pytest

from eda_agent.layout.model import (
    Arc, Component, DiffPair, Layer, LayoutBoard, Pad, PadCopper, Region,
    Room, Rule, Track, Via,
)


def _board() -> LayoutBoard:
    return LayoutBoard(
        name="demo",
        source="unit test",
        outline=[(0, 0), (2000, 0), (2000, 1500), (0, 1500)],
        cutouts=[[(900, 700), (1100, 700), (1100, 800), (900, 800)]],
        layers=[Layer("TopLayer", "signal", 0),
                Layer("GND", "plane", 1, plane_net="GND"),
                Layer("BottomLayer", "signal", 2)],
        components=[Component("U1", "QFN32", 500, 500, 90, "top", False, 35.0,
                              courtyard=[(400, 400), (600, 400), (600, 600), (400, 600)],
                              courtyard_source="courtyard"),
                    Component("C1", "0402", 700, 500, 0, "bottom", True, 20.0)],
        pads=[Pad("U1", "1", 450, 520, 90, "VCC",
                  [PadCopper("TopLayer", "roundrect", 10.0, 34.0, 25.0)]),
              Pad("", "MH1", 100, 100, 0, "",
                  [PadCopper(l, "round", 120, 120) for l in
                   ("TopLayer", "GND", "BottomLayer")], hole=110.0)],
        tracks=[Track("TopLayer", 450, 520, 700, 520, 8.0, "VCC")],
        arcs=[Arc("TopLayer", 800, 800, 100, 0, 90, 10.0, "SIG")],
        vias=[Via(700, 520, 18, 8, "TopLayer", "BottomLayer", "VCC")],
        regions=[Region("GND", [(0, 0), (100, 0), (100, 100)], net="GND",
                        kind="pour", source="polygon:GND_POUR")],
        net_classes={"Power": ["VCC", "GND"]},
        diff_pairs=[DiffPair("USB", "USB_P", "USB_N")],
        rules=[Rule("Clearance", "clearance", "All", "All", 1, True,
                    {"gap": 6.0}, "Clearance Constraint (Gap=6mil) (All),(All)")],
        rooms=[Room("Power", [(0, 0), (500, 0), (500, 500), (0, 500)], "InComponentClass('PWR')")],
        meta={"altium_version": "26.10"},
    )


@pytest.mark.parametrize("suffix", [".json", ".json.gz"])
def test_round_trip_is_exact(tmp_path, suffix):
    board = _board()
    path = tmp_path / f"board{suffix}"
    board.save(path)
    again = LayoutBoard.load(path)
    assert again.to_dict() == board.to_dict()
    assert again.pads[0].copper[0].corner_pct == 25.0
    assert isinstance(again.outline[0], tuple)
    assert isinstance(again.cutouts[0][0], tuple)


def test_queries():
    board = _board()
    assert board.copper_layers() == ["TopLayer", "GND", "BottomLayer"]
    assert board.signal_layers() == ["TopLayer", "BottomLayer"]
    assert board.layers_between("TopLayer", "BottomLayer") == [
        "TopLayer", "GND", "BottomLayer"]
    assert set(board.pads_by_net()) == {"VCC"}
    assert board.component("C1").side == "bottom"
    assert "VCC" in board.nets()


def test_pad_shape_is_on_its_own_layers_only():
    board = _board()
    smd = board.pads[0]
    assert smd.is_smd
    assert smd.shape_on("TopLayer") is not None
    assert smd.shape_on("BottomLayer") is None
    through = board.pads[1]
    assert not through.is_smd
    assert through.layers() == ["TopLayer", "GND", "BottomLayer"]


def test_unknown_keys_are_ignored_on_load():
    data = _board().to_dict()
    data["future_field"] = 1
    data["pads"][0]["future_field"] = 2
    assert LayoutBoard.from_dict(data).pads[0].name == "1"

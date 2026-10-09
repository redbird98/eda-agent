# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The benchmark harness: what it strips, what it keeps, how it scores."""

from __future__ import annotations

from eda_agent.layout.bench import (copy_board, route_benchmark, routing_stats,
                                    strip_routing)
from eda_agent.layout.model import (Arc, Layer, LayoutBoard, Pad, PadCopper,
                                    Region, Rule, Track, Via)


def _board() -> LayoutBoard:
    sq = [(0, 0), (400, 0), (400, 100), (0, 100)]
    return LayoutBoard(
        name="bench",
        layers=[Layer("TopLayer", "signal", 0), Layer("InternalPlane1", "plane", 1),
                Layer("BottomLayer", "signal", 2)],
        pads=[Pad("R1", "1", 0, 0, net="A", copper=[PadCopper("TopLayer", "rect", 10, 10)]),
              Pad("R2", "1", 300, 0, net="A", copper=[PadCopper("TopLayer", "rect", 10, 10)])],
        tracks=[Track("TopLayer", 0, 0, 300, 0, 8, "A"),
                Track("TopLayer", -5, -5, 5, -5, 2, "", comp="R1"),
                Track("TopLayer", 0, 50, 100, 50, 5, "", keepout=True)],
        arcs=[Arc("TopLayer", 0, 0, 100, 0, 90, 8, "A")],
        vias=[Via(150, 0, 20, 10, "TopLayer", "BottomLayer", "A"),
              Via(0, 0, 12, 6, "TopLayer", "BottomLayer", "A", comp="R1")],
        regions=[Region("TopLayer", sq, [], "GND", "pour"),
                 Region("TopLayer", sq, [], "GND", "pour_boundary"),
                 Region("TopLayer", sq, [], "VCC", "copper"),
                 Region("TopLayer", [(-30, -30), (-20, -30), (-20, -20)], [], "", "copper",
                        comp="R1"),
                 Region("InternalPlane1", sq, [], "GND", "plane"),
                 Region("TopLayer", sq, [], "", "keepout", keepout=True)],
        rules=[Rule("Clearance", "0", "All", "All", 1, True, {"gap": 6.0},
                    "Clearance Constraint (Gap=6mil) (All),(All)")],
    )


def test_strip_removes_what_a_router_lays_and_keeps_the_design():
    s = strip_routing(_board())
    assert [(t.comp, t.keepout) for t in s.tracks] == [("R1", False), ("", True)]
    assert s.arcs == []
    assert [v.comp for v in s.vias] == ["R1"], "a footprint via is part of the footprint"
    assert sorted((r.kind, r.comp) for r in s.regions) == [
        ("copper", "R1"), ("keepout", ""), ("plane", ""), ("pour_boundary", "")]


def test_strip_leaves_the_original_alone():
    b = _board()
    before = b.to_dict()
    strip_routing(b)
    assert b.to_dict() == before


def test_routing_stats_count_only_free_copper():
    st = routing_stats(_board())
    assert st["vias"] == 1
    # 300 of track and a quarter circle of radius 100.
    assert abs(st["length"] - (300 + 157.1)) < 0.1


def test_a_benchmark_scores_the_person_the_start_and_the_engine():
    def reroute(board):
        board.tracks.append(Track("TopLayer", 0, 0, 300, 0, 8, "A"))
        return board

    r = route_benchmark(_board(), reroute).as_dict()
    assert r["human"]["completion"] == 1.0
    assert r["start"]["completion"] == 0.0
    assert r["engine"]["completion"] == 1.0
    assert r["engine"]["vias"] == 0 and r["vias_vs_human"] == 0.0


def test_copy_is_deep():
    b = _board()
    c = copy_board(b)
    c.tracks[0].x1 = 999
    assert b.tracks[0].x1 == 0

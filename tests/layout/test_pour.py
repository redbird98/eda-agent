# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Pours: a net with a pour outline is poured round the routes, not routed.

Judged by the exact DRC on the copper the router lays, pours included.
"""

from __future__ import annotations

from eda_agent.layout.drc import run_drc
from eda_agent.layout.model import Layer, LayoutBoard, Pad, PadCopper, Region, Rule
from eda_agent.layout.route import Router


def _board(w=600, h=400, gap=8.0, width=10.0) -> LayoutBoard:
    rules = [
        Rule("Clearance", "0", "All", "All", 1, True, {"gap": gap},
             f"Clearance Constraint (Gap={gap}mil) (All),(All)"),
        Rule("Width", "1", "All", "All", 1, True, {},
             f"Width Constraint (Min={width}mil) (Max={width}mil) (Preferred={width}mil) (All)"),
        Rule("RoutingVias", "2", "All", "All", 1, True, {},
             "Routing Via (MinHoleWidth=10mil) (MaxHoleWidth=10mil) (PreferredHoleWidth=10mil) "
             "(MinWidth=20mil) (MaxWidth=20mil) (PreferedWidth=20mil) (All)"),
    ]
    return LayoutBoard(name="t", outline=[(0, 0), (w, 0), (w, h), (0, h)],
                       layers=[Layer(n, "signal", i) for i, n in enumerate(("TopLayer", "BottomLayer"))],
                       rules=rules)


def _pad(comp, name, x, y, net, size=20.0):
    return Pad(comp, name, x, y, net=net, copper=[PadCopper("TopLayer", "rect", size, size)])


def _outline(net, layer="TopLayer", w=600, h=400):
    return Region(layer, [(0, 0), (w, 0), (w, h), (0, h)], [], net, "pour_boundary",
                  source=f"polygon:{net}")


def _route(board):
    r = Router(board)
    rep = r.run()
    out = r.apply()
    return rep, out, run_drc(out)


def test_a_poured_net_is_joined_by_its_pour_not_by_tracks():
    b = _board(w=600, h=400, gap=8.0, width=10.0)
    b.pads = [_pad("R1", "2", 100, 100, "GND"), _pad("R2", "2", 500, 300, "GND"),
              _pad("R3", "1", 100, 300, "S"), _pad("R4", "1", 500, 100, "S")]
    b.regions = [_outline("GND")]
    rep, out, drc = _route(b)
    assert drc.completion == 1.0 and not drc.violations
    assert not [t for t in out.tracks if t.net == "GND"]
    assert [x for x in out.regions if x.kind == "pour" and x.net == "GND"]


def test_a_pour_cut_in_two_by_a_route_is_stitched_through_the_other_layer():
    # S runs along the top from edge to edge, cutting the top pour in two
    # with a GND pad on each side; there is no pour on the bottom.
    b = _board(w=600, h=400, gap=8.0, width=10.0)
    b.pads = [_pad("R1", "2", 300, 100, "GND"), _pad("R2", "2", 300, 300, "GND"),
              _pad("J1", "1", 12, 200, "S", size=20.0), _pad("J2", "1", 588, 200, "S", size=20.0)]
    b.regions = [_outline("GND")]
    rep, out, drc = _route(b)
    assert all(t.layer == "TopLayer" for t in out.tracks if t.net == "S")
    assert drc.completion == 1.0 and not drc.violations
    assert sum(1 for v in out.vias if v.net == "GND") >= 2


def test_routes_that_meet_join_the_pieces_they_each_touch():
    # Two pieces of a poured net; one route leaves the first and stops on
    # the bottom layer, another leaves the second and ends on the first
    # route there, as a route of a tree does. Joined only through that
    # meeting; counted by the pieces each route touches, they were not,
    # and a pour's connection counted unmade.
    import numpy as np
    from types import SimpleNamespace
    from eda_agent.layout.route.pour import _components
    gs = [{"cells": np.array([[0, 0, 0]])}, {"cells": np.array([[0, 0, 9]])}]
    owner = {(0, 0, 0): 0, (0, 0, 9): 1}
    a = [(0, 0, 0), (-1, 0, 1), (1, 0, 1), (1, 0, 2), (1, 0, 3)]
    b = [(0, 0, 9), (-1, 0, 8), (1, 0, 8), (1, 0, 5), (1, 0, 3)]
    router = SimpleNamespace(L=2)
    assert _components(router, gs, owner, [a, b]) == 1
    assert _components(router, gs, owner, [a]) == 2
    # And through a via one route ends on and the other passes.
    c = [(0, 0, 9), (0, 0, 5), (0, 0, 1)]
    assert _components(router, gs, owner, [a, c]) == 1

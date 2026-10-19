# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The routing grid's distance fields and the room they leave."""

from __future__ import annotations

import numpy as np
import pytest

from eda_agent.layout import geom
from eda_agent.layout.model import Layer, LayoutBoard, Pad, PadCopper, Track
from eda_agent.layout.route.grid import (EDGE, KEEPOUT, NETLESS, RouteGrid,
                                         fill_polygon, inner_depth)


def _grid(board, pitch=1.0, reach=40.0):
    return RouteGrid(board, pitch, reach).build()


def _board(**kw):
    b = LayoutBoard(name="g", outline=[(0, 0), (200, 0), (200, 100), (0, 100)],
                    layers=[Layer("TopLayer", "signal", 0)])
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def _at(g, x, y):
    i, j = g.spec.cell(x, y)
    return (slice(j, j + 1), slice(i, i + 1))


def _clear(table):
    return lambda who: np.vectorize(lambda o: table.get(int(o), 0.0))(who).astype(float)


def test_distances_are_to_the_exact_shape_edge():
    b = _board(pads=[Pad("R1", "1", 100, 50, net="A",
                         copper=[PadCopper("TopLayer", "rect", 20, 10)])])
    g = _grid(b)
    ws = _at(g, 100, 70)              # 15 above the pad's top edge
    assert g.d1[0][ws][0, 0] == pytest.approx(15.0)
    assert g.id1[0][ws][0, 0] == g.net_id["A"]


def test_a_nets_own_copper_is_not_in_its_way_but_the_next_owner_is():
    b = _board(pads=[Pad("R1", "1", 100, 50, net="A", copper=[PadCopper("TopLayer", "rect", 20, 10)]),
                     Pad("R2", "1", 140, 50, net="B", copper=[PadCopper("TopLayer", "rect", 20, 10)])])
    g = _grid(b)
    a, bb = g.net_id["A"], g.net_id["B"]
    ws = _at(g, 105, 50)              # inside A's pad, 25 from B's
    zero = _clear({})
    assert g.slack(a, 0, ws, zero, 0.0)[0, 0] == pytest.approx(25.0)
    assert g.slack(bb, 0, ws, zero, 0.0)[0, 0] == pytest.approx(0.0)


def test_a_keepout_nearer_than_a_pad_does_not_hide_the_pad():
    # The keep-out needs no clearance and lies a little nearer than a pad
    # that needs 5 mil. Mixed in one field, the keep-out took the nearest
    # place and the pad was never checked: a via went in 1.6 mil from it.
    b = _board(pads=[Pad("J1", "1", 145, 50, net="",
                         copper=[PadCopper("TopLayer", "rect", 10, 10)])],
               tracks=[Track("TopLayer", 139.9, 0, 139.9, 100, 0.0, keepout=True)])
    b.pads.append(Pad("R1", "1", 20, 50, net="A", copper=[PadCopper("TopLayer", "rect", 10, 10)]))
    g = _grid(b)
    a = g.net_id["A"]
    ws = _at(g, 128, 50)              # 11.9 from the keep-out, 12 from the pad
    slack = g.slack(a, 0, ws, _clear({NETLESS: 5.0}), 0.0)[0, 0]
    assert slack == pytest.approx(12.0 - 5.0, abs=1e-3)


def test_the_board_edge_is_its_own_field_with_its_own_clearance():
    g = _grid(_board())
    ws = _at(g, 100, 10)
    assert g.slack(1, 0, ws, _clear({}), 15.0)[0, 0] == pytest.approx(10.0 - 15.0)
    assert g.de[_at(g, 100, 10)][0, 0] == pytest.approx(10.0)


def test_fill_polygon_marks_inside_cells_and_respects_holes():
    g = RouteGrid(_board(), 1.0, 10.0)
    outer = [(10, 10), (90, 10), (90, 90), (10, 90)]
    hole = [(40, 40), (60, 40), (60, 60), (40, 60)]
    m = fill_polygon(g.spec, [outer, hole])
    assert m[20, 20] and not m[50, 50] and not m[5, 5]


def test_inner_depth_is_the_distance_to_the_edge_from_inside():
    s = geom.pad_shape(0, 0, 20, 10, "rect", 0)
    X = np.array([[0.0, 8.0, 12.0]])
    Y = np.zeros_like(X)
    d = inner_depth(s, X, Y)
    assert d[0].tolist() == pytest.approx([5.0, 2.0, -2.0])


def test_when_both_nearest_owners_are_foreign_the_further_can_need_more_room():
    # B's pad is nearer but needs 2 mil; C's is 1 mil further and needs 8.
    b = _board(pads=[Pad("R1", "1", 20, 50, net="A", copper=[PadCopper("TopLayer", "rect", 10, 10)]),
                     Pad("R2", "1", 115, 50, net="B", copper=[PadCopper("TopLayer", "rect", 10, 10)]),
                     Pad("R3", "1", 100, 66, net="C", copper=[PadCopper("TopLayer", "rect", 10, 10)])])
    g = _grid(b)
    a, nb, nc = g.net_id["A"], g.net_id["B"], g.net_id["C"]
    ws = _at(g, 100, 50)              # 10 from B's pad, 11 from C's
    slack = g.slack(a, 0, ws, _clear({nb: 2.0, nc: 8.0}), 0.0)[0, 0]
    assert slack == pytest.approx(11.0 - 8.0)


def test_an_arc_is_an_obstacle_along_its_whole_sweep():
    from eda_agent.layout.model import Arc
    b = _board(arcs=[Arc("TopLayer", 100, 50, 30, 0, 180, 4.0, "B")])
    g = _grid(b)
    top = _at(g, 100, 80)             # on the arc's apex
    side = _at(g, 100, 20)            # on the missing half of the circle
    assert g.d1[0][top][0, 0] == pytest.approx(0.0)
    assert g.d1[0][side][0, 0] > 20.0

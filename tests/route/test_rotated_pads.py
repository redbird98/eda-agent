# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Pads at an odd rotation keep their own shape in the route model.

A pad turned 329.5 degrees used to become the box around it. On a
0.5 mm pitch connector that box covered the neighbouring pads' centres,
every terminal started inside another net's keep-out, and route_plan
called pads unreachable that route by hand.
"""

from __future__ import annotations

import math

import pytest

from eda_agent.route import model as route_model
from eda_agent.route.model import RoutingProblem, dist_point_rect, dist_seg_rect
from eda_agent.route.router import route_problem, validate_solution

RULES = {
    "clearance_mils": 4,
    "track_width_mils": {"default": 4},
    "via_size_mils": 20,
    "via_drill_mils": 10,
    "layers": ["TopLayer", "BottomLayer"],
}

PITCH = 19.685          # 0.5 mm
PAD_W, PAD_H = 11.811, 45.276   # 0.3 x 1.15 mm
ROT = 329.5


def _turn(x, y, deg):
    a = math.radians(deg)
    return x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)


def _connector(n=6, cx=500.0, cy=500.0):
    """A row of fine pads along the connector's x axis, turned ROT."""
    pads = []
    for i in range(n):
        lx = (i - (n - 1) / 2.0) * PITCH
        dx, dy = _turn(lx, 0.0, ROT)
        pads.append({"x": cx + dx, "y": cy + dy, "x_size": PAD_W,
                     "y_size": PAD_H, "shape": "Rectangular",
                     "layer": "TopLayer", "net": f"N{i}", "rotation": ROT})
    return pads


def _far_pads(n=6, cx=500.0, cy=500.0, reach=300.0):
    """One partner pad per net, fanned out beyond the pads' long ends."""
    out = []
    for i in range(n):
        lx = (i - (n - 1) / 2.0) * 80.0
        dx, dy = _turn(lx, reach, ROT)
        out.append({"x": cx + dx, "y": cy + dy, "x_size": 30, "y_size": 30,
                    "shape": "Rectangular", "layer": "TopLayer",
                    "net": f"N{i}", "rotation": 0})
    return out


def _geom(pads):
    return {"bbox": {"x1": 0, "y1": 0, "x2": 1000, "y2": 1000},
            "pads": pads, "tracks": [], "vias": []}


def test_a_turned_rect_measures_in_its_own_frame():
    # A 10 x 40 rect turned 90 degrees reaches 20 along x, 5 along y.
    assert dist_point_rect(19.0, 0.0, 0, 0, 5, 20, 90.0) == 0.0
    assert dist_point_rect(0.0, 19.0, 0, 0, 5, 20, 90.0) == pytest.approx(14.0)
    # Turned 45 degrees, the corner of a 10 x 10 square sits at 7.07.
    assert dist_point_rect(7.0, 0.0, 0, 0, 5, 5, 45.0) == 0.0
    assert dist_point_rect(8.0, 0.0, 0, 0, 5, 5, 45.0) == pytest.approx(8.0 - 50 ** 0.5)
    assert dist_seg_rect(-30, 7.0, 30, 7.0, 0, 0, 5, 5, 45.0) == 0.0
    assert dist_seg_rect(-30, 8.0, 30, 8.0, 0, 0, 5, 5, 45.0) == pytest.approx(8.0 - 50 ** 0.5)


def _blocked_centres(prob, pads):
    return [p["net"] for p in pads
            if not prob.passable(0, *prob.snap_cell(p["x"], p["y"]), p["net"])]


def test_no_pad_centre_is_inside_a_neighbours_keep_out():
    pads = _connector()
    prob = RoutingProblem.from_geometry(_geom(pads), RULES, grid_pitch_mils=2)
    assert _blocked_centres(prob, pads) == []


def test_every_net_of_a_turned_fine_pitch_connector_routes():
    pads = _connector() + _far_pads()
    prob = RoutingProblem.from_geometry(_geom(pads), RULES, grid_pitch_mils=2)
    sol = route_problem(prob)
    status = {n: r["status"] for n, r in sol["nets"].items()}
    assert set(status.values()) == {"routed"}, status
    check = validate_solution(prob, sol)
    assert check["ok"], check


def test_the_enclosing_box_made_them_unreachable(monkeypatch):
    # The old model, kept here so the test above cannot pass for a
    # reason unrelated to the pads' shape.
    def boxed(p):
        return (*route_model.rect_extents(
            float(p["x_size"]) / 2, float(p["y_size"]) / 2,
            float(p.get("rotation", 0)) % 180.0), 0.0)
    monkeypatch.setattr(route_model, "_pad_shape", boxed)
    pads = _connector()
    prob = RoutingProblem.from_geometry(_geom(pads), RULES, grid_pitch_mils=2)
    assert len(_blocked_centres(prob, pads)) == len(pads)


def test_a_blocked_centre_cell_moves_onto_its_own_copper():
    # Two 12 x 60 pads 20 apart, centres between grid points: the
    # centre of each snaps to a cell nearer the other pad's keep-out.
    pads = [
        {"x": 503.0, "y": 500.0, "x_size": 12, "y_size": 60,
         "shape": "Rectangular", "layer": "TopLayer", "net": "A", "rotation": 0},
        {"x": 523.0, "y": 500.0, "x_size": 12, "y_size": 60,
         "shape": "Rectangular", "layer": "TopLayer", "net": "B", "rotation": 0},
    ]
    prob = RoutingProblem.from_geometry(_geom(pads), RULES, grid_pitch_mils=5)
    for net, (t,) in prob.terminals.items():
        assert any(prob.passable(li, *t.cell, net) for li in t.layers), net
        x, y = prob.cell_center(*t.cell)
        pad = next(p for p in pads if p["net"] == net)
        assert dist_point_rect(x, y, pad["x"], pad["y"], 6, 30) == 0.0


def test_a_grid_coarser_than_the_pad_is_counted():
    prob = RoutingProblem.from_geometry(_geom(_connector()), RULES,
                                        grid_pitch_mils=25)
    assert prob.off_grid_terminals > 0

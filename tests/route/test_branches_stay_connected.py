# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A net reported routed is one piece of copper, and checks say where.

Corners are cut to 45s after the search, one path at a time, while a
later branch of the same net is routed into ANY cell of the tree. A
branch can therefore end on a corner (or a leg cell) that the cut then
deletes, and its track stops where there is no copper. Clearance checks
cannot see that, so the net came back routed with validation ok.

These pin three things: the cut leaves every join cell in place, the
router measures the emitted copper before calling a net routed, and
``validate_solution`` checks connectivity and names the layer of every
violation.
"""
from __future__ import annotations

import random

from eda_agent.route import router
from eda_agent.route.model import RoutingProblem
from eda_agent.route.router import (
    net_islands,
    route_geometry,
    route_problem,
    validate_solution,
)

from tests.route.test_router import RULES, _connected, _geom, _pad

# Three pads whose third branch lands on a leg of the first path, inside
# the stretch a full-depth chamfer of that corner would delete.
_CORNER_PADS = [(325, 50), (525, 325), (300, 575)]


def _net_copper(sol, net):
    return {"tracks": [t for t in sol["tracks"] if t["net_name"] == net],
            "vias": [v for v in sol["vias"] if v["net"] == net]}


def _track(x1, y1, x2, y2, net="A", layer="TopLayer", width=10):
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "width": width,
            "layer": layer, "net_name": net}


def _via(x, y, net="A"):
    return {"x": x, "y": y, "net": net, "size": 50, "hole_size": 28}


# ---------------------------------------------------------------------------
# The cut leaves join cells in place
# ---------------------------------------------------------------------------


def test_a_branch_that_ends_on_a_corner_stays_joined():
    sol = route_geometry(
        _geom([_pad(x, y, "N") for (x, y) in _CORNER_PADS]), RULES)
    assert sol["nets"]["N"]["status"] == "routed"
    assert _connected(sol, [(x, y, "TopLayer") for (x, y) in _CORNER_PADS])
    assert sol["validation"]["ok"], sol["validation"]
    assert sol["validation"]["unconnected"] == []


def _random_board(rng, n_nets=4):
    """Pads on a 25 mil grid, at least 75 mils apart on one axis, a
    quarter of them through-hole so some nets change layer."""
    taken: list[tuple[int, int]] = []
    pads = []
    for k in range(n_nets):
        for _ in range(rng.randint(2, 4)):
            while True:
                x = rng.randrange(50, 951, 25)
                y = rng.randrange(50, 551, 25)
                if all(abs(x - px) >= 75 or abs(y - py) >= 75
                       for (px, py) in taken):
                    break
            taken.append((x, y))
            layer = "MultiLayer" if rng.random() < 0.25 else "TopLayer"
            pads.append(_pad(x, y, f"N{k}", layer=layer))
    return pads


def test_every_routed_net_on_random_boards_is_one_piece():
    rng = random.Random(20261002)
    routed = branched = 0
    for _ in range(30):
        pads = _random_board(rng)
        sol = route_geometry(_geom(pads), RULES)
        assert sol["validation"]["unconnected"] == []
        for net, res in sol["nets"].items():
            if res["status"] != "routed":
                continue
            points = [(p["x"], p["y"],
                       None if p["layer"] == "MultiLayer" else p["layer"])
                      for p in pads if p["net"] == net]
            routed += 1
            branched += len(points) > 2
            assert _connected(_net_copper(sol, net), points), (
                f"{net} reported routed but its copper is in pieces: "
                f"{_net_copper(sol, net)}")
    # Branches are where the defect lives; a run without them proves
    # nothing.
    assert routed >= 30 and branched >= 15, (routed, branched)


def test_the_chamfer_leaves_a_kept_cell_in_place():
    from eda_agent.route.router import _chamfer

    problem = RoutingProblem.from_geometry(_geom([_pad(500, 300, "A")]),
                                           RULES)
    # Down a column, then right along a row: corner at (10, 18).
    cells = ([(10, y) for y in range(10, 19)]
             + [(x, 18) for x in range(11, 19)])
    joined = (12, 18)  # two cells past the corner, on the outgoing leg
    cut = _chamfer(problem, "A", 0, list(cells))
    assert joined not in cut, "the geometry must put the join in the cut"
    kept = _chamfer(problem, "A", 0, list(cells), {joined})
    assert joined in kept
    # A shallower cut is still taken: the corner is not left square.
    assert (10, 18) not in kept


def test_a_diagonal_does_not_clip_a_blocked_side_cell():
    from eda_agent.route.router import _chamfer

    problem = RoutingProblem.from_geometry(_geom([_pad(500, 300, "A")]),
                                           RULES)
    # Corner at (10, 14); a depth-2 cut runs (10, 12) -> (11, 13) ->
    # (12, 14). Both of those cells are clear, but (12, 13) beside the
    # second step is blocked.
    cells = ([(10, y) for y in range(10, 15)]
             + [(x, 14) for x in range(11, 15)])
    assert (11, 13) in _chamfer(problem, "A", 0, list(cells))
    problem.blocked[0][(12, 13)] = {None}
    out = _chamfer(problem, "A", 0, list(cells))
    assert (11, 13) not in out, out
    for a, b in zip(out, out[1:]):
        if a[0] != b[0] and a[1] != b[1]:
            for side in ((b[0], a[1]), (a[0], b[1])):
                assert problem.passable(0, *side, "A"), (a, b, side)


# ---------------------------------------------------------------------------
# Copper that does not join fails the net
# ---------------------------------------------------------------------------


def test_copper_that_does_not_join_fails_the_net(monkeypatch):
    """Put the defect back (cut corners with no regard for join cells)
    and the net must come back failed, not routed with a gap."""
    real_chamfer = router._chamfer

    def _cut_everything(problem, net, li, cells, keep=None):
        return real_chamfer(problem, net, li, cells)

    monkeypatch.setattr(router, "_chamfer", _cut_everything)
    sol = route_geometry(
        _geom([_pad(x, y, "N") for (x, y) in _CORNER_PADS]), RULES)
    res = sol["nets"]["N"]
    assert res["status"] == "failed"
    assert "2 separate islands" in res["reason"], res["reason"]
    assert res["tracks"] == [] and res["vias"] == []
    assert all(t["net_name"] != "N" for t in sol["tracks"])
    assert sol["summary"]["routed"] == 0 and sol["summary"]["failed"] == 1


def test_net_islands_counts_disjoint_copper():
    problem = RoutingProblem.from_geometry(
        _geom([_pad(100, 300, "A"), _pad(900, 300, "A")]), RULES)
    apart = [_track(100, 300, 400, 300), _track(500, 300, 900, 300)]
    assert net_islands(problem, "A", apart, []) == [[(100, 300)],
                                                   [(900, 300)]]
    bridged = apart + [_track(400, 300, 500, 300)]
    assert net_islands(problem, "A", bridged, []) == [[(100, 300),
                                                      (900, 300)]]
    # Copper edges touch at a 10 mil gap between 10 mil tracks...
    touching = [_track(100, 300, 400, 300), _track(410, 300, 900, 300)]
    assert len(net_islands(problem, "A", touching, [])) == 1
    # ...and do not at 11.
    short = [_track(100, 300, 400, 300), _track(411, 300, 900, 300)]
    assert len(net_islands(problem, "A", short, [])) == 2


def test_net_islands_needs_a_shared_layer_or_a_via():
    problem = RoutingProblem.from_geometry(
        _geom([_pad(100, 300, "A"), _pad(900, 300, "A",
                                         layer="BottomLayer")]), RULES)
    top = _track(100, 300, 500, 300)
    bottom = _track(500, 300, 900, 300, layer="BottomLayer")
    assert len(net_islands(problem, "A", [top, bottom], [])) == 2
    assert len(net_islands(problem, "A", [top, bottom],
                           [_via(500, 300)])) == 1

    # A bottom track over two top-only pads joins neither.
    smd = RoutingProblem.from_geometry(
        _geom([_pad(100, 300, "A"), _pad(900, 300, "A")]), RULES)
    under = [_track(100, 300, 900, 300, layer="BottomLayer")]
    assert len(net_islands(smd, "A", under, [])) == 2
    # Through-hole pads are on every layer, so the same track joins them.
    th = RoutingProblem.from_geometry(
        _geom([_pad(100, 300, "A", layer="MultiLayer"),
               _pad(900, 300, "A", layer="MultiLayer")]), RULES)
    assert len(net_islands(th, "A", under, [])) == 1


def test_net_islands_counts_copper_already_on_the_board():
    existing = {"x1": 400, "y1": 300, "x2": 500, "y2": 300, "width": 10,
                "layer": "TopLayer", "net": "A"}
    problem = RoutingProblem.from_geometry(
        _geom([_pad(100, 300, "A"), _pad(900, 300, "A")],
              tracks=[existing]), RULES)
    apart = [_track(100, 300, 400, 300), _track(500, 300, 900, 300)]
    assert len(net_islands(problem, "A", apart, [])) == 1
    # Another net's copper in the same place joins nothing.
    foreign = dict(existing, net="B")
    other = RoutingProblem.from_geometry(
        _geom([_pad(100, 300, "A"), _pad(900, 300, "A")],
              tracks=[foreign]), RULES)
    assert len(net_islands(other, "A", apart, [])) == 2


def test_validate_solution_lists_an_unconnected_net():
    g = _geom([_pad(100, 300, "SIG"), _pad(900, 300, "SIG")])
    prob = RoutingProblem.from_geometry(g, RULES)
    sol = route_problem(prob)
    assert sol["validation"]["ok"]
    assert sol["validation"]["unconnected"] == []
    bad = dict(sol, tracks=[])  # still claims SIG routed
    check = validate_solution(prob, bad)
    assert not check["ok"]
    assert check["violations"] == []
    assert check["unconnected"] == [{
        "net": "SIG", "islands": 2,
        "pads": [[[100, 300]], [[900, 300]]],
    }]


# ---------------------------------------------------------------------------
# Violation records name the layer, and only same-layer pairs count
# ---------------------------------------------------------------------------


_OLD_KEYS = {"kind", "net_a", "net_b", "distance_mils", "required_mils",
             "at"}


def test_violation_records_name_the_layer():
    g = _geom([_pad(100, 300, "SIG"), _pad(900, 300, "SIG"),
               _pad(500, 100, "SMD"),
               _pad(700, 100, "TH", layer="MultiLayer")])
    prob = RoutingProblem.from_geometry(g, RULES)
    sol = route_problem(prob)
    assert sol["validation"]["ok"], sol["validation"]
    bad = dict(sol)
    bad["tracks"] = sol["tracks"] + [
        _track(300, 200, 300, 400, net="EVIL"),          # crosses SIG
        _track(400, 100, 800, 100, net="EVIL2", layer="BottomLayer"),
    ]
    bad["vias"] = sol["vias"] + [_via(600, 300, net="EVIL3")]
    check = validate_solution(prob, bad)
    assert not check["ok"]
    by_kind = {}
    for v in check["violations"]:
        assert _OLD_KEYS <= set(v), v
        by_kind.setdefault((v["kind"], v["net_a"], v["net_b"]), v)

    v = by_kind[("track_track", "SIG", "EVIL")]
    assert (v["layer"], v["layer_b"]) == ("TopLayer", "TopLayer")
    # The bottom track passes under the top-only SMD pad without a
    # violation, and over the through-hole pad with one.
    assert not any(k[0] == "track_rect" and k[2] == "SMD" for k in by_kind)
    v = by_kind[("track_rect", "EVIL2", "TH")]
    assert (v["layer"], v["layer_b"]) == ("BottomLayer", "MultiLayer")
    v = by_kind[("via_track", "EVIL3", "SIG")]
    assert (v["layer"], v["layer_b"]) == ("MultiLayer", "TopLayer")


def test_a_crossing_on_the_other_layer_is_not_a_violation():
    g = _geom([_pad(100, 300, "SIG"), _pad(900, 300, "SIG"),
               _pad(500, 100, "OTHER")])
    prob = RoutingProblem.from_geometry(g, RULES)
    sol = route_problem(prob)
    assert all(t["layer"] == "TopLayer" for t in sol["tracks"])
    bad = dict(sol)
    bad["tracks"] = sol["tracks"] + [
        # Straight across SIG, and straight through OTHER's top pad,
        # both on the bottom layer.
        _track(500, 50, 500, 550, net="EVIL", layer="BottomLayer"),
    ]
    check = validate_solution(prob, bad)
    assert check["violations"] == [], check["violations"]
    assert check["ok"]
    # The same track on the top layer is reported, so the test would see
    # a violation if there were one.
    bad["tracks"][-1] = dict(bad["tracks"][-1], layer="TopLayer")
    kinds = {v["kind"] for v in validate_solution(prob, bad)["violations"]}
    assert {"track_track", "track_rect"} <= kinds

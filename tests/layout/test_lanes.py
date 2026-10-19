# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Buses: three or more nets between two parts, laid as nested lanes."""

from __future__ import annotations

import math

from eda_agent.layout import geom
from eda_agent.layout.drc import run_drc
from eda_agent.layout.route import Router
from eda_agent.layout.route.exact import sharp_bends
from eda_agent.layout.route.lanes import leads
from eda_agent.layout.route.stages import finish_report
from tests.layout.test_route import _board, _pad


def _long(comp, name, x, y, net, w=12.0, h=40.0):
    p = _pad(comp, name, x, y, net)
    p.copper[0].w, p.copper[0].h = w, h
    return p


def _bus_board(order=(0, 1, 2, 3)):
    """Two parts facing each other across the board, four nets between
    them, and a fifth that has to get across the bus."""
    b = _board(w=900, h=600)
    for k in range(4):
        b.pads.append(_long("U1", str(k + 1), 150 + 25 * k, 120, f"D{k}"))
        b.pads.append(_long("U2", str(k + 1), 600 + 25 * order[k], 480, f"D{k}"))
    b.pads += [_long("U1", "9", 80, 120, "E"), _pad("U2", "9", 820, 300, "E")]
    return b


def _route(b, **kw):
    # Plane fanout is off by default (it cost completion over the benchmark
    # boards); these tests are about what it does when it is on.
    kw.setdefault("stages", {"plane_fanout": True})
    r = Router(b, **kw)
    r.run()
    out = r.apply()
    return r, out, run_drc(out), finish_report(r, out)


def _segments(out, net):
    return [((t.x1, t.y1), (t.x2, t.y2)) for t in out.tracks if t.net == net]


def _cross(a, b, c, d) -> bool:
    def o(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    return (o(a, b, c) * o(a, b, d) < 0) and (o(c, d, a) * o(c, d, b) < 0)


def test_a_four_net_bus_is_laid_as_nested_lanes_that_never_cross():
    b = _bus_board()
    r, out, drc, rep = _route(b)
    assert drc.completion == 1.0 and not drc.violations
    bus = rep["buses"][0]
    assert bus["lanes"] == 4 and sorted(bus["nets"]) == ["D0", "D1", "D2", "D3"]
    nets = [f"D{k}" for k in range(4)]
    # No two lanes cross, on any layer: the bus is all on one.
    assert {t.layer for t in out.tracks if t.net in nets} == {bus["layer"]}
    for i in range(4):
        for j in range(i + 1, 4):
            for a, c in _segments(out, nets[i]):
                for d, e in _segments(out, nets[j]):
                    assert not _cross(a, c, d, e), (nets[i], nets[j])
    assert sharp_bends([t for t in out.tracks if t.net in nets]) == []
    # Between the two fan-ins the lanes run at one pitch: width plus
    # clearance, each its neighbour's distance from the next.
    width = max(t.width for t in out.tracks if t.net in nets)
    pitch = width + r.c
    caps = {n: [geom.capsule(a[0], a[1], c[0], c[1], 0.0) for a, c in _segments(out, n)]
            for n in nets}
    for i in range(3):
        a, c = nets[i], nets[i + 1]
        gaps = [min(geom.clearance(s, t) for t in caps[c]) for s in caps[a]
                if math.dist(s.pts[0], s.pts[-1]) > 60.0]
        assert gaps and all(abs(g - pitch) < 0.05 for g in gaps), (a, c, gaps)


def test_a_bus_whose_ends_disagree_on_order_is_left_to_the_router():
    b = _bus_board(order=(0, 2, 1, 3))
    r, out, drc, rep = _route(b)
    assert all(not x["lanes"] for x in rep["buses"])
    assert any("cross" in x["reason"] for x in rep["buses"])
    assert not drc.violations


def test_dog_bones_in_a_buss_way_are_taken_up_and_planned_again_round_it():
    # Two decoupling parts beside the bus's straight line, each GND pad's
    # dog-bone via pointing into it: between them there is no room for
    # four lanes. Rather than detour, the bus takes the vias up, is laid
    # straight, and the vias are planned again, now beside their pads.
    from eda_agent.layout.model import Layer, Region

    b = _board(w=360, h=600, layers=("TopLayer", "MidLayer1", "MidLayer2", "BottomLayer"))
    whole = [(-5, -5), (365, -5), (365, 605), (-5, 605)]
    b.regions = [Region("MidLayer1", whole, [], "GND", "pour_boundary"),
                 Region("MidLayer2", whole, [], "VCC", "pour_boundary")]
    for k in range(4):
        b.pads.append(_long("U1", str(k + 1), 142.5 + 25 * k, 80, f"D{k}"))
        b.pads.append(_long("U2", str(k + 1), 142.5 + 25 * k, 520, f"D{k}"))
    b.pads += [_pad("C1", "1", 65, 300, "VCC", size=24.0), _pad("C1", "2", 125, 300, "GND", size=24.0),
               _pad("C2", "1", 295, 300, "VCC", size=24.0), _pad("C2", "2", 235, 300, "GND", size=24.0)]
    r, out, drc, rep = _route(b)
    assert drc.completion == 1.0 and not drc.violations
    (bus,) = rep["buses"]
    assert bus["lanes"] == 4
    assert sorted(x["pad"] for x in bus["ripped_up"]) == ["C1.2", "C2.2"]
    assert all(x["replanned"] for x in bus["ripped_up"])
    # Laid straight through, no detour round the caps.
    for net in ("D0", "D1", "D2", "D3"):
        length = sum(math.hypot(t.x2 - t.x1, t.y2 - t.y1) for t in out.tracks if t.net == net)
        assert length < 1.1 * 440.0, (net, length)
    # Each GND pad still has its via, outside its pad and clear of the bus.
    for name in ("C1", "C2"):
        pad = next(p for p in b.pads if p.comp == name and p.net == "GND")
        vias = [v for v in out.vias if v.net == "GND"
                and math.hypot(v.x - pad.x, v.y - pad.y) < 60.0]
        assert vias and all(geom.clearance(geom.circle(v.x, v.y, v.diameter),
                                           pad.shape_on("TopLayer")) > 0 for v in vias)


def _jog_gap(pos, lat, lead, j, k):
    """Perpendicular distance between the 45-degree jogs of pins j and k
    (pins level across the row, jogs moving the same way)."""
    sign = 1.0 if lat[k] > 0 else -1.0
    return abs((pos[k] - pos[j]) - sign * (lead[k] - lead[j])) / math.sqrt(2)


def test_side_by_side_jogs_are_staggered_to_keep_the_pitch():
    # Pins 10 mil apart opening out to lanes 12 mil apart: two jogs that
    # start level run 7 mil apart, under the pitch. The outer pin moves out
    # further, so the inner one waits for it.
    pos = [-15.0, -5.0, 5.0, 15.0]
    offs = {0: -18.0, 1: -6.0, 2: 6.0, 3: 18.0}
    lat = [offs[k] - pos[k] for k in range(4)]
    got = leads(pos, lat, offs, 12.0)
    assert got[0] == got[3] == 0.0 and got[1] > 0 and got[2] > 0
    for j, k in ((1, 0), (2, 3)):
        assert _jog_gap(pos, lat, got, j, k) >= 12.0
    # Pins 20 mil apart closing in on lanes 12 apart, 14 mil once at 45
    # degrees: room enough, nobody waits. At 15 apart (10.6 at 45) the
    # outer ones wait.
    for spread, waits in ((20.0, False), (15.0, True)):
        pos = [-1.5 * spread, -0.5 * spread, 0.5 * spread, 1.5 * spread]
        lat = [offs[k] - pos[k] for k in range(4)]
        got = leads(pos, lat, offs, 12.0)
        assert (got[0] > 0 and got[3] > 0) == waits and got[1] == got[2] == 0.0
        for j, k in ((1, 0), (2, 3)):
            assert _jog_gap(pos, lat, got, j, k) >= 12.0

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The router on boards small enough to know the answer.

Every result is judged by the exact DRC, not by the router's own view of
its grid: a route counts only when the copper it lays connects the pads
and clears everything else.
"""

from __future__ import annotations

from eda_agent.layout.drc import run_drc
from eda_agent.layout.model import (Layer, LayoutBoard, Pad, PadCopper, Region,
                                    Rule, Track)
from eda_agent.layout.route import Router


def _rules(gap=6.0, width=6.0, via=(20.0, 10.0)):
    return [
        Rule("Clearance", "0", "All", "All", 1, True, {"gap": gap},
             f"Clearance Constraint (Gap={gap}mil) (All),(All)"),
        Rule("Width", "1", "All", "All", 1, True, {},
             f"Width Constraint (Min={width}mil) (Max={width}mil) (Preferred={width}mil) (All)"),
        Rule("RoutingVias", "2", "All", "All", 1, True, {},
             f"Routing Via (MinHoleWidth={via[1]}mil) (MaxHoleWidth={via[1]}mil) "
             f"(PreferredHoleWidth={via[1]}mil) (MinWidth={via[0]}mil) "
             f"(MaxWidth={via[0]}mil) (PreferedWidth={via[0]}mil) (All)"),
    ]


def _board(w=600, h=400, layers=("TopLayer", "BottomLayer"), **kw) -> LayoutBoard:
    return LayoutBoard(
        name="t", outline=[(0, 0), (w, 0), (w, h), (0, h)],
        layers=[Layer(n, "signal", i) for i, n in enumerate(layers)],
        rules=_rules(**kw))


def _pad(comp, name, x, y, net, size=20.0, layer="TopLayer"):
    return Pad(comp, name, x, y, net=net, copper=[PadCopper(layer, "rect", size, size)])


def _route(board):
    r = Router(board)
    rep = r.run()
    out = r.apply()
    return rep, out, run_drc(out)


def test_two_pads_are_joined_by_clean_copper():
    b = _board()
    b.pads = [_pad("R1", "1", 100, 200, "A"), _pad("R2", "1", 500, 200, "A")]
    rep, out, drc = _route(b)
    assert drc.completion == 1.0 and not drc.violations
    assert out.tracks and all(t.width == 6.0 for t in out.tracks)
    assert rep.failed == 0


def test_a_foreign_pad_in_the_way_is_passed_at_the_rule_clearance():
    b = _board()
    b.pads = [_pad("R1", "1", 100, 200, "A"), _pad("R2", "1", 500, 200, "A"),
              _pad("U1", "1", 300, 200, "B", size=60.0),
              _pad("U1", "2", 300, 60, "B", size=10.0)]
    rep, out, drc = _route(b)
    assert drc.unrouted.get("A") is None and not drc.violations


def test_a_wall_on_one_layer_is_crossed_on_the_other_through_vias():
    b = _board()
    b.pads = [_pad("R1", "1", 100, 200, "A"), _pad("R2", "1", 500, 200, "A")]
    # A keep-out across the whole top layer between the pads.
    b.tracks = [Track("TopLayer", 300, -10, 300, 410, 20.0, keepout=True)]
    rep, out, drc = _route(b)
    assert drc.completion == 1.0 and not drc.violations
    assert len(out.vias) >= 2
    assert any(t.layer == "BottomLayer" for t in out.tracks)


def test_two_nets_that_must_cross_negotiate_a_layer_each():
    b = _board()
    b.pads = [_pad("R1", "1", 100, 200, "A"), _pad("R2", "1", 500, 200, "A"),
              _pad("R3", "1", 300, 50, "B"), _pad("R4", "1", 300, 350, "B")]
    rep, out, drc = _route(b)
    assert drc.completion == 1.0 and not drc.violations


def test_a_plane_net_gets_a_via_into_its_plane_and_no_tracks_between_pads():
    b = _board()
    b.layers = [Layer("TopLayer", "signal", 0), Layer("InternalPlane1", "plane", 1, "GND"),
                Layer("BottomLayer", "signal", 2)]
    b.pads = [_pad("C1", "2", 100, 200, "GND"), _pad("C2", "2", 500, 200, "GND")]
    rep, out, drc = _route(b)
    assert drc.completion == 1.0 and not drc.violations
    assert len(out.vias) == 2, "one via per surface pad, into the plane"
    for t in out.tracks:
        assert abs(t.x2 - t.x1) + abs(t.y2 - t.y1) < 100, "a fanout, not a pad-to-pad track"


def test_what_cannot_be_routed_is_left_unrouted_not_shorted():
    b = _board(layers=("TopLayer",))
    # Pad A2 sits in a closed ring of B copper on the only layer.
    ring = [(380, 120), (620, 120), (620, 280), (380, 280)]
    hole = [(420, 160), (580, 160), (580, 240), (420, 240)]
    b.outline = [(0, 0), (700, 0), (700, 400), (0, 400)]
    b.pads = [_pad("R1", "1", 100, 200, "A"), _pad("R2", "1", 500, 200, "A"),
              _pad("J1", "1", 500, 60, "B")]
    b.regions = [Region("TopLayer", ring, [hole], "B", "copper")]
    rep, out, drc = _route(b)
    assert drc.unrouted.get("A") == 1
    assert not drc.violations


def test_a_bga_ball_too_close_for_the_preferred_via_takes_a_smaller_one():
    b = _board(gap=4.0, width=4.0, via=(20.0, 10.0))
    # The rule allows vias down to 12/6; balls on an 18 mil pitch leave no
    # room for a 20 mil via between or beside them on a one-sided board
    # where the only way out is down.
    b.rules[2].descriptor = ("Routing Via (MinHoleWidth=6mil) (MaxHoleWidth=10mil) "
                            "(PreferredHoleWidth=10mil) (MinWidth=12mil) (MaxWidth=20mil) "
                            "(PreferedWidth=20mil) (All)")
    ring = []
    for i in range(5):
        for j in range(5):
            net = "S" if (i, j) == (2, 2) else f"N{i}{j}"
            ring.append(_pad("U1", f"{i}{j}", 264 + 18 * i, 164 + 18 * j, net, size=12.0))
    ring = [Pad(p.comp, p.name, p.x, p.y, net=p.net,
                copper=[PadCopper("TopLayer", "round", 12.0, 12.0)]) for p in ring]
    b.pads = ring + [_pad("R1", "1", 300, 350, "S", layer="BottomLayer")]
    rep, out, drc = _route(b)
    assert drc.unrouted.get("S") is None and not drc.violations
    assert any(v.diameter < 20.0 for v in out.vias), "a smaller via than preferred"


def test_a_route_never_leaves_a_concave_board():
    b = _board()
    # A U: the direct line between the arms crosses the notch.
    b.outline = [(0, 0), (600, 0), (600, 400), (400, 400), (400, 150),
                 (200, 150), (200, 400), (0, 400)]
    b.pads = [_pad("R1", "1", 100, 350, "A"), _pad("R2", "1", 500, 350, "A")]
    rep, out, drc = _route(b)
    assert drc.completion == 1.0
    from eda_agent.layout import geom
    board = b.outline_shape()
    for t in out.tracks:
        for x, y in ((t.x1, t.y1), (t.x2, t.y2), ((t.x1 + t.x2) / 2, (t.y1 + t.y2) / 2)):
            assert geom.point_in_poly(x, y, board), (x, y)


def _random_board(seed: int) -> LayoutBoard:
    import random
    rng = random.Random(seed)
    b = _board(w=500, h=400)
    pads = []
    for k in range(10):
        net = f"N{k % 4}"
        x, y = rng.uniform(40, 460), rng.uniform(40, 360)
        w, h = rng.choice([(12, 30), (30, 12), (20, 20), (15, 50)])
        pads.append(Pad(f"U{k}", "1", x, y, net=net,
                        copper=[PadCopper("TopLayer", "rect", w, h)]))
    # Drop pads that overlap another's clearance: a start the rules forbid.
    kept = []
    for p in pads:
        s = p.shape_on("TopLayer")
        if all(geom_clear(s, q.shape_on("TopLayer")) > 20 for q in kept):
            kept.append(p)
    b.pads = kept
    return b


def geom_clear(a, b):
    from eda_agent.layout import geom
    return geom.clearance(a, b)


def test_whatever_is_routed_on_random_boards_is_legal():
    # The router's grid is an approximation; the exact DRC is not. Across
    # these boards nothing it lays may break a rule, whatever it manages
    # to connect.
    for seed in range(8):
        b = _random_board(seed)
        rep, out, drc = _route(b)
        assert not drc.violations, (seed, [v.as_dict() for v in drc.violations[:3]])
        assert drc.completion > 0.5, seed


def test_every_step_between_two_open_cells_clears_a_pad_corner():
    # What the router checks is cell centres; what it lays is the step
    # between two of them, which passes a convex corner closer than
    # either end. The margin for that must cover every step, so walk them
    # all round a pad set off the grid and measure each exactly. At this
    # offset a step came within 5.58 mil of the corner without it.
    from eda_agent.layout import geom
    import numpy as np
    b = _board(w=200, h=200)
    b.pads = [_pad("R1", "1", 20, 20, "A"), _pad("R2", "1", 180, 180, "A"),
              Pad("U6", "11", 100.5, 100.5, net="B",
                  copper=[PadCopper("TopLayer", "rect", 14.76, 64.96)])]
    r = Router(b)
    job = [j for j in r.jobs if j.name == "A"][0]
    row = r.rr.clearance_row(job.id)
    ok = r._slack(job.id, 0, (slice(None), slice(None)), row) >= job.width / 2 + r._bow(job) - 1e-3
    pad = b.pads[2].shape_on("TopLayer")
    spec = r.grid.spec
    worst = np.inf
    for (dx, dy) in ((1, 0), (0, 1), (1, 1), (1, -1)):
        ys, xs = np.nonzero(ok)
        ny, nx = ys + dy, xs + dx
        inside = (ny >= 0) & (ny < spec.ny) & (nx >= 0) & (nx < spec.nx)
        ys, xs, ny, nx = ys[inside], xs[inside], ny[inside], nx[inside]
        both = ok[ny, nx]
        for y0, x0, y1, x1 in zip(ys[both], xs[both], ny[both], nx[both]):
            px0, py0 = float(spec.x(x0)), float(spec.y(y0))
            if abs(px0 - 100) > 40 or abs(py0 - 100) > 60:
                continue
            step = geom.capsule(px0, py0, float(spec.x(x1)), float(spec.y(y1)), job.width)
            worst = min(worst, geom.clearance(step, pad))
    assert worst >= 6.0 - 0.06, worst


def _four_layer(*inner_pours):
    b = _board(layers=("TopLayer", "MidLayer1", "MidLayer2", "BottomLayer"))
    for layer, net, box in inner_pours:
        x0, y0, x1, y1 = box
        b.regions.append(Region(layer, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], [],
                                net, "pour_boundary"))
    return b


def test_an_inner_layer_a_pour_covers_is_a_plane_and_carries_no_tracks():
    b = _four_layer(("MidLayer1", "GND", (-10, -10, 610, 410)))
    b.pads = [_pad("C1", "2", 100, 100, "GND"), _pad("C2", "2", 500, 300, "GND"),
              _pad("R1", "1", 100, 300, "A"), _pad("R2", "1", 500, 100, "A")]
    r = Router(b)
    assert r.grid.layers == ["TopLayer", "MidLayer2", "BottomLayer"]
    r.run()
    out = r.apply()
    drc = run_drc(out)
    assert drc.completion == 1.0 and not drc.violations
    assert not [t for t in out.tracks if t.layer == "MidLayer1"]
    planes = [x for x in out.regions if x.layer == "MidLayer1" and x.kind == "plane"]
    assert [x.net for x in planes] == ["GND"]
    gnd_vias = [v for v in out.vias if v.net == "GND"]
    assert len(gnd_vias) == 2, "one via per GND pad, into the plane"


def test_a_pour_nested_in_a_plane_is_a_split_of_its_own_net():
    b = _four_layer(("MidLayer1", "+1V8", (-10, -10, 610, 410)),
                    ("MidLayer1", "CORE", (350, 200, 590, 390)))
    b.pads = [_pad("C1", "2", 100, 100, "+1V8"), _pad("C2", "2", 150, 350, "+1V8"),
              _pad("C3", "2", 450, 300, "CORE"), _pad("C4", "2", 520, 250, "CORE")]
    r = Router(b)
    planes = {(p.net, len(p.holes)) for p in r.pour_planes}
    assert planes == {("+1V8", 1), ("CORE", 0)}
    r.run()
    drc = run_drc(r.apply())
    assert drc.completion == 1.0 and not drc.violations


def test_a_redundant_plane_between_two_others_is_released_first():
    from eda_agent.layout.route.router import releasable
    order = ("TopLayer", "MidLayer2", "MidLayer3", "MidLayer4", "MidLayer1", "BottomLayer")
    b = _board(layers=order)
    box = (-10, -10, 610, 410)
    for layer, net in (("MidLayer2", "GND"), ("MidLayer3", "GND"),
                       ("MidLayer4", "+3V3"), ("MidLayer1", "GND")):
        x0, y0, x1, y1 = box
        b.regions.append(Region(layer, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], [],
                                net, "pour_boundary"))
    b.pads = [_pad("C1", "2", 100, 100, "GND"), _pad("C2", "2", 500, 300, "GND"),
              _pad("C3", "1", 100, 300, "+3V3"), _pad("C4", "1", 500, 100, "+3V3")]
    r = Router(b)
    assert releasable(r) == "MidLayer3"
    r2 = Router(b, released=frozenset({"MidLayer3", "MidLayer2"}))
    assert releasable(r2) in ("MidLayer4", "MidLayer1"),         "with none to spare, a net's only plane may become a shared layer"
    r3 = Router(b, released=frozenset({"MidLayer3", "MidLayer2", "MidLayer1"}))
    assert releasable(r3) is None, "the board keeps its last plane"


def test_with_no_plane_to_spare_the_lightest_single_plane_goes_first():
    from eda_agent.layout.route.router import releasable
    order = ("TopLayer", "MidLayer1", "MidLayer2", "BottomLayer")
    b = _board(layers=order)
    box = [(-10, -10), (610, -10), (610, 410), (-10, 410)]
    b.regions += [Region("MidLayer1", box, [], "GND", "pour_boundary"),
                  Region("MidLayer2", box, [], "VCC", "pour_boundary")]
    b.pads = [_pad(f"C{i}", "2", 60 + 40 * i, 100, "GND") for i in range(6)]
    b.pads += [_pad("U1", "1", 100, 300, "VCC"), _pad("U2", "1", 500, 300, "VCC")]
    assert releasable(Router(b)) == "MidLayer2", "VCC's two pads, not GND's six"


def test_via_styles_run_from_preferred_to_the_smallest_real_via():
    from eda_agent.layout.route.params import RouteRules
    from eda_agent.layout.rules import RuleSet
    b = _board()
    # A rule whose minimum "via" is a 6 mil hole in 6 mil of copper.
    b.rules[2].descriptor = ("Routing Via (MinHoleWidth=6mil) (MaxHoleWidth=50mil) "
                            "(PreferredHoleWidth=12mil) (MinWidth=6mil) (MaxWidth=75mil) "
                            "(PreferedWidth=26mil) (All)")
    b.pads = [_pad("R1", "1", 100, 200, "A"), _pad("R2", "1", 500, 200, "A")]
    rr = RouteRules(b, RuleSet.from_board(b), {"A": 1})
    styles = [(v.diameter, v.hole) for v in rr.vias("A")]
    assert styles[0] == (26.0, 12.0)
    assert styles[-1] == (12.0, 6.0), "the minimum hole with a 3 mil ring"
    assert all(d - h >= 6.0 - 1e-9 for d, h in styles)
    assert [d for d, _ in styles] == sorted((d for d, _ in styles), reverse=True)


def test_a_via_in_a_ball_goes_in_at_the_balls_exact_centre():
    # Balls set off the grid by a fraction of a cell: a via at the nearest
    # cell would crowd one neighbour; at the centre it clears them all.
    b = _board(gap=4.0, width=4.0, via=(20.0, 10.0))
    b.rules[2].descriptor = ("Routing Via (MinHoleWidth=6mil) (MaxHoleWidth=10mil) "
                            "(PreferredHoleWidth=10mil) (MinWidth=12mil) (MaxWidth=20mil) "
                            "(PreferedWidth=20mil) (All)")
    balls = []
    for i in range(5):
        for j in range(5):
            net = "S" if (i, j) == (2, 2) else f"N{i}{j}"
            balls.append(Pad("U1", f"{i}{j}", 264.37 + 18 * i, 164.61 + 18 * j, net=net,
                             copper=[PadCopper("TopLayer", "round", 12.0, 12.0)]))
    b.pads = balls + [_pad("R1", "1", 300, 350, "S", layer="BottomLayer")]
    rep, out, drc = _route(b)
    assert drc.unrouted.get("S") is None and not drc.violations
    s_vias = [v for v in out.vias if v.net == "S"]
    assert any(abs(v.x - 300.37) < 1e-6 and abs(v.y - 200.61) < 1e-6 for v in s_vias), \
        [(v.x, v.y) for v in s_vias]


def test_a_net_is_never_routed_wider_than_its_narrowest_pad():
    b = _board(width=100.0)
    b.rules[1].descriptor = "Width Constraint (Min=6mil) (Max=100mil) (Preferred=100mil) (All)"
    b.pads = [_pad("R1", "1", 100, 200, "A", size=20.0), _pad("R2", "1", 500, 200, "A", size=20.0)]
    rep, out, drc = _route(b)
    assert out.tracks and max(t.width for t in out.tracks) <= 20.0
    assert drc.completion == 1.0 and not drc.violations


def test_a_net_too_wide_for_the_only_way_through_is_necked_down():
    # The only way from one pad to the other is a 30 mil slot between two
    # keep-outs; the rule prefers 40 mil and allows 6.
    b = _board(width=40.0)
    b.rules[1].descriptor = "Width Constraint (Min=6mil) (Max=40mil) (Preferred=40mil) (All)"
    b.pads = [_pad("R1", "1", 100, 200, "A", size=60.0), _pad("R2", "1", 500, 200, "A", size=60.0)]
    b.layers = [b.layers[0]]
    b.tracks = [Track("TopLayer", 300, -10, 300, 185, 0.0, keepout=True),
                Track("TopLayer", 300, 215, 300, 410, 0.0, keepout=True)]
    rep, out, drc = _route(b)
    assert drc.completion == 1.0 and not drc.violations
    assert max(t.width for t in out.tracks) < 30.0


def test_two_wide_nets_keep_the_clearance_their_own_widths_need():
    # Most nets are 8 mil; A prefers 40 and B 34. Through the middle both
    # must pass a slot 78.5 mil tall, where at full width they need
    # 40/2 + 8 + 34/2 = 45 between centres and there is room for 40.
    # Judged as if the other were 8 mil wide, both went through at full
    # width 3 mil apart. One of them has to be necked down.
    b = _board(w=1000, h=500, gap=8.0, width=8.0)
    for net, w in (("A", 40.0), ("B", 34.0)):
        b.rules.insert(0, Rule("Width", f"w{net}", f"InNet('{net}')", "All", 0, True, {},
                               f"Width Constraint (Min=8mil) (Max={w}mil) "
                               f"(Preferred={w}mil) (InNet('{net}'))"))
    b.layers = [b.layers[0]]
    b.pads = [_pad("R1", "1", 100, 200, "A", size=44.0), _pad("R2", "1", 900, 200, "A", size=44.0),
              _pad("R3", "1", 100, 330, "B", size=38.0), _pad("R4", "1", 900, 330, "B", size=38.0)]
    for i, (x, y) in enumerate(((50, 40), (50, 470), (800, 40), (800, 470))):
        net = "CDEF"[i]
        b.pads += [_pad(f"S{i}", "1", x, y, net, size=12.0),
                   _pad(f"T{i}", "1", x + 150, y, net, size=12.0)]
    b.regions = [Region("TopLayer", [(250, -10), (750, -10), (750, 179), (250, 179)],
                        kind="keepout", keepout=True),
                 Region("TopLayer", [(250, 257.5), (750, 257.5), (750, 510), (250, 510)],
                        kind="keepout", keepout=True)]
    rep, out, drc = _route(b)
    assert drc.completion == 1.0 and not drc.violations
    widest = {n: max(t.width for t in out.tracks if t.net == n) for n in "AB"}
    assert widest["A"] < 40.0 or widest["B"] < 34.0


def test_a_stub_from_a_long_pads_centre_never_leaves_the_pad_into_a_clearance():
    # Three long pads of one part at a 31.5 mil pitch, as wide as the track.
    # Leaving the middle one's end toward a pad up and to the right, the
    # route starts on a cell just off the pad's axis; a stub from the pad's
    # centre to it ran slantwise out of the pad's side into the clearance
    # to the next pad. The cell alone lies inside the pad, which is joint
    # enough.
    w = 21.6535
    b = _board(w=800, h=500, gap=7.874, width=w)
    b.layers = [b.layers[0]]

    def long_pad(name, y, net):
        return Pad("U1", name, 300, y, net=net, copper=[PadCopper("TopLayer", "round", 68.8976, w)])

    b.pads = [long_pad("1", 202.5, "A"), long_pad("2", 233.9961, "B"),
              long_pad("3", 171.0039, "C"), _pad("R1", "1", 600, 352.5, "A", size=30.0)]
    rep, out, drc = _route(b)
    assert drc.completion == 1.0 and not drc.violations


def test_a_net_class_clearance_holds_between_two_routed_nets():
    # A is in class HV, which must keep 30 mil from everything; the rest
    # keep 6. A wall has a 40 mil gap in line with both nets and another
    # far off. At 6 mil both fit the near gap; at 30 one must go round.
    b = _board(w=1000, h=600, gap=6.0, width=8.0)
    b.layers = [b.layers[0]]
    b.rules[0].priority = 2
    b.rules.insert(0, Rule("Clearance", "hv", "InNetClass('HV')", "All", 1, True, {"gap": 30.0},
                           "Clearance Constraint (Gap=30mil) (InNetClass('HV')),(All)"))
    b.net_classes = {"HV": ["A"]}
    b.pads = [_pad("R1", "1", 100, 250, "A", size=12.0), _pad("R2", "1", 900, 250, "A", size=12.0),
              _pad("R3", "1", 100, 350, "B", size=12.0), _pad("R4", "1", 900, 350, "B", size=12.0)]
    b.regions = [Region("TopLayer", [(490, y0), (510, y0), (510, y1), (490, y1)],
                        kind="keepout", keepout=True)
                 for y0, y1 in ((-10, 280), (320, 520), (560, 610))]
    # Without the global stage, whose tile capacities alone sent one net
    # round: the detailed router must keep the rule by itself.
    r = Router(b, global_routing=False)
    r.run()
    drc = run_drc(r.apply())
    assert drc.completion == 1.0 and not drc.violations


def test_a_net_class_clearance_holds_against_a_pad_beyond_the_default_reach():
    # A is in class HV, which must keep 30 mil from everything. Another
    # net's pad sits with its edge 32 mil from the grid row A's straight
    # route runs on: 28 from the copper of an 8 mil track. The distance
    # fields reached only as far as the default clearance asked, so the
    # pad was never seen.
    b = _board(w=1000, h=400, gap=6.0, width=8.0, via=(12.0, 6.0))
    b.layers = [b.layers[0]]
    b.rules[0].priority = 2
    b.rules.insert(0, Rule("Clearance", "hv", "InNetClass('HV')", "All", 1, True, {"gap": 30.0},
                           "Clearance Constraint (Gap=30mil) (InNetClass('HV')),(All)"))
    b.net_classes = {"HV": ["A"]}
    b.pads = [_pad("R1", "1", 100, 200, "A", size=12.0), _pad("R2", "1", 900, 200, "A", size=12.0),
              _pad("U1", "1", 500, 232.7, "B", size=12.0)]
    rep, out, drc = _route(b)
    assert drc.completion == 1.0 and not drc.violations


def test_nets_of_a_metric_width_read_their_own_width_not_the_next_wider():
    # A and B are 0.55 mm, 21.65354 mil; C is 1 mm. Both 0.55 mm nets must
    # pass a 54 mil slot: 29.65 mil between centres fits, but judged as if
    # the other were 1 mm wide it needs 38.5. The widths the occupancy is
    # kept for are rounded to 4 places, and 21.6535 read to the last place
    # is not "at least 21.65354", so both nets read the 1 mm field.
    mm = 1 / 0.0254
    b = _board(w=1000, h=600, gap=8.0, width=0.55 * mm)
    b.rules[1].descriptor = "Width Constraint (Min=0.55mm) (Max=0.55mm) (Preferred=0.55mm) (All)"
    b.rules.insert(0, Rule("Width", "wC", "InNet('C')", "All", 0, True, {},
                           "Width Constraint (Min=1mm) (Max=1mm) (Preferred=1mm) (InNet('C'))"))
    b.layers = [b.layers[0]]
    b.pads = [_pad("R1", "1", 100, 250, "A", size=30.0), _pad("R2", "1", 900, 250, "A", size=30.0),
              _pad("R3", "1", 100, 350, "B", size=30.0), _pad("R4", "1", 900, 350, "B", size=30.0),
              _pad("R5", "1", 100, 540, "C", size=45.0), _pad("R6", "1", 300, 540, "C", size=45.0)]
    b.regions = [Region("TopLayer", [(490, y0), (510, y0), (510, y1), (490, y1)],
                        kind="keepout", keepout=True)
                 for y0, y1 in ((-10, 273), (327, 610))]
    r = Router(b, global_routing=False)
    r.run()
    drc = run_drc(r.apply())
    assert drc.completion == 1.0 and not drc.violations


def test_a_track_leaves_a_pad_too_narrow_for_the_grid_along_the_pads_axis():
    # Long pads 21.65 mil wide, one clearance (9.8425) apart, and a 19.685
    # mil track: inside a pad it may sit within a mil of the centreline,
    # and no cell falls there. The best cells inside let the track out of
    # the pad's side, and a straight stub from the centre to the first
    # cell outside cut the pad's end corner into the next pad's clearance.
    # The stub runs down the pad's axis to where the track still fits in
    # it, then to the cell.
    b = _board(w=700, h=600, gap=9.8425, width=19.685)
    b.layers = [b.layers[0]]

    def long_pad(name, x, net):
        return Pad("U1", name, x, 400, net=net, rotation=180.0,
                   copper=[PadCopper("TopLayer", "round", 21.6535, 64.9606)])

    b.pads = [long_pad("1", 301.0, ""), long_pad("2", 332.4961, "A"), long_pad("3", 363.9921, "B"),
              _pad("R1", "1", 151.0, 150, "A", size=30.0)]
    rep, out, drc = _route(b)
    assert drc.completion == 1.0 and not drc.violations


def test_a_net_is_necked_down_to_the_rules_minimum_when_half_is_below_it():
    # The rule prefers 12 mil and allows 8; the only way through is a slot
    # a 12 mil track does not fit and an 8 mil one does. Half of 12 is
    # below the rule, and halving alone never tried 8.
    b = _board(width=12.0)
    b.rules[1].descriptor = "Width Constraint (Min=8mil) (Max=12mil) (Preferred=12mil) (All)"
    b.pads = [_pad("R1", "1", 100, 200, "A", size=20.0), _pad("R2", "1", 500, 200, "A", size=20.0)]
    b.layers = [b.layers[0]]
    b.tracks = [Track("TopLayer", 300, -10, 300, 194.5, 0.0, keepout=True),
                Track("TopLayer", 300, 205.5, 300, 410, 0.0, keepout=True)]
    rep, out, drc = _route(b)
    assert drc.completion == 1.0 and not drc.violations
    assert {t.width for t in out.tracks if t.net == "A"} == {8.0}


def test_a_fine_pitch_pad_as_wide_as_its_track_is_left_along_its_axis():
    # A QFN's pads, 11.8 mil wide on a 0.5 mm pitch with 0.15 mm
    # clearance, and a 0.3 mm track: the neighbours leave it 1.5 mil
    # either side of a pad's centreline, and the exposed pad closes the
    # inner end. The 5 mil grid has no cell there, so no cell next to
    # the pad had room and the net failed with nothing else on the board.
    # The rule allows nothing narrower, so neck-down cannot help.
    b = _board(w=800, h=800, gap=5.9055, width=11.811)
    b.layers = [b.layers[0]]
    x0 = 303.0
    b.pads = [Pad("U2", str(n + 1), x0 + n * 19.685, 400, net="A" if n == 2 else f"N{n}",
                  copper=[PadCopper("TopLayer", "rect", 11.811, 29.528)]) for n in range(6)]
    b.pads += [Pad("U2", "EP", x0 + 2.5 * 19.685, 400 - 14.764 - 7.5 - 32.5, net="G",
                   copper=[PadCopper("TopLayer", "rect", 93.7, 65.0)]),
               _pad("R1", "1", x0 + 2 * 19.685, 700, "A", size=30.0)]
    rep, out, drc = _route(b)
    assert drc.completion == 1.0 and not drc.violations


def test_a_route_leaves_a_tqfp_pad_along_its_axis_and_then_across():
    # TQFP pads 11.8 by 70.9 mil on a 0.5 mm pitch, one clearance apart,
    # and a track as wide as a pad: a track inside a pad must sit on its
    # axis, and the grid has no cells there. A route left the pad from
    # the best cell inside, 2 mil off the axis, and came 1.2 mil inside
    # the clearance to the next pad. It now runs out along the axis past
    # the pad's end and across to the first cell with room.
    b = _board(w=800, h=800, gap=7.874, width=11.811)
    b.layers = [b.layers[0]]
    b.pads = [Pad("U4", str(n + 1), 500, 402.0 + (n - 2) * 19.685, net="A" if n == 2 else f"N{n}",
                  rotation=270.0, copper=[PadCopper("TopLayer", "round", 11.811, 70.866)])
              for n in range(5)]
    b.pads.append(_pad("R1", "1", 200, 552.0, "A", size=30.0))
    rep, out, drc = _route(b)
    assert drc.completion == 1.0 and not drc.violations


def test_a_stub_to_a_cell_the_track_overhangs_the_pad_at_stays_in_the_pad():
    # A long pad and a 10 mil track: at (325, 207) the track overhangs the
    # pad's side. A straight stub from the centre runs slantwise out of the
    # pad, where no other net's route was ever judged against it; one
    # passed another net's via at 5.4 mil where 6 was required. The stub
    # goes out along the pad to where the track still fits, then over.
    from eda_agent.layout.route.grid import inner_depth
    import numpy as np
    b = _board(width=10.0)
    b.pads = [Pad("U1", "1", 300, 200, net="A", copper=[PadCopper("TopLayer", "round", 80.0, 20.0)]),
              _pad("R1", "1", 500, 200, "A")]
    r = Router(b)
    job = next(j for j in r.jobs if j.name == "A")
    t = next(t for t in job.terminals if t.pad == 0)
    pts = r._stub(job, t, "TopLayer", (325.0, 207.0))
    assert pts[0] == (300.0, 200.0) and pts[-1] == (325.0, 207.0)
    shape = b.pads[0].shape_on("TopLayer")
    depth = [float(inner_depth(shape, np.array([[x]]), np.array([[y]]))[0, 0]) for x, y in pts[:-1]]
    assert len(pts) == 3 and min(depth) >= 5.0 - 1e-6


def test_a_route_that_must_add_no_conflict_keeps_off_pad_cells_ruled_out():
    # A pad narrower than the track: no cell of it holds the whole track,
    # so a track on any of them overhangs it. Another net's route rules
    # out all of them but one. A search pays nothing for the cell it
    # starts on, and a pad's cells were open whatever else was there: a
    # pour's join, which must add no conflict, started 3.9 mil from
    # another net's via.
    b = _board()
    for x_pad, x_other in ((100, 500), (500, 100)):
        # Once as the pad the route ends on, once as the one it starts from.
        b.pads = [Pad("U1", "1", x_pad, 200, net="A",
                      copper=[PadCopper("TopLayer", "rect", 5.0, 40.0)]),
                  _pad("R1", "1", x_other, 200, "A")]
        r = Router(b, global_routing=False)
        job = next(j for j in r.jobs if j.name == "A")
        t = next(t for t in job.terminals if t.pad == 0)
        assert len(t.cells) > 2 and not t.deep.any()
        wq = r._track_class(job)
        free = min(t.cells.tolist(), key=lambda c: -abs(c[1] - t.cells[:, 1].mean()))
        ruled = {tuple(c) for c in t.cells.tolist() if c != free}
        for l, y, x in ruled:
            r.occ_t[wq, l, y, x] += 1
        r.route_net(job, 1.0, hard=True)
        used = {c for path in job.paths for c in path}
        assert job.failed == 0 and not used & ruled, x_pad


def test_a_net_that_needs_more_room_is_seen_behind_two_nearer_ones():
    # A must keep 30 mil from H and 6 from the rest. Between B and C there
    # is room for A at 6; H sits just past B. Along that gap B and C are
    # the nearest two owners, and with distances kept for the nearest two
    # of all owners, H went unseen: A passed it at 23 mil.
    b = _board(w=1000, h=400, gap=6.0, width=8.0)
    b.layers = [b.layers[0]]
    b.rules[0].priority = 2
    b.rules.insert(0, Rule("Clearance", "ha", "InNet('H')", "InNet('A')", 1, True, {"gap": 30.0},
                           "Clearance Constraint (Gap=30mil) (InNet('H')),(InNet('A'))"))
    b.pads = [_pad("R1", "1", 100, 200, "A", size=12.0), _pad("R2", "1", 900, 200, "A", size=12.0),
              _pad("U1", "1", 500, 218, "B", size=6.0), _pad("U1", "2", 500, 182, "C", size=6.0),
              _pad("U2", "1", 500, 230, "H", size=6.0)]
    r = Router(b, global_routing=False)
    r.run()
    drc = run_drc(r.apply())
    assert drc.completion == 1.0 and not drc.violations


def test_a_stub_is_checked_against_other_nets_routes_not_only_fixed_copper():
    # A stub runs off the grid, so no stamp keeps other nets' routes from
    # it; one passed another net's via at 7.63 mil where 7.874 was
    # required. Checked at output against the routes as they stand.
    b = _board(w=800, h=400, gap=6.0, width=8.0)
    b.pads = [_pad("R1", "1", 100, 200, "A"), _pad("R2", "1", 700, 200, "A"),
              _pad("R3", "1", 100, 50, "B"), _pad("R4", "1", 700, 50, "B")]
    r = Router(b, global_routing=False)
    r.run()
    a = next(j for j in r.jobs if j.name == "A")
    bj = next(j for j in r.jobs if j.name == "B")
    spec = r.grid.spec
    i, j = spec.cell(400, 300)
    bj.paths.append([(0, j, i), (-1, j, i), (1, j, i)])       # a 20 mil via of B at (400, 300)
    r._routed = None
    y = float(spec.y(j))
    near = (float(spec.x(i)) - 40, y - 18.0), (float(spec.x(i)) + 40, y - 18.0)   # 18 - 10 - 4 = 4 < 6
    far = (float(spec.x(i)) - 40, y - 25.0), (float(spec.x(i)) + 40, y - 25.0)    # 25 - 10 - 4 = 11
    assert not r._routed_clears(a.id, a.width, 0, *near)
    assert r._routed_clears(a.id, a.width, 0, *far)
    assert r._routed_clears(bj.id, bj.width, 0, *near), "a net's own via is its own"


def test_a_negotiation_that_stops_getting_better_is_called_stalled():
    # A public board fell to half its first round of conflicts and then sat at
    # 11000 cells for 18 rounds of 36 s, before its planes were given
    # back. Four rounds without a tenth off the best before them is a
    # plateau; still falling is not.
    r = Router(_board())
    assert r._plateau([20000, 15000, 11000, 11100, 10950, 11050, 10990])
    assert not r._plateau([20000, 15000, 11000, 9500, 9000, 8800, 8000])
    assert not r._plateau([20000, 15000, 11000])

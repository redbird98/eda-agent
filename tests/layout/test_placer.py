# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The placer on small synthetic boards: legal, inside, and shorter."""

from __future__ import annotations

import random

from eda_agent.layout.bench import hpwl, overlaps, scramble_placement
from eda_agent.layout.drc import run_drc
from eda_agent.layout.model import Component, Layer, LayoutBoard, Pad, PadCopper, Rule
from eda_agent.layout.place.placer import SPREAD_DENSITY, Placer


def _part(ref, x, y, net_a, net_b, w=40.0, h=20.0, locked=False, th=False):
    comp = Component(ref, x=x, y=y, locked=locked,
                     courtyard=[(x - w / 2, y - h / 2), (x + w / 2, y - h / 2),
                                (x + w / 2, y + h / 2), (x - w / 2, y + h / 2)],
                     courtyard_source="courtyard")
    layers = ["TopLayer", "BottomLayer"] if th else ["TopLayer"]
    pads = [Pad(ref, "1", x - w / 4, y, net=net_a, hole=10.0 if th else 0.0,
                copper=[PadCopper(l, "rect", 10, 12) for l in layers]),
            Pad(ref, "2", x + w / 4, y, net=net_b, hole=10.0 if th else 0.0,
                copper=[PadCopper(l, "rect", 10, 12) for l in layers])]
    return comp, pads


def _chain_board(n=12, seed=3) -> LayoutBoard:
    """A chain of two-pin parts between two fixed connectors."""
    rng = random.Random(seed)
    b = LayoutBoard(name="p", outline=[(0, 0), (800, 0), (800, 500), (0, 500)],
                    layers=[Layer("TopLayer", "signal", 0), Layer("BottomLayer", "signal", 1)],
                    rules=[Rule("Clearance", "0", "All", "All", 1, True, {"gap": 6.0},
                                "Clearance Constraint (Gap=6mil) (All),(All)")])
    j1, p1 = _part("J1", 60, 250, "N0", "X0", locked=True)
    j2, p2 = _part("J2", 740, 250, f"N{n}", "X1", locked=True)
    b.components += [j1, j2]
    b.pads += p1 + p2
    for k in range(n):
        c, p = _part(f"R{k + 1}", rng.uniform(100, 700), rng.uniform(60, 440), f"N{k}", f"N{k + 1}")
        b.components.append(c)
        b.pads += p
    return b


def _placed(board, **kw):
    pl = Placer(board, **kw)
    rep = pl.run()
    return pl, rep, pl.apply()


def test_placement_is_legal_inside_and_leaves_fixed_parts_alone():
    b = scramble_placement(_chain_board(), seed=5)
    pl, rep, out = _placed(b)
    assert rep["failed"] == []
    assert overlaps(out) == []
    assert not run_drc(out).violations, "no two pads closer than the clearance"
    from eda_agent.layout import geom
    edge = out.outline_shape()
    for c in out.components:
        assert all(geom.point_in_poly(x, y, edge) for x, y in c.courtyard), c.ref
    for ref in ("J1", "J2"):
        before, after = b.component(ref), out.component(ref)
        assert (after.x, after.y, after.rotation) == (before.x, before.y, before.rotation)


def test_placement_shortens_a_scrambled_chain():
    b = scramble_placement(_chain_board(), seed=5)
    pl, rep, out = _placed(b)
    assert hpwl(out) < 0.6 * hpwl(b), (hpwl(out), hpwl(b))


def test_a_through_hole_part_keeps_room_on_both_sides():
    b = _chain_board(n=4)
    th, pads = _part("K1", 400, 250, "Y0", "Y1", w=120, h=80, locked=True, th=True)
    b.components.append(th)
    b.pads += pads
    # A small part on the bottom, dropped right under the through-hole one
    # and tied to its pins, so its wire wants it exactly there.
    c, p = _part("R9", 400, 250, "Y0", "Y1")
    c.side = "bottom"
    for q in p:
        q.copper[0].layer = "BottomLayer"
    b.components.append(c)
    b.pads += p
    pl, rep, out = _placed(b)
    assert not run_drc(out).violations
    moved = out.component("R9")
    assert abs(moved.x - 400) > 40 or abs(moved.y - 250) > 30, "it had to leave the leads' room"


def test_a_pad_of_no_part_keeps_its_room():
    b = _chain_board(n=4)
    b.pads.append(Pad("", "MH", 400, 250, net="", hole=60.0,
                      copper=[PadCopper(l, "round", 90, 90) for l in ("TopLayer", "BottomLayer")]))
    for k, c in enumerate(b.components):
        if c.ref.startswith("R"):
            from eda_agent.layout.place import set_pose
            set_pose(b, c.ref, x=400 + 5 * k, y=250)
    pl, rep, out = _placed(b)
    assert not run_drc(out).violations


def test_a_body_that_misses_its_pads_does_not_hide_them():
    # The courtyard came from a 3D body smaller than the land pattern.
    b = _chain_board(n=3)
    j = b.component("J1")
    j.courtyard = [(55, 245), (65, 245), (65, 255), (55, 255)]
    j.courtyard_source = "body"
    for q in b.pads:
        if q.comp == "J1":
            q.copper[0].h = 80.0          # tall pads, far past the body
    from eda_agent.layout.place import set_pose
    # R1 tied to both of J1's pads, so its wire wants it on top of them.
    for q in b.pads:
        if q.comp == "R1":
            q.net = "N0" if q.name == "1" else "X0"
    set_pose(b, "R1", x=60, y=250)
    pl, rep, out = _placed(b)
    assert not run_drc(out).violations


def test_parts_keep_the_board_outline_rule_from_the_edge():
    b = _chain_board()
    b.rules.append(Rule("Edge", "63", "All", "All", 1, True, {"gap": 30.0},
                        "Board Outline Clearance (Gap=30mil) (All)"))
    b = scramble_placement(b, seed=5)
    pl, rep, out = _placed(b)
    from eda_agent.layout.bench import is_fixed
    for c in out.components:
        if is_fixed(c):
            continue
        for x, y in c.courtyard:
            assert 30.0 - 1e-6 <= x <= 770.0 + 1e-6 and 30.0 - 1e-6 <= y <= 470.0 + 1e-6, (c.ref, x, y)


def test_the_band_inside_the_outline_rule_is_closed_to_parts():
    b = _chain_board()
    b.rules.append(Rule("Edge", "63", "All", "All", 1, True, {"gap": 30.0},
                        "Board Outline Clearance (Gap=30mil) (All)"))
    pl = Placer(b)
    pl.legalize()
    c = pl.cell
    occ = pl.occ["top"]
    row = int(250 / c)
    # Cells 10 and 25 mil in from the left edge: closed. At 60 mil: open
    # unless a part took it.
    assert occ[row, int(10 / c)] and occ[row, int(25 / c)]
    assert occ[int(10 / c), int(400 / c)], "and along the bottom edge"


def test_what_a_part_keeps_out_is_its_body_and_its_pads():
    # A body need not cover the land pattern; the pads count on their own.
    b = _chain_board(n=2)
    j = b.component("J1")
    j.courtyard = [(55, 245), (65, 245), (65, 255), (55, 255)]
    j.courtyard_source = "body"
    part = Placer(b).by_ref["J1"]
    assert len(part.keep) == 1 + 2


def _plain_board(w, h, edge_gap=None):
    rules = [Rule("Clearance", "0", "All", "All", 1, True, {"gap": 6.0},
                  "Clearance Constraint (Gap=6mil) (All),(All)")]
    if edge_gap is not None:
        rules.append(Rule("BoardOutline", "1", "All", "All", 1, True, {"gap": edge_gap},
                          f"Board Outline Clearance (Gap={edge_gap}mil) (All)"))
    return LayoutBoard(name="p", outline=[(0, 0), (w, 0), (w, h), (0, h)],
                       layers=[Layer("TopLayer", "signal", 0), Layer("BottomLayer", "signal", 1)],
                       rules=rules)


def test_a_part_that_fits_one_slot_only_is_found_there():
    # A slot a few mil wider than the part. The search steps a quarter of
    # the part's size at a time, and from where this one started it
    # stepped over the slot; every spot it fits is then found at once.
    b = _plain_board(400, 120, edge_gap=0.0)
    for ref, x in (("J1", 70), ("J2", 330)):
        c, p = _part(ref, x, 60, "A", "B", w=140, h=110, locked=True)
        b.components.append(c)
        b.pads += p
    c, p = _part("U1", 150, 60, "A", "B", w=110, h=40)
    b.components.append(c)
    b.pads += p
    pl = Placer(b)
    assert pl.legalize() == []
    assert overlaps(pl.apply()) == []


def test_bodies_may_touch_where_their_copper_keeps_its_clearance():
    # Two parts whose bodies must sit 3 mil apart to fit, their pads 30
    # apart. Bodies kept a clearance apart like copper, the second part
    # had no room.
    b = _plain_board(400, 120, edge_gap=0.0)
    for ref, x in (("J1", 60), ("J2", 340)):
        c, p = _part(ref, x, 60, "A", "B", w=100, h=110, locked=True)
        b.components.append(c)
        b.pads += p
    for ref in ("C1", "C2"):
        c, p = _part(ref, 200, 60, "A", "B", w=80, h=40)
        b.components.append(c)
        b.pads += p
    pl = Placer(b)
    assert pl.legalize() == []
    out = pl.apply()
    assert overlaps(out) == [] and not run_drc(out).violations


def test_a_footprints_own_copper_keeps_other_parts_off():
    # J1's footprint draws a copper bar well past its pads; R1 is pulled
    # right onto it. Placed by its body and pads alone, J1 let R1's pads
    # sit on the bar.
    from eda_agent.layout.model import Track
    b = _plain_board(400, 200)
    j1, p1 = _part("J1", 50, 100, "A", "C", locked=True)
    j2, p2 = _part("J2", 350, 100, "D", "E", locked=True)
    b.components += [j1, j2]
    b.pads += p1 + p2
    b.tracks.append(Track("TopLayer", 80, 100, 290, 100, 30.0, "A", comp="J1"))
    r1, pr = _part("R1", 200, 100, "C", "D")
    b.components.append(r1)
    b.pads += pr
    pl, rep, out = _placed(b)
    assert rep["failed"] == [] and not run_drc(out).violations


def test_a_part_with_no_designator_and_the_free_pads_stay_put():
    # A part with no designator: its pads read as belonging to no part,
    # like a mounting hole's. Moved by the name "", every such pad went
    # with it; placed, it failed on a pad its part did not own.
    b = _chain_board(n=3)
    anon, pads = _part("", 400, 400, "N1", "Y", w=30, h=16)
    b.components.append(anon)
    b.pads += pads
    b.pads.append(Pad("", "MH", 700, 80, net="", hole=40.0,
                      copper=[PadCopper("TopLayer", "round", 60, 60), PadCopper("BottomLayer", "round", 60, 60)]))
    before = [(q.x, q.y) for q in b.pads if not q.comp]
    s = scramble_placement(b, seed=2)
    assert [(q.x, q.y) for q in s.pads if not q.comp] == before
    pl, rep, out = _placed(s)
    assert [(q.x, q.y) for q in out.pads if not q.comp] == before


def test_decoupling_parts_sit_at_their_ics_supply_pins():
    # An IC off the board's middle, with its supply and return made big
    # nets by a far part holding thirty pads of each, as ground and a rail
    # are on a real board. Big nets are left out of the wirelength, so
    # nothing pulled the four decoupling parts anywhere: they ended 190
    # to 470 mil from the IC.
    from eda_agent.layout.bench import decoupling_distances
    b = LayoutBoard(name="p", outline=[(0, 0), (1600, 0), (1600, 1200), (0, 1200)],
                    layers=[Layer("TopLayer", "signal", 0), Layer("BottomLayer", "signal", 1)],
                    rules=[Rule("Clearance", "0", "All", "All", 1, True, {"gap": 6.0},
                                "Clearance Constraint (Gap=6mil) (All),(All)")])
    b.components.append(Component("U1", x=1300, y=900, locked=True,
                                  courtyard=[(1200, 800), (1400, 800), (1400, 1000), (1200, 1000)],
                                  courtyard_source="courtyard"))
    for n in range(16):
        side, k = divmod(n, 4)
        off = -60 + 40 * k
        x, y = [(1300 + off, 790), (1410, 900 + off), (1300 - off, 1010), (1190, 900 - off)][side]
        net = "VCC" if k == 0 else ("GND" if k == 3 else f"S{n}")
        b.pads.append(Pad("U1", str(n + 1), x, y, net=net, copper=[PadCopper("TopLayer", "rect", 16, 16)]))
    b.components.append(Component("J1", x=100, y=500, locked=True,
                                  courtyard=[(60, 100), (140, 100), (140, 900), (60, 900)],
                                  courtyard_source="courtyard"))
    for n in range(60):
        b.pads.append(Pad("J1", str(n + 1), 80 + 40 * (n % 2), 120 + 13 * (n // 2),
                          net="VCC" if n % 2 else "GND", copper=[PadCopper("TopLayer", "rect", 10, 8)]))
    for k in range(4):
        c, p = _part(f"C{k + 1}", 300 + 150 * k, 150, "VCC", "GND", w=30, h=16)
        b.components.append(c)
        b.pads += p
    from eda_agent.layout.place.placer import DECAP_PULL
    pl = Placer(scramble_placement(b, seed=4), decap_pull=DECAP_PULL)
    rep = pl.run()
    out = pl.apply()
    assert rep["failed"] == [] and overlaps(out) == []
    d = decoupling_distances(out)
    assert len(d) == 4 and max(d) <= 40.0


def test_a_part_that_fits_nowhere_whole_is_placed_by_its_copper():
    # J1's body covers nearly the whole board, its pads at the ends. U1's
    # body fits nowhere clear of it; its pads fit under J1's body, away
    # from J1's. People place parts so on dense boards. Left where
    # spreading put it, such a part lay on other parts' copper.
    b = _plain_board(300, 120, edge_gap=0.0)
    b.components.append(Component("J1", x=150, y=60, locked=True,
                                  courtyard=[(10, 10), (290, 10), (290, 110), (10, 110)],
                                  courtyard_source="body"))
    b.pads += [Pad("J1", "1", 25, 60, net="A", copper=[PadCopper("TopLayer", "rect", 20, 20)]),
               Pad("J1", "2", 275, 60, net="B", copper=[PadCopper("TopLayer", "rect", 20, 20)])]
    c, p = _part("U1", 150, 60, "A", "B", w=60, h=40)
    b.components.append(c)
    b.pads += p
    pl, rep, out = _placed(b)
    assert rep["failed"] == [] and rep["body_overlaps"] == ["U1"]
    assert not run_drc(out).violations


def test_a_footprints_silkscreen_is_not_copper():
    # J1 is a row of pads across the top of the board; R1's silkscreen
    # outline is nearly as tall as the board, so wherever R1 sits its top
    # edge crosses that row. Taken as copper, it left R1 nowhere to go;
    # drawings are not copper.
    from eda_agent.layout.model import Track
    b = _plain_board(400, 200, edge_gap=0.0)
    b.components.append(Component("J1", x=200, y=180, locked=True,
                                  courtyard=[(5, 172), (395, 172), (395, 188), (5, 188)],
                                  courtyard_source="courtyard"))
    b.pads += [Pad("J1", str(k + 1), 15 + 20 * k, 180, net=f"N{k}",
                   copper=[PadCopper("TopLayer", "rect", 10, 12)]) for k in range(19)]
    r1, pr = _part("R1", 200, 100, "N1", "N2")
    b.components.append(r1)
    b.pads += pr
    b.tracks += [Track("TopOverlay", 50, 20, 350, 20, 8.0, comp="R1"),
                 Track("TopOverlay", 350, 20, 350, 180, 8.0, comp="R1"),
                 Track("TopOverlay", 350, 180, 50, 180, 8.0, comp="R1"),
                 Track("TopOverlay", 50, 180, 50, 20, 8.0, comp="R1")]
    pl = Placer(b)
    assert pl.legalize() == []


def test_a_small_circuit_on_a_large_board_stays_by_its_connectors():
    # Twelve parts in a chain between two connectors in one corner of a
    # 4000 mil board. Spread over the whole board, they went as far as
    # 3400 mil from the connectors; spread compactly, over room they
    # would fill 40% of, they stay by them, as the person would have them.
    import math
    rng = random.Random(3)
    b = LayoutBoard(name="p", outline=[(0, 0), (4000, 0), (4000, 4000), (0, 4000)],
                    layers=[Layer("TopLayer", "signal", 0), Layer("BottomLayer", "signal", 1)],
                    rules=[Rule("Clearance", "0", "All", "All", 1, True, {"gap": 6.0},
                                "Clearance Constraint (Gap=6mil) (All),(All)")])
    j1, p1 = _part("J1", 200, 200, "N0", "X0", locked=True)
    j2, p2 = _part("J2", 600, 200, "N12", "X1", locked=True)
    b.components += [j1, j2]
    b.pads += p1 + p2
    for k in range(12):
        c, p = _part(f"R{k + 1}", rng.uniform(100, 3900), rng.uniform(100, 3900), f"N{k}", f"N{k + 1}")
        b.components.append(c)
        b.pads += p
    pl, rep, out = _placed(scramble_placement(b, seed=5), spread_density=SPREAD_DENSITY)
    assert rep["failed"] == [] and overlaps(out) == []
    assert max(math.hypot(c.x - 400, c.y - 200) for c in out.components if c.ref.startswith("R")) <= 600

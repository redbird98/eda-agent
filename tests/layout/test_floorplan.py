# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Floorplan-first placement ("blocks") on small synthetic boards.

Every board here is made up: an IC with its decoupling and pull-ups,
identical driver channels, a connector, a keepout. The tests look at
where the parts end up relative to the pins they serve, not at numbers
from any real design.
"""

from __future__ import annotations

import math

import pytest

from eda_agent.layout.bench import overlaps, scramble_placement
from eda_agent.layout.drc import run_drc
from eda_agent.layout.model import Component, Layer, LayoutBoard, Pad, PadCopper, Region, Rule
from eda_agent.layout.place import floorplan
from eda_agent.layout.place.floorplan import FLAG_AT, GRID, Legaliser, _align, place_blocks
from eda_agent.layout.place.placer import Placer


def board(w, h) -> LayoutBoard:
    return LayoutBoard(name="synthetic", outline=[(0, 0), (w, 0), (w, h), (0, h)],
                       layers=[Layer("TopLayer", "signal", 0), Layer("BottomLayer", "signal", 1)],
                       rules=[Rule("Clearance", "0", "All", "All", 1, True, {"gap": 6.0},
                                   "Clearance Constraint (Gap=6mil) (All),(All)")])


def _box(x, y, w, h):
    return [(x - w / 2, y - h / 2), (x + w / 2, y - h / 2), (x + w / 2, y + h / 2), (x - w / 2, y + h / 2)]


def two_pin(b, ref, x, y, na, nb, w=40.0, h=20.0, footprint="0603", locked=False):
    b.components.append(Component(ref, footprint=footprint, x=x, y=y, locked=locked,
                                  courtyard=_box(x, y, w, h), courtyard_source="courtyard"))
    b.pads += [Pad(ref, "1", x - w / 4, y, net=na, copper=[PadCopper("TopLayer", "rect", w / 4, h * 0.6)]),
               Pad(ref, "2", x + w / 4, y, net=nb, copper=[PadCopper("TopLayer", "rect", w / 4, h * 0.6)])]


def quad(b, ref, x, y, nets, body=200.0, pitch=20.0, footprint="QFP", locked=False):
    """A square IC, len(nets) / 4 pads a side, numbered counter-clockwise
    from the top of the left side."""
    n = len(nets) // 4
    half, span = body / 2, (n - 1) * pitch
    b.components.append(Component(ref, footprint=footprint, x=x, y=y, locked=locked,
                                  courtyard=_box(x, y, body + 40, body + 40),
                                  courtyard_source="courtyard"))
    k = 0
    for side in range(4):
        for j in range(n):
            t = span / 2 - j * pitch
            px, py, pw, ph = [(x - half - 8, y + t, 24, 12), (x - t, y - half - 8, 12, 24),
                              (x + half + 8, y - t, 24, 12), (x + t, y + half + 8, 12, 24)][side]
            b.pads.append(Pad(ref, str(k + 1), px, py, net=nets[k],
                              copper=[PadCopper("TopLayer", "rect", pw, ph)]))
            k += 1


def sot6(b, ref, x, y, nets, footprint="SOT23-6"):
    b.components.append(Component(ref, footprint=footprint, x=x, y=y,
                                  courtyard=_box(x, y, 140, 120), courtyard_source="courtyard"))
    for k, net in enumerate(nets):
        col, row = divmod(k, 3)
        px = x - 45 if col == 0 else x + 45
        py = y + 37 - row * 37 if col == 0 else y - 37 + row * 37
        b.pads.append(Pad(ref, str(k + 1), px, py, net=net, copper=[PadCopper("TopLayer", "rect", 30, 20)]))


def header(b, ref, x, y, nets, pitch=100.0):
    span = (len(nets) - 1) * pitch
    b.components.append(Component(ref, footprint="HDR", x=x, y=y, locked=True,
                                  courtyard=_box(x, y, 100, span + 100), courtyard_source="courtyard"))
    for k, net in enumerate(nets):
        b.pads.append(Pad(ref, str(k + 1), x, y + span / 2 - k * pitch, net=net, hole=40.0,
                          copper=[PadCopper(l, "round", 66, 66) for l in ("TopLayer", "BottomLayer")]))


def mcu_board() -> LayoutBoard:
    """An MCU: its supply on pad 4 (left side) with two capacitors, a
    pull-up on pad 21 (right side), an LED through a resistor on pad 12,
    and a header on the left edge. Ground and the supply reach enough
    pads to read as supplies."""
    b = board(2000, 1500)
    nets = [f"S{k + 1}" for k in range(32)]
    nets[3] = "VCC"
    for k in (7, 15, 23, 31):
        nets[k] = "GND"
    quad(b, "U1", 1000, 750, nets)
    two_pin(b, "C1", 300, 300, "VCC", "GND")
    two_pin(b, "C2", 400, 300, "VCC", "GND")
    two_pin(b, "R1", 500, 300, "S21", "VCC")
    two_pin(b, "R2", 600, 300, "S12", "LED")
    two_pin(b, "D1", 700, 300, "LED", "GND", footprint="LED0603")
    header(b, "J1", 80, 750, ["S1", "S2", "S3", "VCC", "GND", "GND"])
    return scramble_placement(b, 3)


def placed(b, **kw):
    pl = Placer(b, strategy="blocks", **kw)
    rep = pl.run()
    return pl, rep, pl.apply()


def _pad(b, ref, name):
    return next(p for p in b.pads if p.comp == ref and p.name == name)


def _centre(b, ref):
    c = b.component(ref)
    xs = [q[0] for q in c.courtyard]
    ys = [q[1] for q in c.courtyard]
    return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2


def _side_of(b, head, x, y):
    """Which side of a head's body a point lies beyond: R, L, T or B."""
    hx, hy = _centre(b, head)
    dx, dy = x - hx, y - hy
    if abs(dx) >= abs(dy):
        return "R" if dx > 0 else "L"
    return "T" if dy > 0 else "B"


# -- an IC's block --------------------------------------------------------------

def test_decoupling_sits_closest_on_the_supply_pins_side_and_a_pull_up_on_its_signal_pins():
    pl, rep, out = placed(mcu_board())
    assert rep["failed"] == [] and overlaps(out) == []
    vcc, sig = _pad(out, "U1", "4"), _pad(out, "U1", "21")
    side_vcc, side_sig = _side_of(out, "U1", vcc.x, vcc.y), _side_of(out, "U1", sig.x, sig.y)
    assert side_vcc != side_sig, "the two pins face opposite ways"
    for c in ("C1", "C2"):
        assert _side_of(out, "U1", *_centre(out, c)) == side_vcc, c
    assert _side_of(out, "U1", *_centre(out, "R1")) == side_sig
    # The capacitors are the parts nearest the supply pin...
    dist = {r: math.dist((vcc.x, vcc.y), _centre(out, r)) for r in ("C1", "C2", "R1", "R2", "D1")}
    assert max(dist["C1"], dist["C2"]) < min(dist["R1"], dist["R2"], dist["D1"])
    # ...each turned with its supply pad towards the pin, and the pull-up
    # with its signal pad towards its own.
    for c in ("C1", "C2"):
        to_pin = [math.dist((vcc.x, vcc.y), (q.x, q.y)) for q in out.pads if q.comp == c]
        on_vcc = [q.net == "VCC" for q in out.pads if q.comp == c]
        assert on_vcc[to_pin.index(min(to_pin))], c
    r1 = {q.net: math.dist((sig.x, sig.y), (q.x, q.y)) for q in out.pads if q.comp == "R1"}
    assert r1["S21"] < r1["VCC"]


def test_a_chained_part_stands_further_out_in_its_pins_column():
    pl, rep, out = placed(mcu_board())
    pin = _pad(out, "U1", "12")
    side = _side_of(out, "U1", pin.x, pin.y)
    assert _side_of(out, "U1", *_centre(out, "R2")) == side == _side_of(out, "U1", *_centre(out, "D1"))
    assert math.dist((pin.x, pin.y), _centre(out, "R2")) < math.dist((pin.x, pin.y), _centre(out, "D1"))


def test_one_big_part_on_a_side_does_not_stretch_the_sides_columns():
    # Six pull-ups on neighbouring pins and one big part on the next pin
    # of the same side: sized by the big part, the six stood a big part's
    # width apart.
    b = board(2400, 1600)
    nets = [f"S{k + 1}" for k in range(32)]
    nets[3] = "VCC"
    for k in (7, 15, 23, 31):
        nets[k] = "GND"
    quad(b, "U1", 1200, 800, nets)
    for k in range(6):
        two_pin(b, f"R{k + 1}", 200 + 60 * k, 200, f"S{17 + k}", "VCC")
    two_pin(b, "L1", 1200, 1400, "S23", "BIG", w=300.0, h=120.0, footprint="IND")
    two_pin(b, "C1", 300, 1400, "VCC", "GND")
    two_pin(b, "C2", 400, 1400, "VCC", "GND")
    pl, rep, out = placed(scramble_placement(b, 6))
    assert rep["failed"] == [] and overlaps(out) == []
    pins = [_pad(out, "U1", str(17 + k)) for k in range(6)]
    side = _side_of(out, "U1", pins[0].x, pins[0].y)
    along = (lambda xy: xy[1]) if side in "RL" else (lambda xy: xy[0])
    spots = [along(_centre(out, f"R{k + 1}")) for k in range(6)]
    assert max(spots) - min(spots) < 300.0, spots


def test_every_part_is_on_the_lattice_and_the_board_is_legal():
    pl, rep, out = placed(mcu_board())
    assert not run_drc(out).violations
    for p in pl.parts:
        if p.fixed:
            continue
        for v in (p.x, p.y):
            assert abs(v / GRID - round(v / GRID)) < 1e-6, (p.ref, v)
        assert p.rot in (0.0, 90.0, 180.0, 270.0)


# -- capacitors over the ICs of one supply ----------------------------------

def test_capacitors_on_a_shared_supply_go_to_their_own_ics():
    b = board(2400, 1600)
    for ref, x in (("U1", 600), ("U2", 1800)):
        nets = [f"{ref}S{k}" for k in range(32)]
        nets[3] = "VCC"
        for k in (7, 15, 23, 31):
            nets[k] = "GND"
        quad(b, ref, x, 800, nets)
    for k in range(4):
        two_pin(b, f"C{k + 1}", 200 + 100 * k, 200, "VCC", "GND")
    pl, rep, out = placed(scramble_placement(b, 2))
    owner = {r: bl["head"] for bl in rep["blocks"] for r in bl["members"]}
    assert sorted(owner[f"C{k + 1}"] for k in range(4)) == ["U1", "U1", "U2", "U2"]
    for k in range(4):
        c = f"C{k + 1}"
        pin = _pad(out, owner[c], "4")
        assert math.dist((pin.x, pin.y), _centre(out, c)) < 120.0, c


# -- identical channels -----------------------------------------------------------

def channel_board(n=6) -> LayoutBoard:
    """``n`` identical driver channels (an IC, its capacitor, a feedback
    resistor and a sense resistor) between two headers."""
    b = board(2600, 1800)
    for ch in range(n):
        sot6(b, f"U{ch + 1}", 300 + 300 * ch, 1500, [f"IN{ch}", "GND", f"OUT{ch}", f"FB{ch}", "VCC", f"SNS{ch}"])
        two_pin(b, f"C{ch + 1}", 300 + 300 * ch, 1200, "VCC", "GND")
        two_pin(b, f"R{ch + 1}", 400 + 300 * ch, 1200, f"FB{ch}", f"OUT{ch}")
        two_pin(b, f"R{ch + 11}", 500 + 300 * ch, 1200, f"SNS{ch}", "GND")
    header(b, "J1", 80, 900, [f"IN{ch}" for ch in range(n)] + ["VCC", "GND"])
    header(b, "J2", 2520, 900, [f"OUT{ch}" for ch in range(n)] + ["GND", "GND"])
    return scramble_placement(b, 4)


def test_identical_channels_come_out_as_identical_tiles_on_a_grid():
    n = 6
    pl, rep, out = placed(channel_board(n))
    assert rep["failed"] == [] and overlaps(out) == []
    chans = sorted((bl for bl in rep["blocks"] if "channel" in bl), key=lambda bl: bl["channel"][1])
    assert len(chans) == n and len({bl["channel"][0] for bl in chans}) == 1
    # Identical tiles: every member stands where its counterpart does in
    # the first channel, relative to the channel's IC, turned the same.
    def frame(bl):
        head = out.component(bl["head"])
        rel = {}
        for r in bl["members"]:
            c = out.component(r)
            role = "C" if r.startswith("C") else ("FB" if any(q.net.startswith("FB") for q in out.pads
                                                          if q.comp == r) else
                                                  "SNS" if r.startswith("R") else "U")
            rel[role] = (round(c.x - head.x, 3), round(c.y - head.y, 3), c.rotation % 360)
        return rel
    first = frame(chans[0])
    for bl in chans[1:]:
        assert frame(bl) == first, bl["name"]
    # A strict grid: the ICs on few columns and rows, at one pitch, in
    # channel order from the top left.
    xs = sorted({round(out.component(bl["head"]).x, 3) for bl in chans})
    ys = sorted({round(out.component(bl["head"]).y, 3) for bl in chans}, reverse=True)
    assert len(xs) * len(ys) >= n and len(xs) * len(ys) < n + max(len(xs), len(ys))
    for vals in (xs, ys):
        steps = {round(abs(b - a), 3) for a, b in zip(vals, vals[1:])}
        assert len(steps) <= 1, vals
    order = [(ys.index(round(out.component(bl["head"]).y, 3)), xs.index(round(out.component(bl["head"]).x, 3)))
             for bl in chans]
    assert order == sorted(order), "channel order runs row by row"


# -- the floorplan's rectangles ---------------------------------------------------

def test_block_rectangles_keep_off_each_other_and_off_a_keepout():
    b = channel_board(3)
    nets = [f"M{k}" for k in range(32)]
    nets[3] = "VCC"
    for k in (7, 15, 23, 31):
        nets[k] = "GND"
    quad(b, "U9", 1300, 800, nets)
    for k in range(3):
        two_pin(b, f"C{20 + k}", 1300, 300 + 50 * k, "VCC", "GND")
    keep = _box(1300, 900, 700, 500)
    b.regions.append(Region("KeepOutLayer", keep, kind="keepout", keepout=True))
    pl, rep, out = placed(scramble_placement(b, 1))
    rects = [bl["rect"] for bl in rep["blocks"] if not bl["anchored"]]
    assert len(rects) == 4 and rep["floorplan_packed"] == [] and rep["floorplan_overflow"] == []

    def apart(a, c):
        return a[2] <= c[0] or c[2] <= a[0] or a[3] <= c[1] or c[3] <= a[1]

    for i, a in enumerate(rects):
        for c in rects[i + 1:]:
            assert apart(a, c), (a, c)
    kx0, ky0 = keep[0]
    kx1, ky1 = keep[2]
    for a in rects:
        assert apart(a, (kx0, ky0, kx1, ky1)), a
    for c in out.components:
        if c.ref in ("J1", "J2"):
            continue
        xs = [q[0] for q in c.courtyard]
        ys = [q[1] for q in c.courtyard]
        assert apart((min(xs), min(ys), max(xs), max(ys)), (kx0, ky0, kx1, ky1)), c.ref


def test_rectangles_of_blocks_pulled_together_do_not_overlap():
    # Four different ICs, each with a capacitor on every side (a cross,
    # its corners empty), all wired to one header in the middle and to
    # each other: pulled together, crosses would nest corner into corner.
    b = board(2600, 2600)
    header(b, "J1", 1300, 1300, [f"H{k}" for k in range(8)])
    for u in range(4):
        nets = [f"U{u}S{k}" for k in range(32)]
        for k in (3, 11, 19, 27):
            nets[k] = "VCC"
        for k in (7, 15, 23, 31):
            nets[k] = "GND"
        nets[0], nets[1] = f"H{2 * u}", f"H{2 * u + 1}"
        nets[9], nets[17] = f"X{u}", f"X{(u + 1) % 4}"
        quad(b, f"U{u + 1}", 400 + 600 * u, 300, nets, footprint=f"QFP-{u}")
        for k in range(4):
            two_pin(b, f"C{4 * u + k + 1}", 200 + 100 * k, 2300 - 100 * u, "VCC", "GND")
    pl, rep, out = placed(scramble_placement(b, 5))
    assert not any("channel" in bl for bl in rep["blocks"])
    assert rep["floorplan_packed"] == [] and rep["floorplan_overflow"] == []
    rects = [bl["rect"] for bl in rep["blocks"] if not bl["anchored"]]
    assert len(rects) == 4
    for i, a in enumerate(rects):
        for c in rects[i + 1:]:
            assert a[2] <= c[0] or c[2] <= a[0] or a[3] <= c[1] or c[3] <= a[1], (a, c)


def test_a_placed_blocks_whole_rectangle_is_closed_to_the_blocks_after_it():
    # Closing only a block's parts leaves its rectangle's empty corners
    # open, and the next block's rectangle could take them.
    from eda_agent.layout.place.blocks import find_blocks
    from eda_agent.layout.place.compose import Shapes
    from eda_agent.layout.place.floorplan import GAP, MARGIN, Floorplan
    b = channel_board(3)
    nets = [f"M{k}" for k in range(32)]
    nets[3], nets[19] = "VCC", "VCC"
    for k in (7, 15, 23, 31):
        nets[k] = "GND"
    quad(b, "U9", 1300, 800, nets)
    two_pin(b, "C20", 1300, 300, "VCC", "GND")
    two_pin(b, "C21", 1300, 400, "VCC", "GND")
    pl = Placer(scramble_placement(b, 1), strategy="blocks")
    _align(pl, GRID)
    fp = Floorplan(pl, find_blocks(pl.board, {p.ref for p in pl.parts if not p.fixed}),
                   Shapes(pl), GRID, GAP, MARGIN)
    fp.run()
    for u in fp.units:
        assert not u.packed and not u.overflow
        for blk in u.blocks:
            x0, y0, x1, y1 = fp.rects[blk.name]
            j0, j1, i0, i1 = fp._cells((x0 - MARGIN / 2, y0 - MARGIN / 2, x1 + MARGIN / 2, y1 + MARGIN / 2))
            for s in u.sides:
                assert fp.occ[s][j0:j1, i0:i1].all(), blk.name


# -- the legaliser ------------------------------------------------------------------

def test_the_legaliser_records_every_move_and_flags_the_long_ones():
    b = board(1200, 800)
    b.components.append(Component("K1", x=600, y=400, locked=True, courtyard=_box(600, 400, 300, 300),
                                  courtyard_source="courtyard"))
    b.pads.append(Pad("K1", "1", 600, 400, net="K", copper=[PadCopper("TopLayer", "rect", 40, 40)]))
    for ref in ("R1", "R2", "R3"):
        two_pin(b, ref, 100, 100, "A", "B")
    pl = Placer(b, strategy="blocks")
    _align(pl, GRID)
    leg = Legaliser(pl, GRID, FLAG_AT)

    def on(v):
        return round(v / GRID) * GRID

    leg.put(pl.by_ref["R1"], on(600), on(400), 0.0)          # in the middle of K1
    leg.put(pl.by_ref["R2"], on(600 + 150 + 10), on(400), 0.0)   # just over K1's edge
    leg.put(pl.by_ref["R3"], on(200), on(200), 0.0)          # free
    moved = {m["designator"]: m for m in leg.moves}
    assert set(moved) == {"R1", "R2"}
    assert moved["R1"]["distance"] > FLAG_AT >= moved["R2"]["distance"] > 0
    for m in moved.values():
        assert math.dist(m["from"], m["to"]) == pytest.approx(m["distance"], abs=0.01)


def test_a_connectors_parts_take_a_side_where_nothing_stands():
    # A fixed part over the top half of the connector's open side: the
    # parts go to a side where they all fit (at most nudged off the edge
    # together), rather than into it and scattered round it.
    b = board(1600, 1000)
    header(b, "J1", 80, 500, ["A1", "A2", "A3", "A4", "A5", "GND"])
    for k in range(5):
        two_pin(b, f"R{k + 1}", 800, 800, f"A{k + 1}", "GND")
    b.components.append(Component("K1", x=267.5, y=670, locked=True, courtyard=_box(267.5, 670, 265, 300),
                                  courtyard_source="courtyard"))
    b.pads.append(Pad("K1", "1", 267.5, 670, net="K", copper=[PadCopper("TopLayer", "rect", 40, 40)]))
    pl = Placer(b, strategy="blocks")
    rep = place_blocks(pl)
    assert overlaps(pl.apply()) == []
    assert all(m["distance"] < 40.0 for m in rep["legaliser_moves"]), rep["legaliser_moves"]
    j1 = next(bl for bl in rep["blocks"] if bl["name"] == "J1")
    assert set(j1["members"]) == {"J1", "R1", "R2", "R3", "R4", "R5"}


def crowded_connector_board() -> LayoutBoard:
    """A connector on the left edge, fixed parts above and below it, its
    parts in a column on its right, and a fixed part over the top half of
    that column."""
    b = board(1600, 1000)
    header(b, "J1", 80, 500, ["A1", "A2", "A3", "A4", "A5", "GND"])
    for k in range(5):
        two_pin(b, f"R{k + 1}", 800, 800, f"A{k + 1}", "GND")
    for ref, x, y, w, h in (("K1", 267.5, 670, 265, 300), ("K2", 200, 900, 380, 160),
                            ("K3", 200, 100, 380, 160)):
        b.components.append(Component(ref, x=x, y=y, locked=True, courtyard=_box(x, y, w, h),
                                      courtyard_source="courtyard"))
        b.pads.append(Pad(ref, "1", x, y, net=ref, copper=[PadCopper("TopLayer", "rect", 40, 40)]))
    return b


def test_moves_over_the_threshold_are_reported_as_floorplan_problems():
    # The parts on the connector's upper pins have to go round the fixed
    # part, the nearest of them only a little.
    pl = Placer(crowded_connector_board(), strategy="blocks")
    rep = place_blocks(pl, flag_at=FLAG_AT)
    assert rep["legaliser_moves"], "the parts could not stand where their block put them"
    assert rep["flagged_moves"] == [m for m in rep["legaliser_moves"] if m["distance"] > FLAG_AT]
    assert rep["flagged_moves"]
    assert overlaps(pl.apply()) == []


def test_a_board_the_blocks_do_not_fit_is_placed_analytically_and_says_so():
    from eda_agent.layout.place.floorplan import FALLBACK_SHARE
    b = crowded_connector_board()
    pl = Placer(b, strategy="blocks")
    rep = pl.run()
    assert rep["strategy"] == "analytic" and rep["fallback"]["from"] == "blocks"
    assert rep["fallback"]["flagged_share"] > FALLBACK_SHARE
    ref = Placer(b, strategy="analytic")
    ref.run()
    want = {p.ref: (p.x, p.y, p.rot) for p in ref.parts if not p.fixed}
    assert {p.ref: (p.x, p.y, p.rot) for p in pl.parts if not p.fixed} == want
    kept = Placer(b, strategy="blocks", fallback=0).run()
    assert kept["strategy"] == "blocks" and "fallback" not in kept and kept["flagged_moves"]


def test_the_correlator_counts_what_a_mask_covers_at_every_offset():
    import numpy as np

    from eda_agent.layout.place.floorplan import Correlator
    rng = np.random.default_rng(7)
    busy = (rng.random((23, 31)) < 0.3).astype(np.float32)
    corr = Correlator(busy)
    for h, w in ((9, 6), (1, 1), (4, 6), (9, 2)):
        mask = (rng.random((h, w)) < 0.5).astype(np.float32)
        got = corr.valid(mask)
        assert got.shape == (23 - h + 1, 31 - w + 1)
        for j in range(got.shape[0]):
            for i in range(got.shape[1]):
                assert abs(got[j, i] - float((busy[j:j + h, i:i + w] * mask).sum())) < 1e-3


# -- the analytic strategy -------------------------------------------------------

def test_the_analytic_strategy_still_runs_without_the_floorplan(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the analytic strategy reached the floorplan")

    monkeypatch.setattr(floorplan, "place_blocks", boom)
    b = mcu_board()
    pl = Placer(b, strategy="analytic")
    rep = pl.run()
    out = pl.apply()
    assert "blocks" not in rep and rep["failed"] == [] and overlaps(out) == []
    assert pl.cell == 2.5, "the analytic raster is untouched"


def test_an_unknown_strategy_is_refused():
    with pytest.raises(ValueError):
        Placer(board(100, 100), strategy="spiral")


# -- the job and the tool --------------------------------------------------------

def test_the_job_reports_the_blocks_and_the_legaliser():
    from eda_agent.layout.place.job import place_job
    res = place_job({"board": mcu_board(), "strategy": "blocks"})
    assert res["strategy"] == "blocks" == res["summary"]["strategy"]
    names = {bl["name"]: bl for bl in res["blocks"]}
    assert set(names["U1"]["members"]) >= {"U1", "C1", "C2", "R1", "R2", "D1"}
    x0, y0, x1, y1 = names["U1"]["rect"]
    assert x0 < x1 and y0 < y1
    for m in res["moves"]:
        if m["designator"] in names["U1"]["members"]:
            assert x0 - 1 <= m["x"] <= x1 + 1 and y0 - 1 <= m["y"] <= y1 + 1
    assert "legaliser_moves" in res and "flagged_moves" in res
    assert res["summary"]["overlaps"] == [] and res["summary"]["violations"] == 0


def test_the_job_says_when_the_fallback_is_reported_and_used():
    from eda_agent.layout.place.job import place_job
    res = place_job({"board": crowded_connector_board(), "strategy": "blocks"})
    assert res["strategy"] == "analytic" == res["summary"]["strategy"]
    assert res["fallback"]["from"] == "blocks"
    assert any("analytic strategy instead" in n for n in res["notes"])


@pytest.mark.asyncio
async def test_the_tool_refuses_an_unknown_strategy_before_reading_the_board(monkeypatch, tmp_path):
    from eda_agent.layout import read_altium
    from tests.layout.test_autoplace_tools import BOARD_FILE, _Bridge, _tool
    bridge = _Bridge(focused=BOARD_FILE)
    tool = _tool(monkeypatch, tmp_path, bridge, "pcb_autoplace")
    reads = []

    def read(expect_file, *a, **k):
        reads.append(expect_file)
        raise AssertionError("the board was read")

    monkeypatch.setattr(read_altium, "read_live_board", read)
    out = await tool(expect_file=BOARD_FILE, strategy="spiral")
    assert "strategy" in out.get("error", "") and not reads and not bridge.calls


def test_the_live_view_gets_the_blocks_after_placement(monkeypatch):
    # The Layout tab draws block outlines only when the job hands them over.
    from eda_agent.design import live
    from eda_agent.layout.place.job import place_job

    sent = []
    monkeypatch.setattr(live, "publish_safe",
                        lambda board, note="", kind="", extra=None, **kw: sent.append(extra or {}))
    res = place_job({"board": mcu_board(), "strategy": "blocks"})
    after = sent[-1]
    assert after.get("blocks") and after["blocks"] == res["blocks"]
    assert all(len(bl["rect"]) == 4 for bl in after["blocks"])


def test_blocks_is_the_default_strategy():
    from eda_agent.layout.place.job import place_job
    from eda_agent.layout.place.placer import DEFAULT_STRATEGY

    assert DEFAULT_STRATEGY == "blocks"
    assert place_job({"board": mcu_board()})["strategy"] == "blocks"

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Exact clearance between copper shapes, checked against hand arithmetic.

Every expected value below is worked out by hand from the shapes, not read
back from the code. A clearance engine that is right "to a grid cell" is
the defect this replaces: the old router boxed pads and passed its own
check on a 0.5 mm pitch part that violated on the board.
"""

from __future__ import annotations

import math

import pytest

from eda_agent.layout import geom

MM = 1000.0 / 25.4  # mils per millimetre


def test_two_circles():
    a = geom.circle(0, 0, 4)
    b = geom.circle(10, 0, 4)
    assert geom.clearance(a, b) == pytest.approx(6.0)


def test_overlap_is_negative():
    a = geom.circle(0, 0, 10)
    b = geom.circle(6, 0, 10)
    assert geom.clearance(a, b) == pytest.approx(-4.0)


def test_track_to_via_perpendicular():
    track = geom.capsule(-50, 0, 50, 0, 10)       # 5 mil half width
    via = geom.circle(0, 20, 12)                  # 6 mil radius
    assert geom.clearance(track, via) == pytest.approx(20 - 5 - 6)


def test_track_end_cap_is_round():
    track = geom.capsule(0, 0, 50, 0, 10)
    via = geom.circle(60, 0, 2)
    assert geom.clearance(track, via) == pytest.approx(10 - 5 - 1)


def test_rectangular_pad_corner_to_circle():
    pad = geom.pad_shape(0, 0, 10, 10, "rect")
    dot = geom.circle(8, 8, 2)
    assert geom.clearance(pad, dot) == pytest.approx(math.hypot(3, 3) - 1)


def test_rounded_rectangle_corner():
    # 50% of half the shorter side: corner radius 2.5, inner square 5 x 5.
    pad = geom.pad_shape(0, 0, 10, 10, "roundrect", corner_pct=50)
    dot = geom.circle(8, 8, 2)
    assert geom.clearance(pad, dot) == pytest.approx(
        math.hypot(8 - 2.5, 8 - 2.5) - 2.5 - 1)


def test_rounded_rectangle_flat_side_matches_the_rectangle():
    rr = geom.pad_shape(0, 0, 10, 10, "roundrect", corner_pct=50)
    dot = geom.circle(0, 20, 2)
    assert geom.clearance(rr, dot) == pytest.approx(20 - 5 - 1)


def test_oval_pad_and_its_rotation():
    oval = geom.pad_shape(0, 0, 20, 10, "round")
    above = geom.circle(0, 10, 0)
    assert geom.clearance(oval, above) == pytest.approx(5.0)
    upright = geom.pad_shape(0, 0, 20, 10, "round", rotation=90)
    assert geom.clearance(upright, above) == pytest.approx(0.0)


def test_pad_offset_is_in_the_pad_frame():
    pad = geom.pad_shape(0, 0, 4, 4, "round", rotation=90, offset=(10, 0))
    # Offset (10, 0) rotated 90 degrees lands at (0, 10).
    assert pad.pts[0] == pytest.approx((0.0, 10.0))


def test_fine_pitch_gap_is_exact():
    """0.25 mm pads at 0.5 mm pitch are exactly 0.25 mm apart."""
    a = geom.pad_shape(0, 0, 0.25 * MM, 1.0 * MM, "rect")
    b = geom.pad_shape(0.5 * MM, 0, 0.25 * MM, 1.0 * MM, "rect")
    assert geom.clearance(a, b) == pytest.approx(0.25 * MM)


def test_segment_through_a_polygon_touches_it():
    square = geom.polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
    track = geom.capsule(-50, 50, 150, 50, 10)
    assert geom.core_distance(square, track) == 0.0
    assert geom.clearance(square, track) == pytest.approx(-5.0)


def test_a_hole_in_a_pour_is_outside_it():
    pour = geom.polygon([(0, 0), (100, 0), (100, 100), (0, 100)],
                        holes=[[(40, 40), (60, 40), (60, 60), (40, 60)]])
    in_hole = geom.circle(50, 50, 0)
    in_copper = geom.circle(20, 50, 0)
    assert geom.clearance(pour, in_hole) == pytest.approx(10.0)
    assert geom.clearance(pour, in_copper) == pytest.approx(0.0)


def test_polygon_to_polygon():
    a = geom.polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    b = geom.polygon([(13, 0), (20, 0), (20, 10), (13, 10)])
    assert geom.clearance(a, b) == pytest.approx(3.0)
    inner = geom.polygon([(2, 2), (4, 2), (4, 4), (2, 4)])
    assert geom.clearance(a, inner) == pytest.approx(0.0)


def test_octagon_chamfer():
    octo = geom.pad_shape(0, 0, 40, 40, "octagon")
    # The diagonal corner is cut: the chamfer edge runs from (10, 20) to
    # (20, 10), so the point (20, 20) is sqrt(50) from it.
    corner = geom.circle(20, 20, 0)
    assert geom.clearance(octo, corner) == pytest.approx(math.sqrt(50))


def test_arc_chords_stay_within_tolerance():
    tol = 0.05
    pts = geom.arc_points(0, 0, 200, 0, 90, tol)
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        assert 200 - math.hypot(mx, my) <= tol + 1e-9
    assert pts[0] == pytest.approx((200.0, 0.0))
    assert pts[-1] == pytest.approx((0.0, 200.0), abs=1e-9)


def test_full_circle_arc():
    pts = geom.arc_points(0, 0, 50, 0, 360)
    assert pts[0] == pytest.approx(pts[-1], abs=1e-9)
    assert len(pts) > 8


def test_copper_to_board_edge():
    board = geom.polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)])
    near_edge = geom.circle(30, 500, 20)
    assert geom.shape_to_outline_clearance(near_edge, board) == pytest.approx(20.0)
    off_board = geom.circle(-100, 500, 20)
    assert geom.shape_to_outline_clearance(off_board, board) < 0


def test_bbox_includes_the_radius():
    assert geom.capsule(0, 0, 10, 0, 4).bbox == (-2, -2, 12, 2)

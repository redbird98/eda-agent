# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A round board outline renders as arcs, every quadrant, the right way round.

Altium's outline segment i runs from its own vertex to the next one. The
renderer read the vertex as the END of its segment, so the first arc was
never drawn and the closing Z drew a diagonal across one quadrant.

Each arc is checked by the centre SVG would draw it around (the endpoint
to centre conversion of SVG 1.1 F.6.5): a reversed sweep flag puts that
centre on the wrong side of the chord, which an endpoint check misses.
"""

from __future__ import annotations

import math
import re

import pytest

from eda_agent.render.pcb_svg import PcbRenderOptions, _render_outline

R = 1000.0


def _seg(x, y, a1, a2, cx=0.0, cy=0.0, r=R):
    return {"kind": "arc", "x": x, "y": y, "cx": cx, "cy": cy,
            "radius": r, "angle1": a1, "angle2": a2}


def _path(outline):
    svg = _render_outline(outline, PcbRenderOptions())
    return re.search(r' d="([^"]+)"', svg).group(1)


def _commands(d):
    return re.findall(r"([MLAZ])([^MLAZ]*)", d)


def _arc_centre(sx, sy, r, large, sweep, ex, ey):
    """SVG's own centre for an arc with rx = ry = r and no rotation."""
    x1p, y1p = (sx - ex) / 2.0, (sy - ey) / 2.0
    num = max(0.0, r * r * r * r - r * r * y1p * y1p - r * r * x1p * x1p)
    den = r * r * y1p * y1p + r * r * x1p * x1p
    k = math.sqrt(num / den) if den else 0.0
    if large == sweep:
        k = -k
    cxp, cyp = k * y1p, -k * x1p
    return cxp + (sx + ex) / 2.0, cyp + (sy + ey) / 2.0


def _arcs(d):
    """(start, centre, end) per A command, following the path."""
    out, cur = [], None
    for cmd, args in _commands(d):
        nums = [float(v) for v in args.split()]
        if cmd in "ML":
            cur = (nums[0], nums[1])
        elif cmd == "A":
            r, _rot, large, sweep, ex, ey = nums[0], nums[2], int(nums[3]), int(nums[4]), nums[5], nums[6]
            out.append((cur, _arc_centre(cur[0], cur[1], r, large, sweep, ex, ey), (ex, ey)))
            cur = (ex, ey)
    return out


def test_four_quadrants_counter_clockwise_are_all_arcs_around_the_centre():
    outline = [_seg(R, 0, 0, 90), _seg(0, R, 90, 180),
               _seg(-R, 0, 180, 270), _seg(0, -R, 270, 360)]
    d = _path(outline)
    assert "L" not in d, d
    arcs = _arcs(d)
    assert len(arcs) == 4
    for start, centre, end in arcs:
        assert math.dist(centre, (0, 0)) < 1e-6, (start, centre, end)
    assert arcs[-1][2] == (R, 0.0)


def test_a_clockwise_outline_sweeps_the_other_way_around_the_same_centre():
    # The same circle traced clockwise: segment 0 runs from (R, 0) down to
    # (0, -R), which is Altium's arc 270..360 entered at its angle2 end.
    outline = [_seg(R, 0, 270, 360), _seg(0, -R, 180, 270),
               _seg(-R, 0, 90, 180), _seg(0, R, 0, 90)]
    arcs = _arcs(_path(outline))
    assert len(arcs) == 4
    for start, centre, end in arcs:
        assert math.dist(centre, (0, 0)) < 1e-6, (start, centre, end)
    flags =[args.split()[4] for cmd, args in _commands(_path(outline)) if cmd == "A"]
    assert flags == ["0", "0", "0", "0"]


def test_one_full_circle_segment_is_drawn_in_two_halves():
    arcs = _arcs(_path([_seg(R, 0, 0, 360)]))
    assert len(arcs) == 2
    for _start, centre, _end in arcs:
        assert math.dist(centre, (0, 0)) < 1e-6
    assert arcs[0][2] == pytest.approx((-R, 0.0))
    assert arcs[1][2] == pytest.approx((R, 0.0))


def test_a_rounded_rectangle_closes_through_its_last_corner_arc():
    # 2000 x 1000 with 200-radius corners, counter-clockwise from the
    # bottom edge. The last segment is the bottom-left corner arc, which
    # must end back at the first vertex rather than be replaced by Z.
    c = 200.0
    outline = [
        {"kind": "line", "x": c, "y": 0},
        _seg(2000 - c, 0, 270, 360, 2000 - c, c, c),
        {"kind": "line", "x": 2000, "y": c},
        _seg(2000, 1000 - c, 0, 90, 2000 - c, 1000 - c, c),
        {"kind": "line", "x": 2000 - c, "y": 1000},
        _seg(c, 1000, 90, 180, c, 1000 - c, c),
        {"kind": "line", "x": 0, "y": 1000 - c},
        _seg(0, c, 180, 270, c, c, c),
    ]
    d = _path(outline)
    arcs = _arcs(d)
    centres = [(2000 - c, c), (2000 - c, 1000 - c), (c, 1000 - c), (c, c)]
    assert len(arcs) == 4
    for (_s, centre, _e), want in zip(arcs, centres):
        assert math.dist(centre, want) < 1e-6
    assert arcs[-1][2] == (c, 0.0)
    assert d.count("L") == 4


def test_a_plain_polygon_still_closes_with_z():
    d = _path([{"x": 0, "y": 0}, {"x": 2000, "y": 0},
               {"x": 2000, "y": 1000}, {"x": 0, "y": 1000}])
    assert d == "M 0.0 0.0 L 2000.0 0.0 L 2000.0 1000.0 L 0.0 1000.0 Z"

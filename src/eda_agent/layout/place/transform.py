# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Moving a component on the exact model: place, turn, and flip sides.

Everything the footprint carries moves with it: pads (position, turn,
offset, the layers of their copper), the courtyard, the 3D bodies, and
the footprint's own tracks, arcs, vias and regions. A flip to the other
side mirrors the footprint in x about its origin and swaps the outer
copper layers, as Altium does.
"""

from __future__ import annotations

import math

from ..model import BOTTOM, TOP, LayoutBoard

_SWAP = {"TopLayer": "BottomLayer", "BottomLayer": "TopLayer"}


class _Frame:
    """World <-> footprint frame of one placement (x, y, turn, side)."""

    def __init__(self, x: float, y: float, rotation: float, side: str):
        self.x, self.y = x, y
        self.a = math.radians(rotation)
        self.mirror = side == BOTTOM

    def to_local(self, px: float, py: float) -> tuple[float, float]:
        dx, dy = px - self.x, py - self.y
        c, s = math.cos(-self.a), math.sin(-self.a)
        lx, ly = dx * c - dy * s, dx * s + dy * c
        return (-lx if self.mirror else lx), ly

    def to_world(self, lx: float, ly: float) -> tuple[float, float]:
        if self.mirror:
            lx = -lx
        c, s = math.cos(self.a), math.sin(self.a)
        return self.x + lx * c - ly * s, self.y + lx * s + ly * c


def _angle(old: _Frame, new: _Frame, a: float) -> float:
    """A direction's angle (degrees) carried from one frame to the other."""
    local = a - math.degrees(old.a)
    if old.mirror:
        local = 180.0 - local
    if new.mirror:
        local = 180.0 - local
    return (local + math.degrees(new.a)) % 360.0


def set_pose(board: LayoutBoard, ref: str, x: float | None = None,
                   y: float | None = None, rotation: float | None = None,
                   side: str | None = None) -> None:
    """Move ``ref`` in place; arguments left None keep their value."""
    comp = board.component(ref)
    if comp is None:
        raise KeyError(ref)
    old = _Frame(comp.x, comp.y, comp.rotation, comp.side)
    new_side = side or comp.side
    new = _Frame(comp.x if x is None else x, comp.y if y is None else y,
                 comp.rotation if rotation is None else rotation, new_side)
    flip = old.mirror != new.mirror

    def pt(px, py):
        return new.to_world(*old.to_local(px, py))

    def layer(name):
        return _SWAP.get(name, name) if flip else name

    for p in board.pads:
        if p.comp != ref:
            continue
        p.x, p.y = pt(p.x, p.y)
        p.rotation = _angle(old, new, p.rotation)
        for c in p.copper:
            c.layer = layer(c.layer)
            if flip:
                # The turn became 180 - a; a mirror in x is that turn
                # composed with a mirror in y, so in the pad's own frame
                # the offset keeps its x and changes the sign of its y.
                c.offset_y = -c.offset_y
        if flip and p.hole_type == "slot":
            p.hole_rotation = (180.0 - p.hole_rotation) % 360.0
    for t in board.tracks:
        if t.comp == ref:
            t.x1, t.y1 = pt(t.x1, t.y1)
            t.x2, t.y2 = pt(t.x2, t.y2)
            t.layer = layer(t.layer)
    for a in board.arcs:
        if a.comp == ref:
            a.cx, a.cy = pt(a.cx, a.cy)
            a1, a2 = _angle(old, new, a.a1), _angle(old, new, a.a2)
            # A mirror reverses the sweep: the arc now runs end to start.
            a.a1, a.a2 = (a2, a1) if flip else (a1, a2)
            a.layer = layer(a.layer)
    for v in board.vias:
        if v.comp == ref:
            v.x, v.y = pt(v.x, v.y)
            if flip:
                v.low_layer, v.high_layer = layer(v.high_layer), layer(v.low_layer)
    for r in board.regions:
        if r.comp == ref:
            r.outline = [pt(*q) for q in r.outline]
            r.holes = [[pt(*q) for q in h] for h in r.holes]
            r.layer = layer(r.layer)
    comp.courtyard = [pt(*q) for q in comp.courtyard]
    for b in comp.bodies:
        b["outline"] = [pt(*q) for q in b.get("outline", [])]
    comp.x, comp.y = new.x, new.y
    comp.rotation = math.degrees(new.a) % 360.0
    comp.side = new_side if new_side in (TOP, BOTTOM) else comp.side

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Benchmark: strip a finished board, let an engine redo it, score both.

A board a person finished is the only honest yardstick. The harness takes
one, removes what an engine is meant to produce, hands the rest to the
engine, and scores the result with the same exact DRC it scores the
person's board with. The person's numbers (vias, copper length) are the
reference; completion and violations are absolute.

What a routing benchmark removes is everything a router would lay down:
free tracks, arcs and vias, poured copper, and free copper regions. What
it keeps is the design the router is given: placement, footprint copper
(including footprint vias), planes and their splits, pour boundaries,
keepouts, cutouts and rules. Poured copper goes because it was poured
around the person's tracks: left in, it would hand the router channels
cut to the shape of the answer.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable

from .drc import run_drc
from .model import LayoutBoard
from .rules import RuleSet

Engine = Callable[[LayoutBoard], LayoutBoard]


def copy_board(board: LayoutBoard) -> LayoutBoard:
    return LayoutBoard.from_dict(board.to_dict())


def strip_routing(board: LayoutBoard) -> LayoutBoard:
    """The board as a router receives it: placed, unrouted."""
    out = copy_board(board)
    out.tracks = [t for t in out.tracks if t.comp or t.keepout]
    out.arcs = [a for a in out.arcs if a.comp or a.keepout]
    out.vias = [v for v in out.vias if v.comp]
    out.regions = [r for r in out.regions
                   if r.kind in ("plane", "pour_boundary", "keepout", "cutout")
                   or r.comp]
    return out


def routing_stats(board: LayoutBoard) -> dict:
    """Copper a router laid: free vias, and free track length per layer."""
    per_layer: dict[str, float] = {}
    for t in board.tracks:
        if t.comp or t.keepout:
            continue
        per_layer[t.layer] = per_layer.get(t.layer, 0.0) + math.hypot(t.x2 - t.x1, t.y2 - t.y1)
    for a in board.arcs:
        if a.comp or a.keepout:
            continue
        span = abs(a.a2 - a.a1) % 360.0 or 360.0
        per_layer[a.layer] = per_layer.get(a.layer, 0.0) + a.radius * math.radians(span)
    return {
        "vias": sum(1 for v in board.vias if not v.comp),
        "length": round(sum(per_layer.values()), 1),
        "length_by_layer": {k: round(v, 1) for k, v in sorted(per_layer.items())},
    }


@dataclass
class Score:
    completion: float
    missing_connections: int
    connections_needed: int
    violations: int
    vias: int
    length: float
    seconds: float = 0.0
    unrouted: dict[str, int] = field(default_factory=dict)
    unevaluated_rules: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "completion": round(self.completion, 4),
            "missing_connections": self.missing_connections,
            "connections_needed": self.connections_needed,
            "violations": self.violations,
            "vias": self.vias, "length": self.length,
            "seconds": round(self.seconds, 2),
            "unevaluated_rules": self.unevaluated_rules,
        }


def score(board: LayoutBoard, seconds: float = 0.0) -> Score:
    rules = RuleSet.from_board(board)
    rep = run_drc(board, rules)
    stats = routing_stats(board)
    return Score(rep.completion, rep.missing_connections, rep.connections_needed,
                 len(rep.violations), stats["vias"], stats["length"], seconds,
                 dict(rep.unrouted), rules.unevaluated())


@dataclass
class RouteResult:
    board: str
    human: Score
    start: Score
    engine: Score

    def as_dict(self) -> dict:
        h, e = self.human, self.engine
        return {
            "board": self.board,
            "human": h.as_dict(), "start": self.start.as_dict(), "engine": e.as_dict(),
            # Ratios against the person's board; above 1 is worse.
            "vias_vs_human": round(e.vias / h.vias, 3) if h.vias else None,
            "length_vs_human": round(e.length / h.length, 3) if h.length else None,
        }


def route_benchmark(board: LayoutBoard, engine: Engine) -> RouteResult:
    """Score the person's board, strip it, route it, score the engine's."""
    human = score(board)
    stripped = strip_routing(board)
    start = score(stripped)
    t0 = time.perf_counter()
    routed = engine(copy_board(stripped))
    engine_score = score(routed, time.perf_counter() - t0)
    return RouteResult(board.name, human, start, engine_score)


# ---------------------------------------------------------------------------
# Placement benchmark
# ---------------------------------------------------------------------------

#: Parts a placement benchmark leaves where the person put them: what sits
#: where it does for mechanical reasons, not for the circuit.
FIXED_PREFIXES = ("J", "P", "CN", "USB", "H", "MH", "FID", "TP", "T", "BTN", "SW",
                  "K", "BT", "ANT")


def is_fixed(comp) -> bool:
    # A part with no designator cannot be moved by name: moving "" moved
    # every pad of no part with it, mounting holes and all.
    if comp.locked or not comp.ref:
        return True
    prefix = "".join(ch for ch in comp.ref if ch.isalpha()).upper()
    return prefix in FIXED_PREFIXES


def scramble_placement(board: LayoutBoard, seed: int = 0) -> LayoutBoard:
    """The board with every movable part dropped at random on it: a
    placement problem whose answer the person's board already holds."""
    import random

    from . import geom
    from .place import set_pose

    out = copy_board(board)
    rng = random.Random(seed)
    xs = [p[0] for p in out.outline]
    ys = [p[1] for p in out.outline]
    shape = out.outline_shape()
    for c in out.components:
        if is_fixed(c):
            continue
        for _ in range(200):
            x, y = rng.uniform(min(xs), max(xs)), rng.uniform(min(ys), max(ys))
            if geom.point_in_poly(x, y, shape):
                break
        set_pose(out, c.ref, x=x, y=y, rotation=rng.choice((0.0, 90.0, 180.0, 270.0)))
    return out


def hpwl(board: LayoutBoard, skip_nets: set[str] | None = None) -> float:
    """Half the perimeter of each net's pad box, summed: the wirelength
    estimate placement is scored on."""
    total = 0.0
    for net, pads in board.pads_by_net().items():
        if skip_nets and net in skip_nets or len(pads) < 2:
            continue
        xs = [p.x for p in pads]
        ys = [p.y for p in pads]
        total += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return total


def overlaps(board: LayoutBoard, min_area: float = 1.0) -> list[tuple[str, str]]:
    """Pairs of parts on one side whose courtyards share more than
    ``min_area`` square mils. Touching is not overlapping: two polygons
    that meet measure a distance of 0, so area is the test, not distance."""
    from . import geom

    # What each part keeps out: its courtyard or body, or, when the reader
    # had only its pads, the pads one by one (an edge connector's pads can
    # span the board and fence nothing between them).
    pads_of: dict[str, list] = {}
    for p in board.pads:
        if p.comp:
            pads_of.setdefault(p.comp, []).append(p)
    shapes = []
    for c in board.components:
        if c.courtyard_source == "pads":
            for p in pads_of.get(c.ref, []):
                s = p.shape_on(p.layers()[0]) if p.layers() else None
                if s is not None:
                    x0, y0, x1, y1 = s.bbox
                    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
                    shapes.append((c.ref, c.side, ring, geom.polygon(ring)))
        elif len(c.courtyard) >= 3:
            shapes.append((c.ref, c.side, c.courtyard, geom.polygon(c.courtyard)))
    out = set()
    for i in range(len(shapes)):
        ra, sa, pa, a = shapes[i]
        for j in range(i + 1, len(shapes)):
            rb, sb, pb, b = shapes[j]
            if ra == rb or sa != sb or not geom.bboxes_near(a, b, 0.0):
                continue
            if geom.overlap_area(pa, pb) > min_area:
                out.add(tuple(sorted((ra, rb))))
    return sorted(out)


def decoupling_distances(board: LayoutBoard, ic_pads: int = 8) -> list[float]:
    """For each part that decouples an IC (two pads across two nets that
    each reach at least three pads), how far its nearer pad is from a pad
    of the same net on an IC (a part of ``ic_pads`` pads or more). The
    wirelength cannot see this: a supply net's box spans the board, and a
    decoupling part anywhere inside it changes nothing."""
    pads_of: dict[str, list] = {}
    for p in board.pads:
        if p.comp:
            pads_of.setdefault(p.comp, []).append(p)
    by_net = board.pads_by_net()
    ics = {c for c, ps in pads_of.items() if len(ps) >= ic_pads}
    out = []
    for comp, ps in pads_of.items():
        if len(ps) != 2 or comp in ics:
            continue
        nets = [p.net for p in ps]
        if not all(nets) or nets[0] == nets[1] or min(len(by_net.get(n, [])) for n in nets) < 3:
            continue
        best = min((math.hypot(p.x - q.x, p.y - q.y) for p in ps for q in by_net[p.net]
                    if q.comp in ics), default=None)
        if best is not None:
            out.append(best)
    return out


def placement_score(board: LayoutBoard) -> dict:
    from . import geom

    edge = board.outline_shape()
    outside = [c.ref for c in board.components if len(c.courtyard) >= 3
               and not all(geom.point_in_poly(x, y, edge) for x, y in c.courtyard)]
    dec = sorted(decoupling_distances(board))
    return {"hpwl": round(hpwl(board), 1), "overlaps": len(overlaps(board)),
            "outside": len(outside),
            "decoupling_median": round(dec[len(dec) // 2], 1) if dec else None,
            "decoupling_within_100": round(sum(d <= 100.0 for d in dec) / len(dec), 3) if dec else None}

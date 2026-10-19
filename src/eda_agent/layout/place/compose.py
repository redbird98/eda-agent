# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Each block laid out on its own, in its own frame.

**Blocks with a head.** The head sits at the origin. Each member hangs
from one head pin and goes into a column on that pin's side of the head:
decoupling closest, then the other parts on that pin, then the parts
chained to them, further out. The columns of a side stand in pin order
on a lattice of one pitch, from one baseline, and the k-th parts of all
of a side's columns share a line, so a side reads as a grid. Each part is
turned so that its pad on the net to the head (or to the part before it)
faces the head. A part across two head pins (a crystal, a bootstrap
capacitor) lies along the side, between them. Where two sides' columns
meet at a corner, the side that needs the shorter push moves out.

**Passive blocks.** Rows in the order the parts are wired, wrapped back
and forth so the block is about half again as wide as it is tall, all
turned the same way, each two-pad part flipped where that brings its pads
nearer its neighbours'.

Parts are spaced by their outlines (body, and copper grown by half the
clearance, as the legaliser keeps them) plus ``gap``. Every part's origin
is on the ``grid`` lattice through the head's origin, so a block moved by
a lattice step, or turned a quarter, stays on it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..model import BOTTOM
from .blocks import ROLE_ORDER, Block, natural
from .placer import TURNS, _turn

Box = tuple[float, float, float, float]

_NORMAL = {"R": (1.0, 0.0), "L": (-1.0, 0.0), "T": (0.0, 1.0), "B": (0.0, -1.0)}
#: Width over height a passive block is wrapped to.
ROW_ASPECT = 1.5


def snap(v: float, grid: float) -> float:
    return round(v / grid) * grid


def _out(v: float, grid: float, sign: float) -> float:
    """``v`` to the lattice, away from the head (``sign`` its direction)."""
    k = v / grid
    return (math.ceil(k - 1e-9) if sign > 0 else math.floor(k + 1e-9)) * grid


def turn_point(x: float, y: float, o: float) -> tuple[float, float]:
    """A point turned a multiple of a quarter about the origin, exactly."""
    q = int(round(o / 90.0)) % 4
    return [(x, y), (-y, x), (-x, -y), (y, -x)][q]


@dataclass
class Layout:
    """Where a block's parts go in its frame: (x, y, turn) per part, the
    box their spaced outlines cover, and which group each part is in (the
    head, one side's columns, the rows): a group holds together when the
    legaliser has to move it."""

    poses: dict[str, tuple[float, float, float]]
    box: Box
    groups: dict[str, str] = field(default_factory=dict)

    def turned(self, o: float) -> "Layout":
        poses = {}
        for r, (x, y, rot) in self.poses.items():
            tx, ty = turn_point(x, y, o)
            poses[r] = (tx, ty, (rot + o) % 360.0)
        x0, y0, x1, y1 = self.box
        pts = [turn_point(x, y, o) for x, y in ((x0, y0), (x1, y1))]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return Layout(poses, (min(xs), min(ys), max(xs), max(ys)), dict(self.groups))

    @property
    def size(self) -> tuple[float, float]:
        return self.box[2] - self.box[0], self.box[3] - self.box[1]


class Shapes:
    """Each part's spaced outline and pins at a turn, from the placer's
    parts: bodies as they are, copper grown by half the clearance."""

    def __init__(self, placer):
        self.board = placer.board
        self.by_ref = placer.by_ref
        self.grow = placer.spacing / 2
        self._ext: dict = {}
        self._body: dict = {}

    def side(self, ref: str) -> str:
        return self.by_ref[ref].side

    def net(self, pad: int) -> str:
        return self.board.pads[pad].net

    def extent(self, ref: str, rot: float) -> Box:
        key = (ref, rot)
        if key not in self._ext:
            self._ext[key] = self._box(ref, rot, bodies_only=False)
        return self._ext[key]

    def body(self, ref: str, rot: float) -> Box:
        """The body alone, where the footprint has one: what a head's pins
        are judged against for their side."""
        key = (ref, rot)
        if key not in self._body:
            self._body[key] = self._box(ref, rot, bodies_only=True) or self.extent(ref, rot)
        return self._body[key]

    def _box(self, ref, rot, bodies_only):
        p = self.by_ref[ref]
        xs, ys = [], []
        for pl, kind in zip(p.keep, p.kinds):
            if bodies_only and kind != "body":
                continue
            g = self.grow if kind == "copper" else 0.0
            for x, y in _turn(pl, rot, p.side == BOTTOM):
                xs += [x - g, x + g]
                ys += [y - g, y + g]
        if not xs:
            return None if bodies_only else (0.0, 0.0, 0.0, 0.0)
        return (min(xs), min(ys), max(xs), max(ys))

    def pin(self, ref: str, pad: int, rot: float) -> tuple[float, float]:
        p = self.by_ref[ref]
        return _turn([p.pins[pad]], rot, p.side == BOTTOM)[0]

    def pins(self, ref: str) -> list[int]:
        return list(self.by_ref[ref].pins)


def compose(block: Block, sh: Shapes, grid: float, gap: float, head_rot: float = 0.0,
            sides: str = "RTLB") -> Layout:
    """The block's parts in its frame. ``head_rot`` is the head's turn
    (a fixed head's own); ``sides`` the head's sides its columns may use
    (a fixed connector's that face into the board)."""
    if block.head:
        poses, groups = _compose_head(block, sh, grid, gap, head_rot, sides)
    else:
        poses = _compose_rows(block, sh, grid, gap)
        groups = {r: "rows" for r in poses}
    return Layout(poses, _cover(poses, sh), groups)


def _cover(poses, sh: Shapes) -> Box:
    boxes = [_at(sh.extent(r, rot), x, y) for r, (x, y, rot) in poses.items()]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _at(e: Box, x: float, y: float) -> Box:
    return (e[0] + x, e[1] + y, e[2] + x, e[3] + y)


# -- blocks with a head ------------------------------------------------------

def _compose_head(block: Block, sh: Shapes, grid, gap, head_rot, sides):
    h = block.head
    poses = {h: (0.0, 0.0, head_rot)}
    ext = sh.extent(h, head_rot)
    bx0, by0, bx1, by1 = sh.body(h, head_rot)
    cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
    hw, hh = max((bx1 - bx0) / 2, 1.0), max((by1 - by0) / 2, 1.0)
    sides = _along_the_pins(sh, h, head_rot, sides)

    def side_of(pad: int) -> str:
        px, py = sh.pin(h, pad, head_rot)
        u, v = (px - cx) / hw, (py - cy) / hh
        score = {"R": u, "L": -u, "T": v, "B": -v}
        return max(sides, key=lambda s: (score[s], -"RTLB".index(s)))

    def along(pad: int, s: str) -> float:
        px, py = sh.pin(h, pad, head_rot)
        return py if s in "RL" else px

    # Columns, per board side and head side, keyed by the pin(s) they
    # hang from.
    groups: dict[tuple[str, str], dict[tuple[int, int], list]] = {}
    for m in sorted(block.members[1:], key=lambda m: (m.rank, ROLE_ORDER[m.role], natural(m.ref))):
        s = side_of(m.anchor) if m.anchor >= 0 else ("B" if "B" in sides else sides[0])
        groups.setdefault((sh.side(m.ref), s), {}).setdefault((m.anchor, m.anchor2), []).append(m)

    lay: dict[tuple[str, str], dict] = {}
    for (bside, s), cols in groups.items():
        items = []
        for (a, a2), members in cols.items():
            tau = 0.0 if a < 0 else along(a, s) if a2 < 0 else (along(a, s) + along(a2, s)) / 2
            parts = []
            for m in members:
                rot = _facing(m, s, sh, h, head_rot, along)
                parts.append((m.ref, rot, sh.extent(m.ref, rot)))
            items.append((tau, parts))
        items.sort(key=lambda it: it[0])
        lay[(bside, s)] = {"items": items, "offset": 0.0}

    edge = {"R": ext[2], "L": ext[0], "T": ext[3], "B": ext[1]}

    def place_all():
        out = {}
        for (bside, s), g in lay.items():
            out[(bside, s)] = _lay_side(g["items"], s, edge[s], g["offset"], grid, gap)
        return out

    placed = place_all()
    # Corners: two sides' columns can reach into the same corner. Push
    # one side out until no two parts of a board side are closer than gap.
    for _ in range(16):
        clash = _first_clash(placed, gap)
        if clash is None:
            break
        (ka, ba), (kb, bb) = clash
        push_a, push_b = _push(ka[1], ba, bb, gap), _push(kb[1], bb, ba, gap)
        if (push_a, -len(lay[ka]["items"])) <= (push_b, -len(lay[kb]["items"])):
            lay[ka]["offset"] += _out(push_a, grid, 1.0)
        else:
            lay[kb]["offset"] += _out(push_b, grid, 1.0)
        placed = place_all()
    groups = {h: "head"}
    for (bside, s), group in placed.items():
        for r, (x, y, rot, _) in group.items():
            poses[r] = (x, y, rot)
            groups[r] = f"{bside}:{s}"
    return poses, groups


#: Pins spread this many times further one way than the other stand in
#: rows (a header, a card edge): their parts go on the long sides only.
ROW_SPREAD = 3.0


def _along_the_pins(sh: Shapes, h: str, rot: float, sides: str) -> str:
    """The sides a head's columns may use, narrowed to the long sides when
    its pins stand in a row: on a pin header every pin is as near the
    short ends as the long sides, and its parts went to the two ends."""
    pts = [sh.pin(h, i, rot) for i in sh.pins(h)]
    if len(pts) < 2:
        return sides
    sx = max(p[0] for p in pts) - min(p[0] for p in pts)
    sy = max(p[1] for p in pts) - min(p[1] for p in pts)
    keep = "RL" if sy > ROW_SPREAD * sx else "TB" if sx > ROW_SPREAD * sy else "RTLB"
    narrowed = "".join(s for s in sides if s in keep)
    return narrowed or sides


def _facing(m, s: str, sh: Shapes, head: str, head_rot: float, along) -> float:
    """The member's turn: its pad on the net to its parent towards the
    head; a bridge along the side, each pad towards its own pin."""
    nx, ny = _NORMAL[s]
    tx, ty = (0.0, 1.0) if s in "RL" else (1.0, 0.0)
    pins = sh.pins(m.ref)
    best = None
    for rot in TURNS:
        pts = {i: sh.pin(m.ref, i, rot) for i in pins}
        if not pts:
            score = 0.0
        elif m.role == "bridge":
            na, nb = sh.net(m.anchor), sh.net(m.anchor2)
            a = [pts[i] for i in pins if sh.net(i) == na]
            b = [pts[i] for i in pins if sh.net(i) == nb]
            d = 1.0 if along(m.anchor, s) >= along(m.anchor2, s) else -1.0
            score = 0.0
            if a and b:
                (ax, ay), (bx, by) = _mean(a), _mean(b)
                score = d * ((ax - bx) * tx + (ay - by) * ty)
        else:
            inward = [pts[i] for i in pins if sh.net(i) == m.net]
            score = 0.0
            if inward:
                (ix, iy), (ox, oy) = _mean(inward), _mean(list(pts.values()))
                score = -((ix - ox) * nx + (iy - oy) * ny)
        e = sh.extent(m.ref, rot)
        width = (e[3] - e[1]) if s in "RL" else (e[2] - e[0])
        key = (round(score, 6), -round(width, 6), -TURNS.index(rot))
        if best is None or key > best[0]:
            best = (key, rot)
    return best[1]


def _mean(pts):
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


#: A part more than this many times the typical size of its side's parts
#: (a crystal laid along the side, an inductor among 0402s) takes as many
#: lattice slots as it needs and stands out past the lines: sized by it,
#: every column of the side stretched, and one block spanned a board.
OUTLIER = 2.0


def _typical(values) -> float:
    v = sorted(values)
    return v[len(v) // 2]


def _lay_side(items, s: str, edge: float, offset: float, grid: float, gap: float):
    """One side's columns: a lattice along the side, slots out from it.
    Returns ref -> (x, y, turn, box)."""
    sign = 1.0 if s in "RT" else -1.0
    vertical = s in "RL"         # columns stand along y, parts go out in x

    def tan_w(e):
        return (e[3] - e[1]) if vertical else (e[2] - e[0])

    def rad_w(e):
        return (e[2] - e[0]) if vertical else (e[3] - e[1])

    widths = [tan_w(e) for _, parts in items for _, _, e in parts]
    typical = _typical(widths)
    pitch = _out(max(w for w in widths if w <= OUTLIER * typical) + gap, grid, 1.0)
    spans = [max(1, math.ceil((max(tan_w(e) for _, _, e in parts) + gap) / pitch - 1e-9))
             for _, parts in items]
    centres, end = [], None
    for (tau, _), span in zip(items, spans):
        k = int(round(tau / pitch - (span - 1) / 2))
        if end is not None:
            k = max(k, end + 1)
        centres.append(k + (span - 1) / 2)
        end = k + span - 1
    shift = int(round(sum(tau / pitch - c for (tau, _), c in zip(items, centres)) / len(items)))
    centres = [c + shift for c in centres]
    # One line per rank, from the side's typical parts.
    depth = max(len(parts) for _, parts in items)
    lengths = []
    for j in range(depth):
        here = [rad_w(parts[j][2]) for _, parts in items if len(parts) > j]
        lengths.append(max(ln for ln in here if ln <= OUTLIER * _typical(here)))
    lines = []
    for j, ln in enumerate(lengths):
        if j == 0:
            v = edge + sign * (offset + gap + ln / 2)
        else:
            v = lines[-1] + sign * (lengths[j - 1] / 2 + gap + ln / 2)
        lines.append(_out(v, grid, sign))
    out = {}
    for (tau, parts), c in zip(items, centres):
        t = c * pitch
        inner = edge + sign * offset
        for j, (ref, rot, e) in enumerate(parts):
            ln = rad_w(e)
            v = lines[j]
            need = inner + sign * (gap + ln / 2)
            if (need - v) * sign > 1e-9:
                v = _out(need, grid, sign)      # a deep part: this column alone moves on
            inner = v + sign * ln / 2
            cx, cy = (v, t) if vertical else (t, v)
            x = snap(cx - (e[0] + e[2]) / 2, grid)
            y = snap(cy - (e[1] + e[3]) / 2, grid)
            out[ref] = (x, y, rot, _at(e, x, y))
    return out


def _first_clash(placed, gap):
    keys = list(placed)
    for i, ka in enumerate(keys):
        for kb in keys[i + 1:]:
            if ka[0] != kb[0]:
                continue        # other side of the board
            for ra, a in placed[ka].items():
                for rb, b in placed[kb].items():
                    if _closer(a[3], b[3], gap):
                        return (ka, a[3]), (kb, b[3])
    return None


def _closer(a: Box, b: Box, gap: float) -> bool:
    g = gap * 0.999
    return a[0] < b[2] + g and b[0] < a[2] + g and a[1] < b[3] + g and b[1] < a[3] + g


def _push(s: str, moving: Box, other: Box, gap: float) -> float:
    """How far side ``s``'s columns must move out to clear ``other``."""
    if s == "R":
        return max(other[2] + gap - moving[0], 0.0)
    if s == "L":
        return max(moving[2] - (other[0] - gap), 0.0)
    if s == "T":
        return max(other[3] + gap - moving[1], 0.0)
    return max(moving[3] - (other[1] - gap), 0.0)


# -- passive blocks ----------------------------------------------------------

def _compose_rows(block: Block, sh: Shapes, grid, gap):
    refs = _wiring_order(block.refs(), sh)
    rots = {r: _lengthwise(r, sh) for r in refs}
    n = len(refs)
    ext = {r: sh.extent(r, rots[r]) for r in refs}
    w = max(e[2] - e[0] for e in ext.values())
    hgt = max(e[3] - e[1] for e in ext.values())
    cols = max(1, min(n, int(round(math.sqrt(ROW_ASPECT * n * (hgt + gap) / max(w + gap, 1e-6))))))
    rows = (n + cols - 1) // cols
    cell = []
    for k in range(n):
        row, col = divmod(k, cols)
        if row % 2:
            col = cols - 1 - col      # back and forth: neighbours stay neighbours
        cell.append((row, col))
    colw = [0.0] * cols
    rowh = [0.0] * rows
    for r, (row, col) in zip(refs, cell):
        e = ext[r]
        colw[col] = max(colw[col], e[2] - e[0])
        rowh[row] = max(rowh[row], e[3] - e[1])
    xs, ys = [], []
    for c in range(cols):
        xs.append(0.0 if c == 0 else _out(xs[-1] + colw[c - 1] / 2 + gap + colw[c] / 2, grid, 1.0))
    for r in range(rows):
        ys.append(0.0 if r == 0 else _out(ys[-1] - rowh[r - 1] / 2 - gap - rowh[r] / 2, grid, -1.0))
    poses = {}
    for r, (row, col) in zip(refs, cell):
        e = ext[r]
        poses[r] = (snap(xs[col] - (e[0] + e[2]) / 2, grid), snap(ys[row] - (e[1] + e[3]) / 2, grid),
                    rots[r])
    # A two-pad part turned half round where that brings its pads nearer
    # the pads they connect to on its neighbours in the order.
    for k, r in enumerate(refs):
        if len(sh.pins(r)) != 2:
            continue
        near = [q for q in refs[max(0, k - 1):k + 2] if q != r]
        x, y, rot = poses[r]
        flip = (rot + 180.0) % 360.0
        if _link(r, x, y, flip, near, poses, sh) < _link(r, x, y, rot, near, poses, sh) - 1e-6:
            e0, e1 = sh.extent(r, rot), sh.extent(r, flip)
            # Keep the outline's centre where it was.
            dx = ((e0[0] + e0[2]) - (e1[0] + e1[2])) / 2
            dy = ((e0[1] + e0[3]) - (e1[1] + e1[3])) / 2
            poses[r] = (snap(x + dx, grid), snap(y + dy, grid), flip)
    return poses


def _lengthwise(ref: str, sh: Shapes) -> float:
    """The turn (0 or a quarter) that lays the part's pads along the row."""
    pins = sh.pins(ref)
    if len(pins) < 2:
        return 0.0
    best = None
    for rot in (0.0, 90.0):
        pts = [sh.pin(ref, i, rot) for i in pins]
        spread = (max(p[0] for p in pts) - min(p[0] for p in pts)) - \
            (max(p[1] for p in pts) - min(p[1] for p in pts))
        e = sh.extent(ref, rot)
        key = (round(spread, 6), -round(e[3] - e[1], 6), -rot)
        if best is None or key > best[0]:
            best = (key, rot)
    return best[1]


def _link(ref, x, y, rot, near, poses, sh: Shapes) -> float:
    total = 0.0
    for i in sh.pins(ref):
        n = sh.net(i)
        if not n:
            continue
        px, py = sh.pin(ref, i, rot)
        for q in near:
            qx, qy, qrot = poses[q]
            for j in sh.pins(q):
                if sh.net(j) == n:
                    ox, oy = sh.pin(q, j, qrot)
                    total += abs(x + px - qx - ox) + abs(y + py - qy - oy)
    return total


def _wiring_order(refs: list[str], sh: Shapes) -> list[str]:
    """The parts in an order that keeps wired parts next to each other:
    depth first from a part with the fewest neighbours, the most shared
    nets first."""
    nets = {r: {sh.net(i) for i in sh.pins(r) if sh.net(i)} for r in refs}
    big = _largest_nets(refs, nets)
    adj = {r: {} for r in refs}
    for i, a in enumerate(refs):
        for b in refs[i + 1:]:
            k = len((nets[a] & nets[b]) - big)
            if k:
                adj[a][b] = k
                adj[b][a] = k
    order, seen = [], set()
    while len(order) < len(refs):
        start = min((r for r in refs if r not in seen), key=lambda r: (len(adj[r]), natural(r)))
        stack = [start]
        while stack:
            r = stack.pop()
            if r in seen:
                continue
            seen.add(r)
            order.append(r)
            nxt = sorted((q for q in adj[r] if q not in seen), key=lambda q: (-adj[r][q], natural(q)))
            stack.extend(reversed(nxt))
    return order


def _largest_nets(refs, nets) -> set[str]:
    """Nets every part of a group shares (a rail they all sit on) say
    nothing about which part goes next to which."""
    if len(refs) < 3:
        return set()
    common = set.intersection(*(nets[r] for r in refs)) if refs else set()
    return common

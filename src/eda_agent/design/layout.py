# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba
"""Force-directed layout for the executor.

Topology-agnostic placement: build a spring system from ``plan.nets``,
let connected parts pull together and all parts push apart, iterate
to convergence, snap to a 100-mil grid, clamp to the A4 sheet.

No topology templates. No type-and-net classifier. The same algorithm
produces sensible placement for a buck, an LDO, an MCU board, an audio
amp, a sensor frontend — anything with a clean net graph.

Why force-directed: pairs of parts on the same net are pulled toward a
target separation; pairs of unconnected parts repel below a minimum
separation. Multi-pin ICs have higher mass so they move slowly and
become natural anchors. Power-in connectors are biased toward the
left edge of the sheet. Coordinates are mils; rotation is left at 0
(library-native orientation) — a follow-up pass can pick rotations
from per-pin direction once we read the placed bbox.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from eda_agent.design.plan import DesignPlan, Net, Part, Zone


# Sheet: A4 landscape, usable area starting from a small margin.
SHEET_ORIGIN_X_MILS = 1000
SHEET_ORIGIN_Y_MILS = 1000
SHEET_MAX_X_MILS = 10500
SHEET_MAX_Y_MILS = 7500

# Snap grid for final coordinates. Altium's default schematic snap is
# 100 mil; sticking to it keeps placed pins on Altium's electrical grid.
SNAP_GRID_MILS = 100

# Force-directed parameters. Tuned so connected parts settle ~2200 mils
# centre-to-centre and unconnected ones stay well separated on A4.
_SPRING_K = 0.05           # spring stiffness
_SPRING_REST_MILS = 1400   # ideal centre-to-centre for parts sharing a net
_REPEL_K = 6_000_000.0     # repulsion strength (Coulomb-style 1/r^2)
_REPEL_CUTOFF_MILS = 3000  # ignore repulsion beyond this distance
_EDGE_PULL_K = 0.35        # bias force toward the chosen sheet edge
_BOUNDARY_K = 0.25         # corrective force pulling parts back into the sheet
_DAMPING = 0.82
_DT = 1.0
_COOLING = 0.995
_MAX_ITERATIONS = 500

# Hard-shove (audit-aware second pass) parameters.
# The solver above is a local-minimum-seeker; on dense plans (e.g. the
# 14-part buck) the converged state still contains pairwise bbox
# overlaps. The shove pass below runs after convergence, audits every
# part pair against `_bbox_half(pin_count)`, and pushes overlapping
# pairs apart along their minimum-overlap axis until the audit is
# clean (or the cap is hit). This is what physical place-and-route
# tools do as a post-pass; spring relaxation alone is too soft at
# close range.
_SHOVE_MAX_ROUNDS = 50
# Extra clearance added on top of bbox-sum so the audit's own margin
# (OVERLAP_MARGIN_MILS == 25) doesn't immediately re-flag a kissing
# pair. A few mils of slop is cheaper than re-running the solver.
_SHOVE_CLEARANCE_MILS = 50
# Mass-weighted split: lighter mass moves more. The bias is capped so
# an extremely heavy IC against a very light passive still moves a
# little (avoids degenerate cases where one part is pinned and the
# other can't reach it because of a wall).
_SHOVE_IC_SPLIT = 0.8       # heavy part moves 20%, light part 80%
_SHOVE_EDGE_SPLIT = 0.8     # edge-biased part moves 20%, the other 80%


# Part-role hints map onto a preferred sheet edge for layout bias.
# These are generic conventions: power inputs land left, power outputs
# right, anything else is unbiased. Recognised on both ``Part.role``
# and ``Zone.role`` (which the planner can attach to a part via its
# zone field).
_EDGE_BIAS_BY_ROLE: dict[str, str] = {
    "power_in": "left",
    "vin": "left",
    "vin_conn": "left",
    "input": "left",
    "power_out": "right",
    "vout": "right",
    "vout_conn": "right",
    "output": "right",
}

# Conservative per-part bbox half-extents in mils. These intentionally
# overestimate the real symbol body so labels, stub wires, and ground /
# rail port glyphs that flank the part still have breathing room without
# colliding with neighbouring parts. The values include a typical
# stub-length + label-height pad.
_BBOX_HALF_2PIN_MILS = 450    # 2-pin passives — tight: body ~300, small stub margin
_BBOX_HALF_3PIN_MILS = 550
_BBOX_HALF_ICMIN_MILS = 800   # 4+ pin parts — body ~600, plus stubs both sides
_BBOX_HALF_ICBIG_MILS = 1200  # 16+ pin parts

# Reproducible jitter so the layout is deterministic across runs.
_RANDOM_SEED = 0x5EDA_A6EE


@dataclass(frozen=True)
class PlacedPart:
    """Computed placement for one part."""

    refdes: str
    sheet: str
    x_mils: int
    y_mils: int
    rotation: int = 0


def _pin_count_per_part(plan: DesignPlan) -> dict[str, int]:
    counts: dict[str, int] = {p.refdes: 0 for p in plan.parts}
    for net in plan.nets:
        for pin in net.pins:
            counts[pin.refdes] = counts.get(pin.refdes, 0) + 1
    return counts


def _rotation_for_part(part: Part, plan_nets: list[Net]) -> int:
    """Pick a rotation (0/90/180/270) from the part's net categories.

    Schematic conventions:
    - 2-pin part with at least one pin on a power or ground rail
      -> VERTICAL (90 deg CCW). Covers decoupling caps, pull-up /
      pull-down resistors, bulk caps, terminating resistors, etc.
    - 2-pin part with both pins on plain signal nets -> HORIZONTAL
      (rotation 0). Covers inline series resistors, AC-coupling caps.
    - Anything else (single-pin parts, ICs with 3+ pins, parts not
      participating in any net) -> rotation 0 (library-native).

    This rule uses ONLY net classification (is_power / is_ground), no
    part-type or refdes-prefix assumptions. It is therefore generic
    across topologies. The actual direction (power up vs power down)
    depends on which physical pin of the symbol the library puts
    where; a follow-up pass can flip 90 -> 270 after reading the
    placed pin coordinates if the polarity ends up inverted.
    """
    nets_for_part = [
        n for n in plan_nets if any(p.refdes == part.refdes for p in n.pins)
    ]
    pins_used: set[str] = set()
    for n in nets_for_part:
        for pin in n.pins:
            if pin.refdes == part.refdes:
                pins_used.add(pin.pin)
    if len(pins_used) != 2:
        return 0
    has_power = any(n.is_power for n in nets_for_part)
    has_ground = any(n.is_ground for n in nets_for_part)
    if has_power or has_ground:
        # 270 (CCW = 90 CW) is empirically correct for libraries whose
        # 2-pin parts have pin 1 on the RIGHT in native orientation
        # (e.g. the user's SELibrary). A library-aware pre-place pass
        # (query lib_get_pin_list, pick rotation based on which pin
        # ends up on top) is the generic fix; this is the interim.
        return 270
    return 0


def _bbox_half(pin_count: int) -> int:
    if pin_count >= 16:
        return _BBOX_HALF_ICBIG_MILS
    if pin_count >= 4:
        return _BBOX_HALF_ICMIN_MILS
    if pin_count == 3:
        return _BBOX_HALF_3PIN_MILS
    return _BBOX_HALF_2PIN_MILS


def _mass(pin_count: int) -> float:
    # ICs are anchors; passives drift around them. Mass scales with pin count.
    return max(1.0, pin_count / 2.0)


def _zone_role(plan: DesignPlan, zone_name: str | None) -> str | None:
    if not zone_name:
        return None
    for z in plan.zones:
        if z.name == zone_name:
            return z.role
    return None


def _edge_for_part(plan: DesignPlan, part: Part) -> str | None:
    """Pick a preferred sheet edge based on Part.role or its zone's role.

    Returns 'left', 'right', or None. Part.role wins over zone role
    when both are present.
    """
    role = (part.role or "").strip().lower()
    if role in _EDGE_BIAS_BY_ROLE:
        return _EDGE_BIAS_BY_ROLE[role]
    zone_role = (_zone_role(plan, part.zone) or "").strip().lower()
    return _EDGE_BIAS_BY_ROLE.get(zone_role)


def _net_members(net: Net) -> list[str]:
    seen: list[str] = []
    seen_set: set[str] = set()
    for pin in net.pins:
        if pin.refdes not in seen_set:
            seen.append(pin.refdes)
            seen_set.add(pin.refdes)
    return seen


def _connected_pairs(plan: DesignPlan) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for net in plan.nets:
        members = _net_members(net)
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                lo, hi = sorted((a, b))
                pairs.add((lo, hi))
    return pairs


def _zone_for_refdes(plan: DesignPlan) -> dict[str, str | None]:
    return {p.refdes: p.zone for p in plan.parts}


def _initial_positions(
    plan: DesignPlan,
    pin_count: dict[str, int],
) -> dict[str, list[float]]:
    """Sparse grid with a small jitter — converges fast and avoids
    degenerate co-located starting points."""
    rng = random.Random(_RANDOM_SEED)
    parts = list(plan.parts)
    n = len(parts)
    if n == 0:
        return {}
    cols = max(1, int(math.ceil(math.sqrt(n))))
    pitch_x = 1800
    pitch_y = 1500
    sheet_w = SHEET_MAX_X_MILS - SHEET_ORIGIN_X_MILS
    sheet_h = SHEET_MAX_Y_MILS - SHEET_ORIGIN_Y_MILS
    # Centre the grid roughly on the sheet.
    grid_w = (cols - 1) * pitch_x
    grid_h = ((n - 1) // cols) * pitch_y
    x0 = SHEET_ORIGIN_X_MILS + max(0, (sheet_w - grid_w) // 2)
    y0 = SHEET_ORIGIN_Y_MILS + max(0, (sheet_h - grid_h) // 2)

    pos: dict[str, list[float]] = {}
    # Anchor ICs (most pins) at the centre row first; then everything else.
    anchors = sorted(
        (p.refdes for p in parts if pin_count.get(p.refdes, 0) >= 4),
        key=lambda r: -pin_count[r],
    )
    others = [p.refdes for p in parts if p.refdes not in set(anchors)]

    placement_order = anchors + others
    for i, refdes in enumerate(placement_order):
        row = i // cols
        col = i % cols
        jx = rng.uniform(-40, 40)
        jy = rng.uniform(-40, 40)
        pos[refdes] = [
            x0 + col * pitch_x + jx,
            y0 + row * pitch_y + jy,
        ]
    return pos


def _force_directed_layout(plan: DesignPlan) -> list[PlacedPart]:
    """Run the spring/repulsion solver and return PlacedPart per part."""
    parts = list(plan.parts)
    if not parts:
        return []

    pin_count = _pin_count_per_part(plan)
    mass = {r: _mass(pin_count.get(r, 2)) for r in pin_count}
    bbox_half = {r: _bbox_half(pin_count.get(r, 2)) for r in pin_count}
    zone_for = _zone_for_refdes(plan)

    pos = _initial_positions(plan, pin_count)
    velocity = {r: [0.0, 0.0] for r in pos}

    # Pre-compute net-membership pairs (deduped).
    spring_pairs = _connected_pairs(plan)

    refdes_list = list(pos.keys())
    dt = _DT
    for _ in range(_MAX_ITERATIONS):
        forces = {r: [0.0, 0.0] for r in pos}

        # Spring attraction: every (a, b) pair that shares at least one net.
        for a, b in spring_pairs:
            if a not in pos or b not in pos:
                continue
            dx = pos[b][0] - pos[a][0]
            dy = pos[b][1] - pos[a][1]
            dist = math.hypot(dx, dy) + 1e-3
            disp = dist - _SPRING_REST_MILS
            f = disp * _SPRING_K
            ux = dx / dist
            uy = dy / dist
            forces[a][0] += f * ux
            forces[a][1] += f * uy
            forces[b][0] -= f * ux
            forces[b][1] -= f * uy

        # All-pairs repulsion, bbox-aware.
        n = len(refdes_list)
        for i in range(n):
            a = refdes_list[i]
            for j in range(i + 1, n):
                b = refdes_list[j]
                dx = pos[b][0] - pos[a][0]
                dy = pos[b][1] - pos[a][1]
                dist2 = dx * dx + dy * dy + 1.0
                if dist2 > _REPEL_CUTOFF_MILS * _REPEL_CUTOFF_MILS:
                    continue
                dist = math.sqrt(dist2)
                # Effective minimum separation = half-bbox of each + a margin.
                min_sep = bbox_half[a] + bbox_half[b] + 200
                if dist < min_sep:
                    # Hard push when overlapping.
                    f = (min_sep - dist) * 0.25
                else:
                    # Soft Coulomb falloff otherwise.
                    f = _REPEL_K / dist2
                ux = dx / dist
                uy = dy / dist
                forces[a][0] -= f * ux
                forces[a][1] -= f * uy
                forces[b][0] += f * ux
                forces[b][1] += f * uy

        # Edge bias for parts whose role (or zone role) points to an edge.
        for part in parts:
            edge = _edge_for_part(plan, part)
            if edge == "left":
                target_x = SHEET_ORIGIN_X_MILS + 500
                forces[part.refdes][0] += (target_x - pos[part.refdes][0]) * _EDGE_PULL_K
            elif edge == "right":
                target_x = SHEET_MAX_X_MILS - 500
                forces[part.refdes][0] += (target_x - pos[part.refdes][0]) * _EDGE_PULL_K

        # Boundary pull-back: parts drifting outside the sheet feel a
        # restorative force, proportional to how far out they are.
        for r in pos:
            x, y = pos[r]
            half = bbox_half[r]
            if x < SHEET_ORIGIN_X_MILS + half:
                forces[r][0] += (SHEET_ORIGIN_X_MILS + half - x) * _BOUNDARY_K
            elif x > SHEET_MAX_X_MILS - half:
                forces[r][0] -= (x - (SHEET_MAX_X_MILS - half)) * _BOUNDARY_K
            if y < SHEET_ORIGIN_Y_MILS + half:
                forces[r][1] += (SHEET_ORIGIN_Y_MILS + half - y) * _BOUNDARY_K
            elif y > SHEET_MAX_Y_MILS - half:
                forces[r][1] -= (y - (SHEET_MAX_Y_MILS - half)) * _BOUNDARY_K

        # Integrate.
        for r in pos:
            ax = forces[r][0] / mass[r]
            ay = forces[r][1] / mass[r]
            velocity[r][0] = (velocity[r][0] + ax * dt) * _DAMPING
            velocity[r][1] = (velocity[r][1] + ay * dt) * _DAMPING
            pos[r][0] += velocity[r][0] * dt
            pos[r][1] += velocity[r][1] * dt

        dt *= _COOLING

    # Snap, clamp, and assign rotation based on net categories.
    placed: list[PlacedPart] = []
    for part in parts:
        x = pos[part.refdes][0]
        y = pos[part.refdes][1]
        x = int(round(x / SNAP_GRID_MILS) * SNAP_GRID_MILS)
        y = int(round(y / SNAP_GRID_MILS) * SNAP_GRID_MILS)
        half = bbox_half[part.refdes]
        x = max(SHEET_ORIGIN_X_MILS + half, min(SHEET_MAX_X_MILS - half, x))
        y = max(SHEET_ORIGIN_Y_MILS + half, min(SHEET_MAX_Y_MILS - half, y))
        rotation = _rotation_for_part(part, plan.nets)
        placed.append(
            PlacedPart(
                refdes=part.refdes,
                sheet=part.sheet,
                x_mils=x,
                y_mils=y,
                rotation=rotation,
            )
        )
    return placed


def _overlap_pair(
    ax: float,
    ay: float,
    bx: float,
    by: float,
    half_a: int,
    half_b: int,
    clearance: int,
) -> tuple[float, float] | None:
    """Return the (dx, dy) overlap-along-axis distances, or None.

    AABB overlap test. Each axis returns ``(ha + hb + clearance) - |delta|``;
    if either axis returns <= 0 the parts do not overlap and ``None`` is
    returned. When both are positive the smaller of the two indicates
    the cheapest direction to separate.
    """
    needed = half_a + half_b + clearance
    overlap_x = needed - abs(ax - bx)
    overlap_y = needed - abs(ay - by)
    if overlap_x <= 0 or overlap_y <= 0:
        return None
    return overlap_x, overlap_y


def _shove_split(
    plan: DesignPlan,
    part_by_refdes: dict[str, Part],
    mass: dict[str, float],
    pin_count: dict[str, int],
    a: str,
    b: str,
) -> tuple[float, float]:
    """Pick the (frac_a, frac_b) split for a shove between two parts.

    ``frac_a + frac_b == 1.0``. The part that should move LESS gets the
    smaller fraction. Priority order:

    1. If exactly one side is edge-biased (``power_in`` / ``power_out``
       via Part.role or zone), it stays put — the other side absorbs
       80% of the push.
    2. Else if exactly one side is an IC (4+ pins), the IC absorbs 20%
       of the push.
    3. Else split inversely by mass so lighter parts drift more, but
       guarantee both sides move at least 30% so we don't stall.
    """
    is_edge_a = _edge_for_part(plan, part_by_refdes[a]) is not None
    is_edge_b = _edge_for_part(plan, part_by_refdes[b]) is not None
    if is_edge_a and not is_edge_b:
        return (1.0 - _SHOVE_EDGE_SPLIT, _SHOVE_EDGE_SPLIT)
    if is_edge_b and not is_edge_a:
        return (_SHOVE_EDGE_SPLIT, 1.0 - _SHOVE_EDGE_SPLIT)

    pin_a = pin_count.get(a, 2)
    pin_b = pin_count.get(b, 2)
    a_is_ic = pin_a >= 4
    b_is_ic = pin_b >= 4
    if a_is_ic and not b_is_ic:
        return (1.0 - _SHOVE_IC_SPLIT, _SHOVE_IC_SPLIT)
    if b_is_ic and not a_is_ic:
        return (_SHOVE_IC_SPLIT, 1.0 - _SHOVE_IC_SPLIT)

    # Both same class. Inverse-mass weighting, clamped to [0.3, 0.7] so
    # we always make progress on both sides.
    total_inv = (1.0 / mass[a]) + (1.0 / mass[b])
    frac_a = (1.0 / mass[a]) / total_inv  # heavier => smaller fraction
    # frac_a is the share the OTHER side (b) feels — we want a's own
    # movement to be inversely-mass-weighted, i.e. light moves more.
    # The way the caller applies frac_a is "a moves frac_a of the push";
    # so light => big frac. (1/m_a) / total_inv already gives that.
    frac_a = max(0.3, min(0.7, frac_a))
    return (frac_a, 1.0 - frac_a)


def _hard_shove_pass(
    plan: DesignPlan,
    placed: list[PlacedPart],
) -> tuple[list[PlacedPart], int]:
    """Audit-aware deterministic shove.

    Detects pairwise bbox overlaps against ``_bbox_half(pin_count)`` and
    pushes each overlapping pair apart along the cheaper axis until the
    audit is clean or ``_SHOVE_MAX_ROUNDS`` is reached. Splits the push
    by mass / role (see :func:`_shove_split`). Respects sheet bounds:
    if one side would breach a wall, the other side absorbs the full
    push instead.

    Returns the new placement list and the residual overlap count
    after the final round (0 means clean).
    """
    if len(placed) < 2:
        return list(placed), 0

    pin_count = _pin_count_per_part(plan)
    mass = {r: _mass(pin_count.get(r, 2)) for r in pin_count}
    bbox_half = {r: _bbox_half(pin_count.get(r, 2)) for r in pin_count}
    part_by_refdes = {p.refdes: p for p in plan.parts}

    # Mutable float positions per refdes, keyed off the placed list so
    # we preserve sheet/rotation untouched.
    pos: dict[str, list[float]] = {
        p.refdes: [float(p.x_mils), float(p.y_mils)] for p in placed
    }
    sheet_of = {p.refdes: p.sheet for p in placed}
    rot_of = {p.refdes: p.rotation for p in placed}

    refdes_list = [p.refdes for p in placed]
    residual = 0
    for _ in range(_SHOVE_MAX_ROUNDS):
        overlaps_this_round = 0
        # Accumulate displacement per part this round, apply at end.
        # Gauss-Seidel (apply-as-we-go) oscillates when one part
        # participates in many overlaps with neighbours on opposite
        # sides — heavy ICs in particular hit this in the buck plan.
        # Jacobi-style accumulation (collect all displacements from
        # the current frozen positions, then sum and apply) converges
        # monotonically.
        delta: dict[str, list[float]] = {r: [0.0, 0.0] for r in pos}
        for i in range(len(refdes_list)):
            a = refdes_list[i]
            for j in range(i + 1, len(refdes_list)):
                b = refdes_list[j]
                if sheet_of[a] != sheet_of[b]:
                    continue
                ax, ay = pos[a]
                bx, by = pos[b]
                ovl = _overlap_pair(
                    ax,
                    ay,
                    bx,
                    by,
                    bbox_half[a],
                    bbox_half[b],
                    _SHOVE_CLEARANCE_MILS,
                )
                if ovl is None:
                    continue
                overlaps_this_round += 1
                ox, oy = ovl

                # Two-axis Jacobi push: contribute on BOTH axes
                # proportional to that axis' overlap, but scaled by a
                # damping factor so simultaneous opposing pushes from
                # different pair partners average out instead of
                # oscillating. The cheaper axis still dominates because
                # its overlap is the limiting one, but the perpendicular
                # axis gets a smaller nudge — this is what lets two
                # parts that meet on a corner slide apart diagonally
                # rather than fighting along a single axis.
                frac_a, frac_b = _shove_split(
                    plan, part_by_refdes, mass, pin_count, a, b
                )
                for axis, push in ((0, ox), (1, oy)):
                    if push <= 0:
                        continue
                    if axis == 0:
                        sign_v = 1.0 if (bx - ax) >= 0 else -1.0
                        if (bx - ax) == 0:
                            sign_v = 1.0 if a < b else -1.0
                    else:
                        sign_v = 1.0 if (by - ay) >= 0 else -1.0
                        if (by - ay) == 0:
                            sign_v = 1.0 if a < b else -1.0
                    # Weight: full push on the cheaper axis, half push
                    # on the more-expensive one. Damping per axis = 0.5.
                    weight = 1.0 if push == min(ox, oy) else 0.5
                    # Wall-aware split (per-axis).
                    lo_a = (SHEET_ORIGIN_X_MILS if axis == 0 else SHEET_ORIGIN_Y_MILS) + bbox_half[a]
                    hi_a = (SHEET_MAX_X_MILS if axis == 0 else SHEET_MAX_Y_MILS) - bbox_half[a]
                    lo_b = (SHEET_ORIGIN_X_MILS if axis == 0 else SHEET_ORIGIN_Y_MILS) + bbox_half[b]
                    hi_b = (SHEET_MAX_X_MILS if axis == 0 else SHEET_MAX_Y_MILS) - bbox_half[b]
                    a_blocked = (
                        pos[a][axis] <= lo_a + 0.5
                        if sign_v > 0
                        else pos[a][axis] >= hi_a - 0.5
                    )
                    b_blocked = (
                        pos[b][axis] >= hi_b - 0.5
                        if sign_v > 0
                        else pos[b][axis] <= lo_b + 0.5
                    )
                    fa, fb = frac_a, frac_b
                    if a_blocked and not b_blocked:
                        fa, fb = 0.0, 1.0
                    elif b_blocked and not a_blocked:
                        fa, fb = 1.0, 0.0
                    elif a_blocked and b_blocked:
                        continue  # axis stuck; skip
                    delta[a][axis] += -sign_v * push * fa * weight
                    delta[b][axis] += sign_v * push * fb * weight

        # Apply accumulated displacements, then walls. If a part hits a
        # wall, the displacement that would have crossed it is
        # discarded — we cannot retroactively redistribute to all the
        # partners that contributed without re-running the pass. The
        # next round will pick up any remaining overlap.
        for r in pos:
            for axis in (0, 1):
                lo = (SHEET_ORIGIN_X_MILS if axis == 0 else SHEET_ORIGIN_Y_MILS) + bbox_half[r]
                hi = (SHEET_MAX_X_MILS if axis == 0 else SHEET_MAX_Y_MILS) - bbox_half[r]
                new = pos[r][axis] + delta[r][axis]
                pos[r][axis] = max(lo, min(hi, new))

        residual = overlaps_this_round
        if overlaps_this_round == 0:
            break

    # Snap + clamp, then run a final integer-grid shove sweep so the
    # ±SNAP_GRID/2 snap displacement can't reintroduce a borderline
    # overlap that the float-space loop just resolved.
    snapped: dict[str, list[int]] = {}
    for r in pos:
        half = bbox_half[r]
        x = int(round(pos[r][0] / SNAP_GRID_MILS) * SNAP_GRID_MILS)
        y = int(round(pos[r][1] / SNAP_GRID_MILS) * SNAP_GRID_MILS)
        x = max(SHEET_ORIGIN_X_MILS + half, min(SHEET_MAX_X_MILS - half, x))
        y = max(SHEET_ORIGIN_Y_MILS + half, min(SHEET_MAX_Y_MILS - half, y))
        snapped[r] = [x, y]

    # Post-snap integer-grid shove. Moves in SNAP_GRID_MILS increments
    # so the result stays on the grid. Uses Jacobi accumulation
    # (collect-then-apply) for the same reason as the float pass.
    for _ in range(_SHOVE_MAX_ROUNDS):
        any_overlap = False
        idelta: dict[str, list[int]] = {r: [0, 0] for r in snapped}
        for i in range(len(refdes_list)):
            a = refdes_list[i]
            for j in range(i + 1, len(refdes_list)):
                b = refdes_list[j]
                if sheet_of[a] != sheet_of[b]:
                    continue
                ax, ay = snapped[a]
                bx, by = snapped[b]
                # Use 0 clearance here: kissing on the grid is OK, the
                # solver's _SHOVE_CLEARANCE_MILS is already baked into
                # the float pass. We only want to break true overlap.
                if abs(ax - bx) < (bbox_half[a] + bbox_half[b]) and abs(ay - by) < (bbox_half[a] + bbox_half[b]):
                    any_overlap = True
                    needed = bbox_half[a] + bbox_half[b]
                    ox = needed - abs(ax - bx)
                    oy = needed - abs(ay - by)
                    frac_a, frac_b = _shove_split(
                        plan, part_by_refdes, mass, pin_count, a, b
                    )
                    cheaper = min(ox, oy)
                    for axis, push in ((0, ox), (1, oy)):
                        if push <= 0:
                            continue
                        if axis == 0:
                            sign_v = 1 if (bx - ax) >= 0 else -1
                            if (bx - ax) == 0:
                                sign_v = 1 if a < b else -1
                        else:
                            sign_v = 1 if (by - ay) >= 0 else -1
                            if (by - ay) == 0:
                                sign_v = 1 if a < b else -1
                        # Snap-pad and damp the perpendicular axis.
                        push_grid = int(math.ceil(push / SNAP_GRID_MILS) * SNAP_GRID_MILS)
                        if push != cheaper:
                            push_grid = max(0, push_grid // 2)
                            push_grid = int(math.ceil(push_grid / SNAP_GRID_MILS) * SNAP_GRID_MILS)
                        if push_grid <= 0:
                            continue
                        lo_a_i = (SHEET_ORIGIN_X_MILS if axis == 0 else SHEET_ORIGIN_Y_MILS) + bbox_half[a]
                        hi_a_i = (SHEET_MAX_X_MILS if axis == 0 else SHEET_MAX_Y_MILS) - bbox_half[a]
                        lo_b_i = (SHEET_ORIGIN_X_MILS if axis == 0 else SHEET_ORIGIN_Y_MILS) + bbox_half[b]
                        hi_b_i = (SHEET_MAX_X_MILS if axis == 0 else SHEET_MAX_Y_MILS) - bbox_half[b]
                        a_blk = snapped[a][axis] <= lo_a_i if sign_v > 0 else snapped[a][axis] >= hi_a_i
                        b_blk = snapped[b][axis] >= hi_b_i if sign_v > 0 else snapped[b][axis] <= lo_b_i
                        fa, fb = frac_a, frac_b
                        if a_blk and not b_blk:
                            fa, fb = 0.0, 1.0
                        elif b_blk and not a_blk:
                            fa, fb = 1.0, 0.0
                        elif a_blk and b_blk:
                            continue
                        move_b = int(math.ceil(push_grid * fb / SNAP_GRID_MILS) * SNAP_GRID_MILS)
                        move_a = max(0, push_grid - move_b)
                        move_a = int(math.ceil(move_a / SNAP_GRID_MILS) * SNAP_GRID_MILS)
                        idelta[a][axis] += -sign_v * move_a
                        idelta[b][axis] += sign_v * move_b
        # Apply with per-axis clamp; off-grid wall corrections re-snap.
        for r in snapped:
            for axis in (0, 1):
                lo = (SHEET_ORIGIN_X_MILS if axis == 0 else SHEET_ORIGIN_Y_MILS) + bbox_half[r]
                hi = (SHEET_MAX_X_MILS if axis == 0 else SHEET_MAX_Y_MILS) - bbox_half[r]
                new = snapped[r][axis] + idelta[r][axis]
                new = max(lo, min(hi, new))
                snapped[r][axis] = int(round(new / SNAP_GRID_MILS) * SNAP_GRID_MILS)
        if not any_overlap:
            residual = 0
            break

    out: list[PlacedPart] = []
    for p in placed:
        r = p.refdes
        x, y = snapped[r]
        out.append(
            PlacedPart(
                refdes=r,
                sheet=sheet_of[r],
                x_mils=x,
                y_mils=y,
                rotation=rot_of[r],
            )
        )
    # Final residual count (audit-style) for the caller.
    residual = sum(
        1
        for i in range(len(out))
        for j in range(i + 1, len(out))
        if out[i].sheet == out[j].sheet
        and abs(out[i].x_mils - out[j].x_mils) < (bbox_half[out[i].refdes] + bbox_half[out[j].refdes])
        and abs(out[i].y_mils - out[j].y_mils) < (bbox_half[out[i].refdes] + bbox_half[out[j].refdes])
    )
    return out, residual


def audit_overlaps(
    plan: DesignPlan,
    placed: list[PlacedPart],
) -> list[tuple[str, str]]:
    """Return overlapping (refdes_a, refdes_b) pairs using solver bboxes.

    Public helper used by tests and (optionally) the executor to verify
    the post-shove layout is clean. Uses the SAME ``_bbox_half`` sizing
    the force-directed solver and shove pass use, so a non-empty result
    here is the definitive failure indicator.
    """
    pin_count = _pin_count_per_part(plan)
    bbox_half = {r: _bbox_half(pin_count.get(r, 2)) for r in pin_count}
    out: list[tuple[str, str]] = []
    for i in range(len(placed)):
        for j in range(i + 1, len(placed)):
            a = placed[i]
            b = placed[j]
            if a.sheet != b.sheet:
                continue
            ha = bbox_half[a.refdes]
            hb = bbox_half[b.refdes]
            # Strict inequality: a kissing pair (delta == sum) is NOT
            # an overlap. Mirrors the audit-side semantics where touch
            # alone is acceptable post-grid-snap.
            if abs(a.x_mils - b.x_mils) < (ha + hb) and abs(a.y_mils - b.y_mils) < (ha + hb):
                out.append((a.refdes, b.refdes))
    return out


def compute_layout(plan: DesignPlan) -> list[PlacedPart]:
    """Force-directed (x, y) for every part in the plan.

    Builds a spring system from ``plan.nets`` and iterates to convergence.
    Connected parts pull together, unconnected parts repel, ICs are heavy
    anchors, ``power_in`` connectors are biased toward the left edge.
    After the solver converges a deterministic hard-shove pass runs as
    a second stage: it audits all pairs against the same bbox sizes the
    solver uses and pushes overlapping pairs apart until the audit is
    clean. Output is snapped to a 100-mil grid and clamped to the A4
    sheet. Rotation stays at 0 (library-native).
    """
    placed = _force_directed_layout(plan)
    cleaned, _residual = _hard_shove_pass(plan, placed)
    return cleaned

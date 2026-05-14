# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba
"""Executor, instantiate a DesignPlan in Altium.

Slice B.1 scope: parts placement. Opens or creates the project, creates
SchDoc(s) for each plan.sheet, places every existing-lib part at a
grid-computed position, saves. Returns a structured result.

Wiring (net labels at pin coordinates) is Slice B.2, needs a new Pascal
helper to look up pin world coords on a placed component instance.

The executor is mechanical: it consumes a validated plan, calls existing
MCP-style commands via the bridge, and reports outcomes. It does not
reason. Reasoning belongs to the planner (Claude Code).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from eda_agent.design.layout import PlacedPart, compute_layout
from eda_agent.design.plan import DesignPlan, Net, Part, PartStatus, PinRef

logger = logging.getLogger("eda_agent.design.executor")


@dataclass
class PartFailure:
    """One part the executor could not place."""

    refdes: str
    reason: str
    code: str


# First-time loading a SchLib in Altium (especially a 400-component one) can
# take 20-30s. Bump the per-call budget so the executor doesn't time out on
# the first place. Subsequent calls are fast because the lib stays cached.
_PLACE_TIMEOUT_S = 60.0
_LABEL_TIMEOUT_S = 30.0
_SAVE_TIMEOUT_S = 60.0
_PARAM_STAMP_TIMEOUT_S = 30.0


@dataclass
class ExecutorResult:
    """What ``execute_plan`` returns."""

    ok: bool = True
    project_path: str = ""
    sheets_touched: list[str] = field(default_factory=list)
    placed: list[PlacedPart] = field(default_factory=list)
    failures: list[PartFailure] = field(default_factory=list)
    needs_creation: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    nets_labelled: int = 0
    power_ports_placed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "project_path": self.project_path,
            "sheets_touched": list(self.sheets_touched),
            "placed": [
                {
                    "refdes": p.refdes,
                    "sheet": p.sheet,
                    "x_mils": p.x_mils,
                    "y_mils": p.y_mils,
                    "rotation": p.rotation,
                }
                for p in self.placed
            ],
            "failures": [
                {"refdes": f.refdes, "reason": f.reason, "code": f.code}
                for f in self.failures
            ],
            "needs_creation": list(self.needs_creation),
            "notes": list(self.notes),
            "nets_labelled": self.nets_labelled,
            "power_ports_placed": self.power_ports_placed,
        }


def _sheet_path(project_path: Path, sheet_name: str) -> Path:
    """Default SchDoc path for a plan-declared sheet."""
    return project_path.parent / f"{sheet_name}.SchDoc"


def _bom_lookup(plan: DesignPlan) -> dict[str, tuple[Optional[str], Optional[str]]]:
    """Map refdes -> (manufacturer, mpn) drawn from plan.bom for fallback.

    Built once per execution so the per-part lookup is O(1). Multiple BomLines
    can reference the same refdes only if there's a planner bug; first match
    wins.
    """
    lookup: dict[str, tuple[Optional[str], Optional[str]]] = {}
    for line in plan.bom:
        for refdes in line.refdes_list:
            lookup.setdefault(refdes, (line.manufacturer, line.mpn))
    return lookup


def _part_parameters(
    part: Part,
    bom_lookup: dict[str, tuple[Optional[str], Optional[str]]],
) -> dict[str, str]:
    """Build the parameter sub-object the Pascal handler will stamp.

    Resolution order for Manufacturer / MPN: Part fields win over BomLine
    fallback. Empty values are stripped so the Pascal side has nothing to
    skip and the IPC payload stays compact.
    """
    bom_mfr, bom_mpn = bom_lookup.get(part.refdes, (None, None))
    mfr = part.manufacturer or bom_mfr
    mpn = part.mpn or bom_mpn

    candidate: dict[str, Optional[str]] = {
        "Value": part.value,
        "Manufacturer": mfr,
        "Manufacturer Part Number": mpn,
        "Footprint": part.footprint,
    }
    return {k: v for k, v in candidate.items() if v}


def execute_plan(plan: DesignPlan, project_path: str, *, bridge: Optional[Any] = None) -> ExecutorResult:
    """Instantiate the plan in Altium.

    Args:
        plan: A validated DesignPlan (caller is responsible for validation;
            this function re-runs cross_check defensively).
        project_path: Absolute path to the .PrjPcb (created if absent).
        bridge: Optional bridge to use; defaults to the global one. Tests
            inject a fake to avoid Altium round-trips.

    Returns:
        ExecutorResult, ok=True iff all existing-lib parts placed and
        no needs_creation parts were present.
    """
    result = ExecutorResult(project_path=project_path)

    # Defensive cross-check (caller should have run it, but cheap to repeat).
    cross = plan.cross_check()
    if cross:
        result.ok = False
        result.notes.extend(cross)
        return result

    # Halt early on needs_creation parts, escalate to caller, do not place
    # partials that would mislead a reviewer about completeness.
    needs_creation = [p.refdes for p in plan.parts if p.status == PartStatus.NEEDS_CREATION]
    if needs_creation:
        result.ok = False
        result.needs_creation = needs_creation
        result.notes.append(
            "Plan contains needs_creation parts; refusing to instantiate a "
            "partial design. Resolve those parts (pick existing or author "
            "a new symbol) and re-run."
        )
        return result

    if bridge is None:
        from eda_agent.bridge import get_bridge  # late import, needs Altium
        bridge = get_bridge()

    project = Path(project_path).expanduser().resolve()

    # Open or create the project.
    if project.exists():
        try:
            bridge.send_command("project.open", {"project_path": str(project)})
            result.notes.append(f"Opened existing project: {project}")
        except Exception as exc:
            result.ok = False
            result.notes.append(f"project.open failed: {exc}")
            return result
    else:
        try:
            bridge.send_command(
                "project.create",
                {"project_path": str(project), "project_type": "PCB"},
            )
            result.notes.append(f"Created project: {project}")
        except Exception as exc:
            result.ok = False
            result.notes.append(f"project.create failed: {exc}")
            return result

    # Ensure each sheet exists. Skip if already present (project.open will have
    # picked up sheets attached to the project file).
    sheets_to_create: list[tuple[str, Path]] = []
    for sheet in plan.sheets:
        target = _sheet_path(project, sheet.name)
        sheets_to_create.append((sheet.name, target))

    for sheet_name, sheet_path in sheets_to_create:
        if sheet_path.exists():
            # Sheet on disk, make sure it is loaded into the editor so
            # set_active_document can target it. project.open does not
            # always pull child sheets into the editor.
            try:
                bridge.send_command(
                    "application.run_process",
                    {
                        "process_name": "WorkspaceManager:OpenObject",
                        "parameters": (
                            "ObjectKind=Document|FileName=" + str(sheet_path)
                        ),
                    },
                )
                result.notes.append(f"Sheet loaded: {sheet_path}")
            except Exception as exc:
                result.ok = False
                result.notes.append(f"OpenObject failed for {sheet_name}: {exc}")
                return result
            continue
        try:
            bridge.send_command(
                "application.create_document",
                {
                    "kind": "SCH",
                    "file_path": str(sheet_path),
                    "name": sheet_name,
                    "add_to_project": "true",
                },
            )
            result.notes.append(f"Created sheet: {sheet_path}")
        except Exception as exc:
            result.ok = False
            result.notes.append(f"create_document failed for {sheet_name}: {exc}")
            return result

    # Compute placement for every part.
    placements = compute_layout(plan)
    placement_by_refdes = {p.refdes: p for p in placements}

    bom_lookup = _bom_lookup(plan)

    # Group parts by sheet so we set the active document once per sheet.
    parts_by_sheet: dict[str, list[Part]] = {}
    for p in plan.parts:
        parts_by_sheet.setdefault(p.sheet, []).append(p)

    for sheet_name, parts in parts_by_sheet.items():
        sheet_path = _sheet_path(project, sheet_name)
        try:
            bridge.send_command(
                "application.set_active_document", {"file_path": str(sheet_path)}
            )
            result.sheets_touched.append(sheet_name)
        except Exception as exc:
            for part in parts:
                result.failures.append(
                    PartFailure(
                        refdes=part.refdes,
                        reason=f"could not activate sheet {sheet_name}: {exc}",
                        code="SHEET_ACTIVATE_FAILED",
                    )
                )
            continue

        # Bulk-place every part for this sheet in a single IPC call.
        # generic.place_sch_components_from_library wraps the whole batch
        # in one PreProcess/PostProcess + one GraphicallyInvalidate, which
        # is 10-100x faster than looping the singular variant.
        placement_ops: list[str] = []
        parts_to_place: list[Part] = []
        for part in parts:
            placement = placement_by_refdes.get(part.refdes)
            if placement is None:
                result.failures.append(
                    PartFailure(
                        refdes=part.refdes,
                        reason="layout did not produce a placement",
                        code="LAYOUT_GAP",
                    )
                )
                continue
            placement_ops.append(
                f"library_path={part.lib_path or ''};"
                f"lib_reference={part.lib_ref};"
                f"x={placement.x_mils};"
                f"y={placement.y_mils};"
                f"designator={part.refdes};"
                f"rotation={placement.rotation};"
                f"footprint={part.footprint or ''}"
            )
            parts_to_place.append(part)

        if placement_ops:
            try:
                bridge.send_command(
                    "generic.place_sch_components_from_library",
                    {"placements": "~~".join(placement_ops)},
                    timeout=_PLACE_TIMEOUT_S * max(1, len(placement_ops) // 4),
                )
                for part in parts_to_place:
                    result.placed.append(placement_by_refdes[part.refdes])
            except Exception as exc:
                for part in parts_to_place:
                    result.failures.append(
                        PartFailure(
                            refdes=part.refdes,
                            reason=f"bulk place failed: {exc}",
                            code="PLACE_FAILED",
                        )
                    )
                continue

        # Bulk-stamp Value / Manufacturer / MPN / Footprint on every just-placed
        # part. Bulk handler iterates the sheet once, applies all params under
        # ONE PreProcess/PostProcess. Saves N-1 IPC round trips.
        stamp_ops: list[str] = []
        for part in parts_to_place:
            stamp_payload = _part_parameters(part, bom_lookup)
            if not stamp_payload:
                continue
            fields = [f"designator={part.refdes}"]
            for k, v in stamp_payload.items():
                if not k or v is None:
                    continue
                vs = str(v).strip()
                if not vs:
                    continue
                # The batch field separator is ';' so we strip stray ones from
                # values to keep the encoding safe. Values with '=' are fine
                # because GetBatchField splits each field at the FIRST '='.
                fields.append(f"{k}={vs.replace(';', ',')}")
            if len(fields) > 1:
                stamp_ops.append(";".join(fields))

        if stamp_ops:
            try:
                bridge.send_command(
                    "generic.set_sch_components_parameters",
                    {
                        "stamps": "~~".join(stamp_ops),
                        "sheet_path": str(_sheet_path(project, sheet_name)),
                    },
                    timeout=_PARAM_STAMP_TIMEOUT_S * max(1, len(stamp_ops) // 8),
                )
            except Exception as exc:
                result.notes.append(
                    f"bulk param-stamp for sheet {sheet_name} failed: {exc}"
                )

    # Wiring stage, drop a net label at every plan-defined pin endpoint.
    # Power and ground nets get power ports instead of plain labels.
    placed_refdes = {p.refdes for p in result.placed}
    if placed_refdes:
        _place_net_labels(
            plan, placed_refdes, parts_by_sheet, project, bridge, result,
            placement_by_refdes=placement_by_refdes,
        )

    # Save everything we touched.
    try:
        bridge.send_command("application.save_all", {}, timeout=_SAVE_TIMEOUT_S)
        result.notes.append("save_all completed")
    except Exception as exc:
        result.notes.append(f"save_all failed: {exc}")
        result.ok = False

    if result.failures:
        result.ok = False

    return result


def _ground_style(net_name: str) -> str:
    """Pick a power-port style for an is_ground net based on its name.

    Altium has separate gnd_power / gnd_signal / gnd_earth glyphs. When the
    net name carries a hint we honour it; otherwise default to gnd_power.
    """
    upper = net_name.upper()
    if "EARTH" in upper or upper == "PE":
        return "gnd_earth"
    if "AGND" in upper or "ANALOG" in upper or upper == "AGND":
        return "gnd_signal"
    return "gnd_power"


# 100-mil stub wire length between a pin's electrical hot end and the net
# label / power port that attaches to it. ERC reports "Floating net labels"
# when a label sits exactly on a pin endpoint without an intervening wire,
# so every label/port gets pulled out along the pin's vector by this much.
_STUB_LEN_MILS = 300


def _pin_direction_vector(orientation: int) -> tuple[int, int]:
    """Map Altium's TRotationBy90 pin orientation to a unit vector.

    0=right (+x), 1=up (+y), 2=left (-x), 3=down (-y). Matches what
    Pascal's ``Gen_GetSchComponentPins`` returns from ``Pin.Orientation``.
    Unknown values fall through as (1, 0) so the stub still draws.
    """
    if orientation == 1:
        return (0, 1)
    if orientation == 2:
        return (-1, 0)
    if orientation == 3:
        return (0, -1)
    return (1, 0)


def _stub_endpoints(
    pin_x: int,
    pin_y: int,
    orientation: int,
    pin_length_mils: int,  # retained for ABI compat; ignored
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Compute (stub_start, stub_end) for a pin.

    ``ISch_Pin.Location`` returns the ELECTRICAL endpoint of the pin
    (the point where a wire attaches), NOT the body-side end. Verified
    empirically: U1 pin 1 BOOT of a placed TPS54331D symbol returned
    location (3500, 4400) when the IC body's left edge was at x=3700;
    the leftmost (electrical) end of the pin graphic at x=3500 is what
    Pin.Location reports.

    Therefore the stub starts AT Pin.Location and extends outward along
    the pin's orientation vector by ``_STUB_LEN_MILS``. ``pin_length``
    is not added: doing so was the original Slice 1-3 bug that left the
    stub wires floating ``pin_length`` mils away from the actual pin
    terminal.
    """
    dx, dy = _pin_direction_vector(orientation)
    hot_x = pin_x
    hot_y = pin_y
    end_x = hot_x + dx * _STUB_LEN_MILS
    end_y = hot_y + dy * _STUB_LEN_MILS
    return ((hot_x, hot_y), (end_x, end_y))


def _power_port_orientation(pin_orientation: int, is_ground: bool) -> int:
    """Canonical schematic convention:

    - VCC / power rails ALWAYS point up   (orientation 1) -- bar / circle
      glyph sits above the connection point.
    - GND ALWAYS points down              (orientation 3) -- triangle /
      bar glyph hangs below the connection point.

    Independent of the pin's outward direction. The stub wire absorbs
    the horizontal-vs-vertical mismatch when the pin faces sideways:
    the port's electrical connection is always at the stub end, and
    the glyph extends UP for power or DOWN for ground from there.

    ``pin_orientation`` is unused (retained for ABI compat).
    """
    del pin_orientation  # noqa: F841 - retained for ABI compat
    return 3 if is_ground else 1


def _net_label_orientation(pin_orientation: int) -> int:
    """Pick net-label rotation so text runs parallel to the stub wire.

    Horizontal pins (right/left) -> label horizontal (0).
    Vertical pins (up/down)      -> label rotated 90 so text reads along Y.
    """
    if pin_orientation in (1, 3):
        return 1
    return 0


def _segment_crosses_rect(
    x1: int, y1: int, x2: int, y2: int,
    rx1: int, ry1: int, rx2: int, ry2: int,
) -> bool:
    """True iff axis-aligned segment (x1,y1)->(x2,y2) crosses the interior
    of the axis-aligned rectangle [rx1,rx2] x [ry1,ry2]. Endpoints sitting
    on the boundary count as crossings only when the segment continues
    into the interior.
    """
    rx1, rx2 = (rx1, rx2) if rx1 <= rx2 else (rx2, rx1)
    ry1, ry2 = (ry1, ry2) if ry1 <= ry2 else (ry2, ry1)
    if y1 == y2:  # horizontal segment
        if y1 <= ry1 or y1 >= ry2:
            return False
        seg_x_lo, seg_x_hi = min(x1, x2), max(x1, x2)
        return seg_x_lo < rx2 and seg_x_hi > rx1
    if x1 == x2:  # vertical segment
        if x1 <= rx1 or x1 >= rx2:
            return False
        seg_y_lo, seg_y_hi = min(y1, y2), max(y1, y2)
        return seg_y_lo < ry2 and seg_y_hi > ry1
    return False  # only orthogonal segments here


def _l_path_collisions(
    x1: int, y1: int, x2: int, y2: int,
    horiz_first: bool,
    obstacles: list[tuple[int, int, int, int]],
    skip_at: tuple[tuple[int, int], ...] = (),
) -> int:
    """Count how many obstacle rects an L-path (x1,y1)->(x2,y2) crosses.

    Obstacles are (rx1, ry1, rx2, ry2) bboxes. Skip rects that contain
    either endpoint (those are the pin's own component or the centroid's
    host part — wires must enter / leave SOME bbox to connect).
    """
    if horiz_first:
        segs = [(x1, y1, x2, y1), (x2, y1, x2, y2)]
    else:
        segs = [(x1, y1, x1, y2), (x1, y2, x2, y2)]
    n = 0
    for (sx1, sy1, sx2, sy2) in segs:
        for rx1, ry1, rx2, ry2 in obstacles:
            # Skip obstacles owning either endpoint.
            owns_start = rx1 <= x1 <= rx2 and ry1 <= y1 <= ry2
            owns_end = rx1 <= x2 <= rx2 and ry1 <= y2 <= ry2
            if owns_start or owns_end:
                continue
            if (x1, y1) in skip_at or (x2, y2) in skip_at:
                continue
            if _segment_crosses_rect(sx1, sy1, sx2, sy2, rx1, ry1, rx2, ry2):
                n += 1
                break
    return n


def _route_l_path(
    x1: int, y1: int, x2: int, y2: int,
    obstacles: list[tuple[int, int, int, int]],
) -> list[tuple[int, int, int, int]]:
    """Pick the better-of-two L-path orderings (H-then-V vs V-then-H)
    based on which crosses fewer obstacles. Returns the segment list."""
    if x1 == x2 and y1 == y2:
        return []
    if x1 == x2 or y1 == y2:
        return [(x1, y1, x2, y2)]
    h_first = _l_path_collisions(x1, y1, x2, y2, True, obstacles)
    v_first = _l_path_collisions(x1, y1, x2, y2, False, obstacles)
    if v_first < h_first:
        return [(x1, y1, x1, y2), (x1, y2, x2, y2)]
    return [(x1, y1, x2, y1), (x2, y1, x2, y2)]


def _route_signal_pins(
    stub_ends: list[tuple[int, int]],
    obstacles: list[tuple[int, int, int, int]] | None = None,
) -> list[tuple[int, int, int, int]]:
    """Manhattan-route wires connecting the stub ends of pins on a signal net.

    Within-block schematic convention: connect same-net pins with real
    wires rather than relying on per-pin net labels. Power and ground
    nets are exempt (the rail symbology is the connection).

    For 2 pins: a 2-segment L-path between the two stub ends.
    For 3+ pins: a star to the centroid, each spoke an L-path.

    All segments are axis-aligned (horizontal OR vertical), grid-snapped.
    When ``obstacles`` (component-body bboxes) are supplied each L-path
    picks the ordering that crosses fewer of them; this is what task #50
    needed to drop the 37-wire-through-component crossings the audit saw.
    """
    obstacles = obstacles or []
    if len(stub_ends) < 2:
        return []
    segs: list[tuple[int, int, int, int]] = []
    if len(stub_ends) == 2:
        (x1, y1), (x2, y2) = stub_ends
        segs.extend(_route_l_path(x1, y1, x2, y2, obstacles))
        return segs

    # 3+ pin: try CHAIN topology (sort by x then y, connect consecutive
    # pairs) and STAR topology (hub at a smartly-chosen point), pick the
    # one with fewer crossings. Chain avoids the central hub crowding;
    # star is sometimes shorter total but tends to cross the middle.
    def _count_chain_crossings(pts: list[tuple[int, int]]) -> tuple[int, list[tuple[int, int, int, int]]]:
        cs = 0
        ss: list[tuple[int, int, int, int]] = []
        for i in range(len(pts) - 1):
            x1, y1 = pts[i]
            x2, y2 = pts[i + 1]
            spoke = _route_l_path(x1, y1, x2, y2, obstacles)
            ss.extend(spoke)
            for (sx1, sy1, sx2, sy2) in spoke:
                for rx1, ry1, rx2, ry2 in obstacles:
                    owns_a = rx1 <= x1 <= rx2 and ry1 <= y1 <= ry2
                    owns_b = rx1 <= x2 <= rx2 and ry1 <= y2 <= ry2
                    if owns_a or owns_b:
                        continue
                    if _segment_crosses_rect(sx1, sy1, sx2, sy2, rx1, ry1, rx2, ry2):
                        cs += 1
                        break
        return cs, ss

    # Try chain in x-then-y sort and in y-then-x sort.
    by_x = sorted(stub_ends, key=lambda p: (p[0], p[1]))
    by_y = sorted(stub_ends, key=lambda p: (p[1], p[0]))
    best_segs: list[tuple[int, int, int, int]] = []
    best_crossings = -1
    for chain in (by_x, by_y):
        cs, ss = _count_chain_crossings(chain)
        if best_crossings < 0 or cs < best_crossings:
            best_crossings = cs
            best_segs = ss

    if best_crossings == 0:
        return best_segs

    # 3+ pin star. Pick the routing HUB so that no L-path spoke is forced to
    # cross a component body. Candidates:
    #   * each stub end (using a pin as the hub - cleanest for nets where one
    #     pin clearly belongs to the IC anchoring the net)
    #   * the geometric centroid (snapped to grid)
    #   * the centroid pushed out of any obstacle it lands inside
    # The candidate with the fewest spoke collisions wins.
    raw_cx = sum(p[0] for p in stub_ends) // len(stub_ends)
    raw_cy = sum(p[1] for p in stub_ends) // len(stub_ends)
    raw_cx = (raw_cx // 100) * 100
    raw_cy = (raw_cy // 100) * 100

    candidates: list[tuple[int, int]] = [(raw_cx, raw_cy)]
    candidates.extend(stub_ends)
    # Centroid pushed to each obstacle edge if the centroid is interior.
    for rx1, ry1, rx2, ry2 in obstacles:
        if rx1 < raw_cx < rx2 and ry1 < raw_cy < ry2:
            candidates.append((rx1 - 100, raw_cy))
            candidates.append((rx2 + 100, raw_cy))
            candidates.append((raw_cx, ry1 - 100))
            candidates.append((raw_cx, ry2 + 100))

    best_segs: list[tuple[int, int, int, int]] = []
    best_crossings = -1
    for (hx, hy) in candidates:
        cand_segs: list[tuple[int, int, int, int]] = []
        crossings = 0
        for (x, y) in stub_ends:
            if x == hx and y == hy:
                continue
            spoke = _route_l_path(x, y, hx, hy, obstacles)
            cand_segs.extend(spoke)
            for (sx1, sy1, sx2, sy2) in spoke:
                for rx1, ry1, rx2, ry2 in obstacles:
                    owns_start = rx1 <= x <= rx2 and ry1 <= y <= ry2
                    owns_end = rx1 <= hx <= rx2 and ry1 <= hy <= ry2
                    if owns_start or owns_end:
                        continue
                    if _segment_crosses_rect(
                        sx1, sy1, sx2, sy2, rx1, ry1, rx2, ry2
                    ):
                        crossings += 1
                        break
        if crossings < best_crossings:
            best_crossings = crossings
            best_segs = cand_segs
            if crossings == 0:
                break
    return best_segs


def _signal_label_anchor(
    stub_ends: list[tuple[int, int]],
) -> tuple[int, int]:
    """Pick where a signal net's ONE label sits.

    2-pin nets: at the first stub end (sits next to a part, easy to read).
    3+ pin nets: at the centroid (the star's hub).
    """
    if not stub_ends:
        return (0, 0)
    if len(stub_ends) <= 2:
        return stub_ends[0]
    cx = sum(p[0] for p in stub_ends) // len(stub_ends)
    cy = sum(p[1] for p in stub_ends) // len(stub_ends)
    return ((cx // 100) * 100, (cy // 100) * 100)


def _place_net_labels(
    plan: DesignPlan,
    placed_refdes: set[str],
    parts_by_sheet: dict[str, list[Part]],
    project: Path,
    bridge: Any,
    result: ExecutorResult,
    *,
    placement_by_refdes: dict[str, Any] | None = None,
) -> None:
    """For each plan net, drop a net label or power port at every endpoint pin.

    Reads pin world coordinates via ``generic.get_sch_component_pins``,
    a helper in Pascal that iterates ePin children of a placed
    eSchComponent on the active sheet. Pin lookups are cached per sheet
    activation to avoid redundant IPC round-trips.

    ``placement_by_refdes`` is the layout output. When supplied, signal-net
    wire routing avoids component-body bboxes derived from each part's
    pin count via ``_bbox_half``.
    """
    # Component-body obstacles for wire-collision-aware routing (task #50).
    # Query REAL BoundingRectangle from Altium so the router and the
    # downstream audit see the same world. Falls back to a body-only
    # estimate if the query fails (e.g. Pascal hasn't been updated).
    def _estimate_body_half(pc: int) -> int:
        if pc >= 16:
            return 600
        if pc >= 4:
            return 350
        return 150

    pin_count_for_obstacles: dict[str, int] = {p.refdes: 0 for p in plan.parts}
    for net in plan.nets:
        for pin in net.pins:
            pin_count_for_obstacles[pin.refdes] = (
                pin_count_for_obstacles.get(pin.refdes, 0) + 1
            )

    obstacles_by_sheet: dict[str, list[tuple[int, int, int, int]]] = {}
    if placement_by_refdes:
        # Group placements by sheet to do one query per sheet.
        placements_by_sheet: dict[str, list[Any]] = {}
        for p in placement_by_refdes.values():
            placements_by_sheet.setdefault(p.sheet, []).append(p)

        for sheet, sheet_placements in placements_by_sheet.items():
            sheet_p = str(_sheet_path(project, sheet))
            real_bboxes: dict[str, tuple[int, int, int, int]] = {}
            try:
                resp = bridge.send_command(
                    "generic.query_objects",
                    {
                        "object_type": "eSchComponent",
                        "scope": f"doc:{sheet_p}",
                        "properties": (
                            "Designator.Text,"
                            "BoundingRectangle.X1,BoundingRectangle.Y1,"
                            "BoundingRectangle.X2,BoundingRectangle.Y2"
                        ),
                    },
                )
                for row in (resp or {}).get("objects", []):
                    refdes = str(row.get("Designator.Text", "")).strip()
                    if not refdes:
                        continue
                    try:
                        x1 = int(row["BoundingRectangle.X1"])
                        y1 = int(row["BoundingRectangle.Y1"])
                        x2 = int(row["BoundingRectangle.X2"])
                        y2 = int(row["BoundingRectangle.Y2"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    real_bboxes[refdes] = (
                        min(x1, x2), min(y1, y2),
                        max(x1, x2), max(y1, y2),
                    )
            except Exception:
                real_bboxes = {}

            for p in sheet_placements:
                if p.refdes in real_bboxes:
                    obstacles_by_sheet.setdefault(sheet, []).append(
                        real_bboxes[p.refdes]
                    )
                else:
                    half = _estimate_body_half(
                        pin_count_for_obstacles.get(p.refdes, 2)
                    )
                    obstacles_by_sheet.setdefault(sheet, []).append((
                        p.x_mils - half, p.y_mils - half,
                        p.x_mils + half, p.y_mils + half,
                    ))
    refdes_to_sheet: dict[str, str] = {p.refdes: p.sheet for p in plan.parts}

    # (sheet_name, refdes) -> {pin_id -> (x_mils, y_mils, orientation, pin_length_mils)}
    # orientation is the TRotationBy90 enum (0=right, 1=up, 2=left, 3=down)
    # returned by Gen_GetSchComponentPins; pin_length_mils is how far the
    # electrical hot end sits from Pin.Location along that vector.
    sheet_pin_cache: dict[
        tuple[str, str], dict[str, tuple[int, int, int, int]]
    ] = {}

    nets_by_sheet: dict[str, list[Net]] = {}
    for net in plan.nets:
        sheets_touched = {refdes_to_sheet.get(p.refdes) for p in net.pins}
        sheets_touched.discard(None)
        for sheet in sheets_touched:
            nets_by_sheet.setdefault(sheet, []).append(net)

    for sheet_name, nets in nets_by_sheet.items():
        sheet_path = _sheet_path(project, sheet_name)
        try:
            bridge.send_command(
                "application.set_active_document", {"file_path": str(sheet_path)}
            )
        except Exception as exc:
            result.notes.append(
                f"could not activate sheet {sheet_name} for wiring: {exc}"
            )
            continue

        # Per-sheet bulk buffers:
        # - wires: stubs + signal-net Manhattan routing
        # - labels: signal-net naming labels
        # - ports: power-rail / ground glyphs
        pending_wires: list[tuple[int, int, int, int]] = []
        pending_labels: list[tuple[str, int, int]] = []
        pending_ports: list[tuple[str, int, int, str, int]] = []

        sheet_p = str(_sheet_path(project, sheet_name))

        # Whole-sheet pin dump: ONE IPC call replaces N per-component
        # get_sch_component_pins calls. Pascal iterates the sheet once
        # and returns every pin keyed by refdes.
        try:
            dump = bridge.send_command(
                "generic.get_sch_doc_pins",
                {"sheet_path": str(sheet_path)},
                timeout=_LABEL_TIMEOUT_S,
            )
        except Exception as exc:
            result.notes.append(
                f"sheet pin dump for {sheet_name} failed, falling back: {exc}"
            )
            dump = None
        if dump:
            for row in dump.get("pins", []):
                rd = str(row.get("refdes", "")).strip()
                if not rd:
                    continue
                try:
                    px = int(row.get("x_mils", 0))
                    py = int(row.get("y_mils", 0))
                except (TypeError, ValueError):
                    continue
                try:
                    orient = int(row.get("orientation", 0))
                except (TypeError, ValueError):
                    orient = 0
                try:
                    plen = int(row.get("pin_length_mils", 0))
                except (TypeError, ValueError):
                    plen = 0
                pn = str(row.get("pin_number", "")).strip()
                nm = str(row.get("pin_name", "")).strip()
                entry = (px, py, orient, plen)
                cache_key = (sheet_name, rd)
                pm = sheet_pin_cache.setdefault(cache_key, {})
                if pn:
                    pm[pn] = entry
                if nm and nm not in pm:
                    pm[nm] = entry

        for net in nets:
            # Two-pass: gather per-pin info into ``net_actions`` first, then
            # decide between per-pin power ports (rails) and wire-routing +
            # ONE label (signals) at the net level.
            net_actions: list[tuple[Any, tuple[int, int], int]] = []
            for pin_ref in net.pins:
                if pin_ref.refdes not in placed_refdes:
                    result.failures.append(
                        PartFailure(
                            refdes=pin_ref.refdes,
                            reason=(
                                f"net {net.name} references unplaced "
                                f"refdes {pin_ref.refdes}"
                            ),
                            code="UNPLACED_REFDES",
                        )
                    )
                    continue
                if refdes_to_sheet.get(pin_ref.refdes) != sheet_name:
                    # Cross-sheet net, only label the pin on its home sheet.
                    continue

                # Whole-sheet pin dump already populated sheet_pin_cache
                # above. If it failed, fall back to a per-part query.
                cache_key = (sheet_name, pin_ref.refdes)
                if cache_key not in sheet_pin_cache:
                    try:
                        info = bridge.send_command(
                            "generic.get_sch_component_pins",
                            {
                                "designator": pin_ref.refdes,
                                "sheet_path": str(_sheet_path(project, sheet_name)),
                            },
                            timeout=_LABEL_TIMEOUT_S,
                        )
                    except Exception as exc:
                        result.failures.append(
                            PartFailure(
                                refdes=pin_ref.refdes,
                                reason=f"get_sch_component_pins failed: {exc}",
                                code="PIN_LOOKUP_FAILED",
                            )
                        )
                        sheet_pin_cache[cache_key] = {}
                        continue

                    pin_map: dict[str, tuple[int, int, int, int]] = {}
                    for row in (info or {}).get("pins", []):
                        try:
                            x = int(row.get("x_mils", 0))
                            y = int(row.get("y_mils", 0))
                        except (TypeError, ValueError):
                            continue
                        try:
                            orient = int(row.get("orientation", 0))
                        except (TypeError, ValueError):
                            orient = 0
                        try:
                            plen = int(row.get("pin_length_mils", 0))
                        except (TypeError, ValueError):
                            plen = 0
                        pn = str(row.get("pin_number", "")).strip()
                        nm = str(row.get("pin_name", "")).strip()
                        entry = (x, y, orient, plen)
                        if pn:
                            pin_map[pn] = entry
                        if nm and nm not in pin_map:
                            pin_map[nm] = entry
                    sheet_pin_cache[cache_key] = pin_map

                pin_map = sheet_pin_cache[cache_key]
                entry = pin_map.get(pin_ref.pin)
                if entry is None:
                    result.failures.append(
                        PartFailure(
                            refdes=pin_ref.refdes,
                            reason=(
                                f"pin {pin_ref.pin} not found on placed "
                                f"component (have: {sorted(pin_map.keys())})"
                            ),
                            code="PIN_NOT_FOUND",
                        )
                    )
                    continue

                x, y, pin_orient, pin_len = entry
                (hot_x, hot_y), (end_x, end_y) = _stub_endpoints(
                    x, y, pin_orient, pin_len
                )
                # Every pin still gets a stub wire from the pin endpoint
                # outward to the label / port / routing-junction point.
                pending_wires.append((hot_x, hot_y, end_x, end_y))
                net_actions.append((pin_ref, (end_x, end_y), pin_orient))

            if not net_actions:
                continue

            stub_ends: list[tuple[int, int]] = [a[1] for a in net_actions]

            if net.is_power or net.is_ground:
                # Rails: power-port consolidation (task #51). Group pins by
                # proximity (Manhattan radius), place ONE rail glyph per
                # cluster, wire every pin in the cluster to the glyph via
                # the same obstacle-aware Manhattan router used for signals.
                # Cuts the buck's 18 stacked GND/VIN/VOUT glyphs down to a
                # handful and frees vertical space around each pin.
                is_gnd = net.is_ground or "GND" in net.name.upper()
                style = _ground_style(net.name) if is_gnd else "bar"
                # Greedy clustering: each stub_end joins the first cluster
                # whose nearest member is within CLUSTER_RADIUS_MILS.
                CLUSTER_RADIUS_MILS = 2500
                clusters: list[list[int]] = []  # list of indices into net_actions
                for i, (_, (ex, ey), _) in enumerate(net_actions):
                    joined = False
                    for cl in clusters:
                        for j in cl:
                            mx, my = net_actions[j][1]
                            if abs(ex - mx) + abs(ey - my) <= CLUSTER_RADIUS_MILS:
                                cl.append(i)
                                joined = True
                                break
                        if joined:
                            break
                    if not joined:
                        clusters.append([i])

                sheet_obstacles = obstacles_by_sheet.get(sheet_name, [])
                for cl in clusters:
                    cluster_pts = [net_actions[i][1] for i in cl]
                    cluster_orients = [net_actions[i][2] for i in cl]
                    # Port location: centroid X, then above the topmost pin
                    # (VCC, glyph goes up) or below the bottom pin (GND).
                    centroid_x = (
                        sum(pt[0] for pt in cluster_pts) // len(cluster_pts)
                    )
                    centroid_x = (centroid_x // 100) * 100
                    if is_gnd:
                        port_y = min(pt[1] for pt in cluster_pts) - 400
                    else:
                        port_y = max(pt[1] for pt in cluster_pts) + 400
                    port_y = (port_y // 100) * 100
                    # Re-derive an orientation FROM the average pin orient
                    # in the cluster (still respects rail-direction rule).
                    port_orient = _power_port_orientation(
                        cluster_orients[0], is_ground=is_gnd
                    )
                    pending_ports.append(
                        (net.name, centroid_x, port_y, style, port_orient)
                    )
                    result.power_ports_placed += 1
                    # Wire each pin in the cluster to the port.
                    for pt in cluster_pts:
                        if pt == (centroid_x, port_y):
                            continue
                        for seg in _route_l_path(
                            pt[0], pt[1], centroid_x, port_y, sheet_obstacles
                        ):
                            pending_wires.append(seg)
            else:
                # Signal nets: wire pins together directly + ONE label.
                for seg in _route_signal_pins(
                    stub_ends, obstacles_by_sheet.get(sheet_name, [])
                ):
                    pending_wires.append(seg)
                if stub_ends:
                    label_x, label_y = _signal_label_anchor(stub_ends)
                    pending_labels.append((net.name, label_x, label_y))
                    result.nets_labelled += 1

        # End of net/pin loops for this sheet — flush the three batches.
        if pending_wires:
            wires_payload = "~~".join(
                f"x1={hx};y1={hy};x2={ex};y2={ey}"
                for (hx, hy, ex, ey) in pending_wires
            )
            try:
                bridge.send_command(
                    "generic.place_wires",
                    {"wires": wires_payload},
                    timeout=_LABEL_TIMEOUT_S * max(1, len(pending_wires) // 8),
                )
            except Exception as exc:
                result.notes.append(
                    f"bulk stub-wires for sheet {sheet_name} failed: {exc}"
                )

        if pending_labels:
            labels_payload = "~~".join(
                f"text={t};x={x};y={y};orientation=0"
                for (t, x, y) in pending_labels
            )
            try:
                bridge.send_command(
                    "generic.place_net_labels",
                    {"labels": labels_payload},
                    timeout=_LABEL_TIMEOUT_S * max(1, len(pending_labels) // 8),
                )
            except Exception as exc:
                result.notes.append(
                    f"bulk net-labels for sheet {sheet_name} failed: {exc}"
                )

        if pending_ports:
            ports_payload = "~~".join(
                f"text={t};x={x};y={y};style={style};orientation={orient}"
                for (t, x, y, style, orient) in pending_ports
            )
            try:
                bridge.send_command(
                    "generic.place_power_ports",
                    {"ports": ports_payload},
                    timeout=_LABEL_TIMEOUT_S * max(1, len(pending_ports) // 8),
                )
            except Exception as exc:
                result.notes.append(
                    f"bulk power-ports for sheet {sheet_name} failed: {exc}"
                )


def execute_plan_from_json(
    plan_json: str,
    project_path: str,
    *,
    bridge: Optional[Any] = None,
) -> ExecutorResult:
    """Convenience wrapper: parse + validate JSON, then run executor."""
    import json as _json

    result = ExecutorResult(project_path=project_path)
    try:
        payload = _json.loads(plan_json)
    except _json.JSONDecodeError as exc:
        result.ok = False
        result.notes.append(f"invalid JSON: {exc}")
        return result

    try:
        plan = DesignPlan.model_validate(payload)
    except ValidationError as exc:
        result.ok = False
        result.notes.extend(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        )
        return result

    return execute_plan(plan, project_path, bridge=bridge)

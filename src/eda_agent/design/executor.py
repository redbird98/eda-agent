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

        for part in parts:
            placement = placement_by_refdes.get(part.refdes)
            if placement is None:  # defensive, compute_layout covers every part
                result.failures.append(
                    PartFailure(
                        refdes=part.refdes,
                        reason="layout did not produce a placement",
                        code="LAYOUT_GAP",
                    )
                )
                continue

            params: dict[str, Any] = {
                "lib_reference": part.lib_ref,
                "library_path": part.lib_path or "",
                "sheet_path": str(_sheet_path(project, part.sheet)),
                "x": str(placement.x_mils),
                "y": str(placement.y_mils),
                "designator": part.refdes,
                "rotation": str(placement.rotation),
                "footprint": part.footprint or "",
            }

            try:
                bridge.send_command(
                    "generic.place_sch_component_from_library",
                    params,
                    timeout=_PLACE_TIMEOUT_S,
                )
                result.placed.append(placement)
            except Exception as exc:
                result.failures.append(
                    PartFailure(
                        refdes=part.refdes,
                        reason=f"place failed: {exc}",
                        code="PLACE_FAILED",
                    )
                )

    # Wiring stage, drop a net label at every plan-defined pin endpoint.
    # Power and ground nets get power ports instead of plain labels.
    placed_refdes = {p.refdes for p in result.placed}
    if placed_refdes:
        _place_net_labels(plan, placed_refdes, parts_by_sheet, project, bridge, result)

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


def _place_net_labels(
    plan: DesignPlan,
    placed_refdes: set[str],
    parts_by_sheet: dict[str, list[Part]],
    project: Path,
    bridge: Any,
    result: ExecutorResult,
) -> None:
    """For each plan net, drop a net label or power port at every endpoint pin.

    Reads pin world coordinates via ``generic.get_sch_component_pins``,
    a helper in Pascal that iterates ePin children of a placed
    eSchComponent on the active sheet. Pin lookups are cached per sheet
    activation to avoid redundant IPC round-trips.
    """
    refdes_to_sheet: dict[str, str] = {p.refdes: p.sheet for p in plan.parts}

    sheet_pin_cache: dict[tuple[str, str], dict[str, tuple[int, int]]] = {}
    """(sheet_name, refdes) -> {pin_id (number or name): (x_mils, y_mils)}"""

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

        for net in nets:
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
                    # Cross-sheet net, Slice B.2 keeps it simple and only
                    # places a label on the home sheet of each pin. The pin
                    # gets its label when we visit its sheet in another loop
                    # iteration.
                    continue

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

                    pin_map: dict[str, tuple[int, int]] = {}
                    for row in (info or {}).get("pins", []):
                        try:
                            x = int(row.get("x_mils", 0))
                            y = int(row.get("y_mils", 0))
                        except (TypeError, ValueError):
                            continue
                        pn = str(row.get("pin_number", "")).strip()
                        nm = str(row.get("pin_name", "")).strip()
                        if pn:
                            pin_map[pn] = (x, y)
                        if nm and nm not in pin_map:
                            pin_map[nm] = (x, y)
                    sheet_pin_cache[cache_key] = pin_map

                pin_map = sheet_pin_cache[cache_key]
                coords = pin_map.get(pin_ref.pin)
                if coords is None:
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

                x, y = coords

                sheet_p = str(_sheet_path(project, sheet_name))
                if net.is_power:
                    style = "circle"
                    if "GND" in net.name.upper():
                        style = _ground_style(net.name)
                    try:
                        bridge.send_command(
                            "generic.place_power_port",
                            {
                                "text": net.name,
                                "sheet_path": sheet_p,
                                "x": str(x),
                                "y": str(y),
                                "style": style,
                            },
                            timeout=_LABEL_TIMEOUT_S,
                        )
                        result.power_ports_placed += 1
                    except Exception as exc:
                        result.failures.append(
                            PartFailure(
                                refdes=pin_ref.refdes,
                                reason=f"place_power_port failed: {exc}",
                                code="POWER_PORT_FAILED",
                            )
                        )
                elif net.is_ground:
                    try:
                        bridge.send_command(
                            "generic.place_power_port",
                            {
                                "text": net.name,
                                "sheet_path": sheet_p,
                                "x": str(x),
                                "y": str(y),
                                "style": _ground_style(net.name),
                            },
                            timeout=_LABEL_TIMEOUT_S,
                        )
                        result.power_ports_placed += 1
                    except Exception as exc:
                        result.failures.append(
                            PartFailure(
                                refdes=pin_ref.refdes,
                                reason=f"place_power_port (gnd) failed: {exc}",
                                code="POWER_PORT_FAILED",
                            )
                        )
                else:
                    try:
                        bridge.send_command(
                            "generic.place_net_label",
                            {
                                "text": net.name,
                                "sheet_path": sheet_p,
                                "x": str(x),
                                "y": str(y),
                                "orientation": "0",
                            },
                            timeout=_LABEL_TIMEOUT_S,
                        )
                        result.nets_labelled += 1
                    except Exception as exc:
                        result.failures.append(
                            PartFailure(
                                refdes=pin_ref.refdes,
                                reason=f"place_net_label failed: {exc}",
                                code="NET_LABEL_FAILED",
                            )
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

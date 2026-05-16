# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba
"""Design-agent MCP tools, surfaces the design discipline + primitives.

Claude Code is the planner. It calls ``design.get_discipline`` to read
the rules, ``design.snapshot_inventory`` to learn what parts exist in
the user's libraries, then constructs a DesignPlan JSON, validates it
with ``design.validate_plan``, and hands it to ``design.execute_plan``
to instantiate the schematic.

This module deliberately makes no Anthropic API calls, the AI is the
client (Claude Code), this is just the tool layer it drives.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Union

from pydantic import ValidationError

from ..design.audit import audit_schematic as run_audit_schematic
from ..design.discipline import get_discipline
from ..design.executor import execute_plan_from_json
from ..design.inventory import LibraryInventory, snapshot_live
from ..design.learner import learn_from_layout
from ..design.orchestrator import (
    execute_plan_via_canvas_from_json,
    preview_plan_from_json,
)
from ..design.plan import DesignPlan
from ..design.validator import validate as run_validate


def register_design_tools(mcp) -> None:
    """Register the design-agent tools with the MCP server."""

    @mcp.tool()
    async def design_get_discipline() -> dict[str, Any]:
        """Read the design discipline + DesignPlan JSON schema.

        ALWAYS call this first when starting a design task. The result
        contains the hard rules the planner must follow (net-label-driven
        wiring, datasheet-first part choice, NDA isolation, etc.) plus
        the JSON schema that ``design.execute_plan`` enforces on input.

        Returns:
            A dict with ``discipline`` (markdown text) and
            ``schema`` (DesignPlan JSON schema as a dict).
        """
        return {
            "discipline": get_discipline(),
            "schema": DesignPlan.model_json_schema(),
        }

    @mcp.tool()
    async def design_snapshot_inventory(
        library_paths: list[str],
    ) -> dict[str, Any]:
        """Open a list of SchLib files and return what components live in them.

        NDA scope: only pass paths to the user's own neutral standard
        libraries. Do NOT pass project-local library paths from another
        client engagement, design knowledge cannot cross NDA boundaries.

        Args:
            library_paths: Absolute paths to .SchLib files to scan.

        Returns:
            Inventory dict: ``{"libraries": [{"path", "components": [...]}]}``.
            Each component carries lib_ref, designator_prefix, pin_count,
            description, and footprint when available. The planner uses
            this to bias its part choices toward existing-lib parts.
        """
        paths = [Path(p) for p in library_paths]
        inventory = snapshot_live(paths)
        return inventory.model_dump()

    @mcp.tool()
    async def design_validate_plan(plan_json: Union[str, dict]) -> dict[str, Any]:
        """Validate a candidate DesignPlan JSON against the schema + cross-check.

        Run this before ``design.execute_plan`` to catch schema problems
        and cross-references that the executor will reject. Cheap; no
        Altium round-trip.

        Args:
            plan_json: Either a JSON string of the DesignPlan, or the
                DesignPlan as a JSON object/dict. The MCP framework
                auto-deserializes JSON-object literals to dicts before
                the tool sees them, so both shapes are accepted.

        Returns:
            ``{"ok": True, "summary": "..."}`` on success, or
            ``{"ok": False, "errors": [...]}`` listing the specific
            problems. The planner can read these and revise.
        """
        if isinstance(plan_json, dict):
            payload = plan_json
        else:
            try:
                payload = json.loads(plan_json)
            except json.JSONDecodeError as exc:
                return {"ok": False, "errors": [f"invalid JSON: {exc}"]}

        try:
            plan = DesignPlan.model_validate(payload)
        except ValidationError as exc:
            return {
                "ok": False,
                "errors": [
                    f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                    for err in exc.errors()
                ],
            }

        cross = plan.cross_check()
        if cross:
            return {"ok": False, "errors": cross}

        return {
            "ok": True,
            "summary": (
                f"Plan valid. {len(plan.parts)} parts, {len(plan.nets)} nets, "
                f"{len(plan.sheets)} sheet(s)."
            ),
        }

    @mcp.tool()
    async def design_execute_plan(
        plan_json: Union[str, dict],
        project_path: str,
        use_canvas: bool = True,
        placement_hints: Optional[dict[str, dict[str, int]]] = None,
    ) -> dict[str, Any]:
        """Instantiate a DesignPlan in Altium.

        Two execution paths:

        - ``use_canvas=True`` (default): the new canvas-based pipeline.
          Plan -> SymbolExtractor -> SchematicCanvas (all in Python) ->
          one-shot batched emit to Altium. Layout decisions are made
          before any IPC; an SVG preview is written next to the project
          file so you can sanity-check the schematic without opening it.

        - ``use_canvas=False``: the legacy executor that interleaves
          layout with Altium IPC. Kept as a fallback while the new path
          stabilises.

        Both halt early if the plan contains any needs_creation parts.
        Resolve those first by either picking an existing-lib equivalent
        or branching into a library-authoring sub-task.

        Args:
            plan_json: Either a DesignPlan JSON string, or the DesignPlan
                as a JSON object/dict. The MCP framework auto-deserializes
                JSON-object literals to dicts before the tool sees them,
                so both shapes are accepted.
            project_path: Absolute path to the target .PrjPcb. Created
                if it does not exist.
            use_canvas: Pick the execution path. Default ``True`` runs
                the new canvas pipeline; pass ``False`` to fall back to
                the legacy executor.

        Args (continued):
            placement_hints: Optional ``{refdes: {"x": int, "y": int,
                "rotation": int}}`` partial anchors. Hinted refdes pin
                to the supplied position; others run through the
                algorithmic placement (Sugiyama + multi-try scoring).
                Used by the agent-in-loop refinement workflow:
                  1. Run ``design_preview_plan`` -> see SVG + score.
                  2. If layout is bad, identify specific refdes that
                     should sit elsewhere; build hints dict.
                  3. Call ``design_preview_plan`` again with hints ->
                     iterate until score is acceptable.
                  4. Call ``design_execute_plan`` with the same hints
                     to emit the refined layout.

        Returns:
            Result dict with ok / project_path / sheets_touched / placed
            (list of placements) / failures / needs_creation / notes.
            Canvas-path additions: ``canvas`` (the SchematicCanvas dict
            snapshot) and ``preview_svg_path`` (where the SVG was written).
        """
        if use_canvas:
            return execute_plan_via_canvas_from_json(
                plan_json, project_path,
                placement_hints=placement_hints,
            )
        if isinstance(plan_json, dict):
            plan_json = json.dumps(plan_json)
        result = execute_plan_from_json(plan_json, project_path)
        return result.to_dict()

    @mcp.tool()
    async def design_learn_from_layout(
        project_path: str,
    ) -> dict[str, Any]:
        """Capture your placement edits as training data.

        Workflow:
        1. Run ``design_execute_plan`` (canvas path) — it writes a
           ``<project>.canvas.json`` snapshot alongside the .PrjPcb.
        2. Open the schematic in Altium, drag components to taste, save.
        3. Call this tool. It reads the snapshot + current Altium
           positions, diffs them, and appends one row per moved component
           to ``%USERPROFILE%\\.eda-agent\\placement_edits.jsonl``.

        Each row carries: design_id, refdes, part_role, part_lib_ref,
        anchor_refdes, anchor_role, anchor_lib_ref, dx_mils, dy_mils,
        rot_delta_deg, design_size, ts.

        Anchor = highest-pin-count netlist neighbor on a non-power /
        non-ground net (or spatial-nearest fallback). Captures the
        relational placement preference, not just "I moved this 200 mils
        right".

        The accumulating log feeds ``placement_priors.json`` (the
        relative-anchor priors used by the pipeline's placement pass).

        Args:
            project_path: Same project path you passed to
                ``design_execute_plan``.

        Returns:
            Dict with ok, rows_appended, refdes_moved, refdes_unchanged,
            log_path, notes.
        """
        return learn_from_layout(project_path)

    @mcp.tool()
    async def design_preview_plan(
        plan_json: Union[str, dict],
        output_svg_path: Optional[str] = None,
        placement_hints: Optional[dict[str, dict[str, int]]] = None,
    ) -> dict[str, Any]:
        """Render a DesignPlan to SVG without emitting to Altium.

        Same pipeline as ``design_execute_plan(use_canvas=True)`` minus
        the place + wire + save IPC pass. Symbol extraction still
        consults Altium on cache miss (it has to read the SchLib), but
        no project is created and nothing is placed. Use this to
        sanity-check a layout cheaply before committing to a full emit.

        Args:
            plan_json: DesignPlan as JSON string or dict.
            output_svg_path: Where to write the SVG. Default:
                ``<repo>/.symbol_cache/preview.svg``.

        Returns:
            Dict with ok / preview_svg_path / canvas (snapshot) /
            counts {placements, wires, labels, power_ports, junctions} /
            notes / failures.
        """
        return preview_plan_from_json(
            plan_json, output_svg_path, placement_hints=placement_hints,
        )

    @mcp.tool()
    async def design_audit_schematic(
        project_path: Optional[str] = None,
        cluster_radius_mils: int = 600,
    ) -> dict[str, Any]:
        """Structured visual/layout audit of the active schematic.

        Call AFTER ``design.execute_plan`` and BEFORE ``design.validate``
        so layout problems are fixed first; ERC violations downstream are
        less noisy. Detects three classes of issue, each with enough
        geometry for the planner to compute a corrective move:

          * overlaps        - pairs of components whose bboxes intersect
          * wire_crossings  - wire segments that cross a component body
                              (excluding pin-to-pin connections)
          * stacked_ports   - 3+ power/ground glyphs of the same net
                              huddled inside ``cluster_radius_mils``

        Args:
            project_path: Optional .PrjPcb path. None uses the focused
                project.
            cluster_radius_mils: Radius for stacked-port clustering.
                Default 600 mils.

        Returns:
            SchematicAuditReport dict: ``{ok, project_path, overlaps[],
            wire_crossings[], stacked_ports[], notes[]}``.
            ``ok=True`` iff every list is empty.
        """
        report = run_audit_schematic(
            project_path,
            cluster_radius_mils=cluster_radius_mils,
        )
        return report.to_dict()

    @mcp.tool()
    async def design_validate(project_path: Optional[str] = None) -> dict[str, Any]:
        """ERC + connectivity sanity report on the focused project.

        Runs run_erc, project.get_messages, and get_unconnected_pins,
        then bundles the output into a structured ValidationReport that
        the planner can read and respond to. Schematic-only in this
        slice, PCB validation is a separate later slice.

        Args:
            project_path: Optional absolute path to a .PrjPcb. If omitted,
                uses the focused project.

        Returns:
            ValidationReport dict: ``{passed, project_path, errors,
            warnings, notes}`` where each error/warning is an Issue with
            ``{category, severity, text, refdes, pin, net, sheet}``.
        """
        report = run_validate(project_path)
        return report.to_dict()

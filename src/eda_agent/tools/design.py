# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba
"""Design-agent MCP tools — surfaces the design discipline + primitives.

Claude Code is the planner. It calls ``design.get_discipline`` to read
the rules, ``design.snapshot_inventory`` to learn what parts exist in
the user's libraries, then constructs a DesignPlan JSON, validates it
with ``design.validate_plan``, and hands it to ``design.execute_plan``
to instantiate the schematic.

This module deliberately makes no Anthropic API calls — the AI is the
client (Claude Code), this is just the tool layer it drives.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from ..design.discipline import get_discipline
from ..design.executor import execute_plan_from_json
from ..design.inventory import LibraryInventory, snapshot_live
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
        client engagement — design knowledge cannot cross NDA boundaries.

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
    async def design_validate_plan(plan_json: str) -> dict[str, Any]:
        """Validate a candidate DesignPlan JSON against the schema + cross-check.

        Run this before ``design.execute_plan`` to catch schema problems
        and cross-references that the executor will reject. Cheap; no
        Altium round-trip.

        Args:
            plan_json: A JSON string of the DesignPlan.

        Returns:
            ``{"ok": True, "summary": "..."}`` on success, or
            ``{"ok": False, "errors": [...]}`` listing the specific
            problems. The planner can read these and revise.
        """
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
        plan_json: str,
        project_path: str,
    ) -> dict[str, Any]:
        """Instantiate a DesignPlan in Altium (parts placement only).

        Slice B.1 scope: opens or creates the project at project_path,
        creates a SchDoc per plan.sheet, places every existing-lib part
        at a grid-computed position, saves. Wiring (net labels at pin
        coordinates) is NOT in this slice — see design.execute_plan_wires
        when Slice B.2 lands.

        The call halts early if the plan contains any needs_creation
        parts. Resolve those first by either picking an existing-lib
        equivalent or branching into a library-authoring sub-task.

        Args:
            plan_json: A DesignPlan JSON string. Validated against the
                schema before any Altium mutation.
            project_path: Absolute path to the target .PrjPcb. Created
                if it does not exist.

        Returns:
            ExecutorResult dict with ok / project_path / sheets_touched /
            placed (list of placements) / failures / needs_creation /
            notes.
        """
        result = execute_plan_from_json(plan_json, project_path)
        return result.to_dict()

    @mcp.tool()
    async def design_validate(project_path: Optional[str] = None) -> dict[str, Any]:
        """ERC + connectivity sanity report on the focused project.

        Runs run_erc, project.get_messages, and get_unconnected_pins,
        then bundles the output into a structured ValidationReport that
        the planner can read and respond to. Schematic-only in this
        slice — PCB validation is a separate later slice.

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

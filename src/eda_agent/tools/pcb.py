# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""PCB-specific tools for Altium Designer MCP Server.

Provides high-level PCB operations: net classes, design rules, DRC,
component placement, trace lengths, layer stackup, board outline, etc.
"""

from typing import Any, Optional
from ..bridge import get_bridge
from ..placement import (
    BoardRegion,
    PlaceComp,
    PlaceNet,
    PlaceOptions,
    PlacePin,
    plan_placement,
    rotate_offset,
)
from .bulk_hints import BulkHintTracker
from .datasheet_hints import tag_response


def register_pcb_tools(mcp):
    """Register PCB tools with the MCP server."""

    @mcp.tool()
    async def pcb_get_nets() -> dict[str, Any]:
        """Get all unique net names from the active PCB board.

        Returns:
            Dictionary with "nets" array of net name strings and "count"
        """
        bridge = get_bridge()
        result = await bridge.send_command_async("pcb.get_nets", {})
        return result

    @mcp.tool()
    async def pcb_focus_board(board_path: str) -> dict[str, Any]:
        """Make a specific PCB the focused/current board.

        When several PcbDocs are open, the other PCB tools
        (`pcb_get_components`, `pcb_delete_object`, `pcb_delete_net`,
        `pcb_plan_placement`, `design_visual_review`, …) operate on the
        *focused* board — and `set_active_document` does NOT reliably set
        that for a PcbDoc. Call this first to point them all at the board
        you mean. (`pcb_place_component(s)` already accept `board_path`
        directly.)

        Args:
            board_path: Absolute path to the .PcbDoc to focus.

        Returns:
            Dict with ``focused`` and the resolved ``board`` file name.
        """
        bridge = get_bridge()
        return await bridge.send_command_async(
            "pcb.focus_board", {"board_path": board_path}
        )

    @mcp.tool()
    async def pcb_delete_net(
        nets: Optional[list[str]] = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Delete nets from the active PCB.

        By default removes only EMPTY nets (no connected pads / tracks /
        vias) — the cleanup for stray nets left behind after deleting
        components, e.g. nets created by `pcb_place_component`'s synced
        mode. A net that still has connections is skipped unless
        ``force=True`` (forcing orphans those pads/tracks, so use it
        deliberately).

        Args:
            nets: Specific net names to delete. Omit (or pass an empty
                list) to sweep ALL empty nets on the board.
            force: Also delete nets that still have connected primitives
                (orphans them). Default False.

        Returns:
            Dict with ``deleted`` (count removed), ``skipped_connected``
            (count), and ``skipped_nets`` (names skipped because still
            connected).
        """
        bridge = get_bridge()
        return await bridge.send_command_async(
            "pcb.delete_nets",
            {
                "nets": ",".join(
                    str(n).strip() for n in (nets or []) if str(n).strip()
                ),
                "force": "true" if force else "false",
            },
        )

    @mcp.tool()
    async def pcb_get_net_classes() -> dict[str, Any]:
        """Get all net classes from the active PCB.

        Only returns class metadata, IPCB_ObjectClass.MemberCount and
        MemberName[] are not exposed in Altium's DelphiScript host, so
        per-member enumeration has to be done by iterating eNetObject and
        grouping by each net's parent class.

        Returns:
            Dictionary with "net_classes" array (each with name, super_class)
            and "count"
        """
        bridge = get_bridge()
        result = await bridge.send_command_async("pcb.get_net_classes", {})
        return result

    @mcp.tool()
    async def pcb_create_net_class(
        name: str,
        nets: str,
    ) -> dict[str, Any]:
        """Create a net class (or add nets to an existing one) on the active PCB.

        Args:
            name: Name for the net class (e.g., "PowerNets", "HighSpeed")
            nets: Comma-separated list of net names to add
                  (e.g., "VCC,GND,3V3")

        Returns:
            Dictionary with class_name, class_created (bool), nets_added count
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.create_net_class",
            {"name": name, "nets": nets},
        )
        return result

    @mcp.tool()
    async def pcb_get_design_rules() -> dict[str, Any]:
        """Get all design rules from the active PCB.

        Returns:
            Dictionary with "rules" array (each with name, rule_kind, enabled,
            priority, scope_1, scope_2, comment, descriptor) and "count"
        """
        bridge = get_bridge()
        result = await bridge.send_command_async("pcb.get_design_rules", {})
        return result

    @mcp.tool()
    async def pcb_set_rules_enabled(
        names: list[str],
        enabled: bool,
        match: str = "name",
    ) -> dict[str, Any]:
        """Bulk-toggle the DRC-enabled flag on design rules by name.

        Useful for focused review passes: disable a noisy class of rules
        to surface the violations that matter (e.g. silence Silk-to-Silk
        while you sweep Clearance), or re-enable a set before a release
        sweep. Matches case-insensitively; supports trailing-* wildcards
        so a rule family can be targeted without listing every name
        (``"DiffPair_*"`` hits every rule whose name starts with that).

        Note that this flips ``DRCEnabled`` (whether DRC actually checks the rule),
        not ``Enabled`` (whether the rule exists in the rule list); the
        former is the right knob for review iteration.

        Args:
            names: Rule names to toggle. With ``match="name"`` (default)
                each entry is matched case-insensitively against
                ``Rule.Name``; a trailing ``*`` wildcards the suffix
                (``"Clearance*"``). With ``match="kind"`` each entry is
                a TRuleKind ordinal as a string (look up via
                ``pcb_get_design_rules``).
            enabled: New value for ``DRCEnabled``. True turns the rule
                ON in DRC; False disables it for this run.
            match: ``"name"`` (default) or ``"kind"``.

        Returns:
            Dict with:
              - ``matched``: rules that matched any pattern
              - ``updated``: how many actually changed value
              - ``enabled``: the requested target value (for confirmation)
              - ``items``: per-matched ``{name, kind, prev_enabled, new_enabled}``
        """
        if not names:
            return {"ok": False, "reason": "names list is empty"}
        bridge = get_bridge()
        return await bridge.send_command_async(
            "pcb.set_rules_enabled",
            {
                "names": "|".join(str(n) for n in names),
                "enabled": "true" if enabled else "false",
                "match": match,
            },
        )

    @mcp.tool()
    async def pcb_get_clearance_violations(
        net: str = "",
    ) -> dict[str, Any]:
        """Run DRC and return clearance / other violations, optionally
        filtered to one net.

        Sibling of ``pcb_run_drc`` -- same underlying DRC trigger, but
        accepts a ``net`` filter so the agent can drill into a single
        net's violations without scrolling through the whole board's
        DRC report. Useful when investigating a specific high-speed
        signal or power rail.

        Filter is substring-matched against the violation's Description
        and Name, so it catches both "Net USB_DP and Net GND" clearance
        warnings and net-named via antennas.

        Returns the same enriched payload as ``pcb_run_drc`` -- each
        violation carries ``x_mils`` / ``y_mils`` / ``layer`` for the
        agent to navigate to, plus ``primitive1`` / ``primitive2``
        objects with ``{detail, type, net, layer, x_mils, y_mils}``.

        Args:
            net: Net name to filter by (substring match). Empty string
                returns ALL violations (equivalent to ``pcb_run_drc``).

        Returns:
            Dict with ``{violation_count, violations}``. Capped at 200.
        """
        bridge = get_bridge()
        params = {"net": net} if net else {}
        return await bridge.send_command_async(
            "pcb.get_clearance_violations", params, timeout=90.0)

    @mcp.tool()
    async def pcb_run_drc() -> dict[str, Any]:
        """Run Design Rule Check (DRC) on the active PCB.

        Executes the DRC and returns up to 100 violations with full
        location data, so the agent can jump straight to the offending
        spot instead of guessing from the description.

        Per-violation shape:
          - ``name``, ``description``, ``rule``: text from Altium
          - ``x_mils`` / ``y_mils`` / ``layer``: violation's bbox
            centre (the "go here" hint)
          - ``primitive1`` / ``primitive2``: objects with
            ``{detail, type, net, layer, x_mils, y_mils}``. ``type``
            is the object kind (Track / Pad / Via / Region / Comp);
            ``net`` is the net name. Two primitives are involved in
            most rule kinds (Clearance, Short Circuit); a single-
            primitive violation (e.g. AcuteAngle) leaves
            ``primitive2`` with empty fields.

        Use ``primitive1.net`` and ``primitive2.net`` to spot the
        nets in conflict; use ``x_mils`` / ``y_mils`` to drive
        ``cross_probe`` or the dashboard's Drawing tab to the site.

        Returns:
            Dict with ``violation_count`` (full count even if > 100)
            and ``violations`` array (first 100).
        """
        bridge = get_bridge()
        result = await bridge.send_command_async("pcb.run_drc", {})
        return result

    @mcp.tool()
    async def pcb_get_components() -> dict[str, Any]:
        """Get all components from the active PCB with position and properties.

        DATASHEET DISCIPLINE: The response carries a `_datasheet_guidance`
        block. Before drawing any conclusion about a listed part's
        electrical behavior, pin function, or voltage rating, fetch its
        manufacturer datasheet (use WebSearch + WebFetch if you don't
        already have it). Library metadata here is NOT authoritative.

        Returns:
            Dictionary with "components" array (each with designator, x, y,
            rotation, layer, footprint) and "count", plus
            `_datasheet_guidance` + `_datasheet_parts`.
        """
        bridge = get_bridge()
        result = await bridge.send_command_async("pcb.get_components", {})
        return tag_response(result, components=result, context="pcb_get_components")

    @mcp.tool()
    async def pcb_move_component(
        designator: str,
        x: int | None = None,
        y: int | None = None,
        rotation: float | None = None,
    ) -> dict[str, Any]:
        """Move and/or rotate ONE PCB component by its designator.

        IMPORTANT, if you need to reposition more than one component,
        use `pcb_move_components` (batch) instead. Looping this tool is
        the single biggest wall-time cost: each call is a full LLM
        round-trip, but the batch version does N moves in one turn.

        Sets the absolute position/rotation. Only provided parameters are
        changed; omitted parameters keep their current values.

        Args:
            designator: Component reference designator (e.g., "U1", "R5")
            x: New X position in mils (optional)
            y: New Y position in mils (optional)
            rotation: New rotation angle in degrees (optional, 0-360)

        Returns:
            Dictionary with final designator, x, y, rotation values
        """
        bridge = get_bridge()
        params: dict[str, Any] = {"designator": designator}
        if x is not None:
            params["x"] = str(x)
        if y is not None:
            params["y"] = str(y)
        if rotation is not None:
            params["rotation"] = str(rotation)
        result = await bridge.send_command_async("pcb.move_component", params)
        hint = BulkHintTracker.record_and_hint("pcb_move_component")
        if hint and isinstance(result, dict):
            result["_hint_bulk"] = hint
        return result

    @mcp.tool()
    async def pcb_move_components(
        moves: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Move and/or rotate MANY PCB components in ONE IPC round-trip.

        PREFER THIS over looping `pcb_move_component`. Each call to the
        singular tool is a full LLM turn (5-15 s); one call to this tool
        repositions every component in the list in a single Altium
        transaction.

        Typical uses: applying a full layout pass, running an
        auto-placement result, undoing and redoing a placement set,
        adjusting a row of components relative to each other.

        Args:
            moves: List of move dicts. Each dict supports:
                designator (required) , target component
                x        (optional)   , new X in mils
                y        (optional)   , new Y in mils
                rotation (optional)   , new rotation in degrees

            Example:
                [
                  {"designator": "U1", "x": 5000, "y": 5000, "rotation": 0},
                  {"designator": "R1", "x": 5200, "y": 4800},
                  {"designator": "C1", "rotation": 90},
                ]

        Returns:
            Dictionary with per-designator results and a count.
        """
        # Pack each move as comma-separated fields: designator,x,y,rotation
        # (empty field = leave that property unchanged). Moves joined by '|'.
        # This format is unambiguous and matches PCB_PlaceTracks.
        ops: list[str] = []
        for m in moves:
            desig = str(m.get("designator", "")).strip()
            if not desig:
                continue
            x_str = (
                str(int(m["x"])) if "x" in m and m["x"] is not None else ""
            )
            y_str = (
                str(int(m["y"])) if "y" in m and m["y"] is not None else ""
            )
            rot_str = (
                str(m["rotation"])
                if "rotation" in m and m["rotation"] is not None
                else ""
            )
            if x_str == "" and y_str == "" and rot_str == "":
                continue
            ops.append(f"{desig},{x_str},{y_str},{rot_str}")

        if not ops:
            return {"error": "No valid moves provided", "moves_applied": 0}

        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.batch_move_components",
            {"moves": "|".join(ops)},
        )
        return result

    @mcp.tool()
    async def pcb_plan_placement(
        designators: Optional[list[str]] = None,
        fixed: Optional[list[str]] = None,
        region: Optional[dict[str, float]] = None,
        iterations: int = 400,
        grid_mils: float = 5.0,
        clearance_mils: float = 15.0,
        max_net_fanout: int = 0,
        exclude_nets: Optional[list[str]] = None,
        reseed_grid: bool = False,
        optimize_rotation: bool = True,
        apply: bool = False,
    ) -> dict[str, Any]:
        """Connectivity-driven auto-placement: shorten wirelength, keep
        components legal. **Dry-run by default** -- returns a proposed
        move list and quality metrics without touching the board.

        This is the analytical-placement idea from the PCB-placement
        literature (global spring/repulsion relaxation -> legalization),
        run as a pure-Python solver on the current board. It reads the
        compiled netlist and every component's real footprint bounding
        box, then refines positions to minimize half-perimeter
        wirelength (HPWL) while keeping components inside the board
        outline and free of same-layer overlaps.

        With ``optimize_rotation`` (default on) it also picks each
        eligible part's orientation (0/90/180/270) to point its pins at
        the nets they connect -- the classic auto-place win for 2-pin
        passives. This needs per-pin geometry, so the tool also reads the
        board's pads and maps them to components; orientation is only
        considered for top-side parts currently sitting at an orthogonal
        angle (others keep their rotation).

        Workflow: call once with ``apply=False`` (default), read the
        ``hpwl_improvement_pct`` and ``overlap_pairs_after``, eyeball the
        ``moves``, then call again with ``apply=True`` to commit them in
        one ``pcb.batch_move_components`` transaction. Components on
        opposite layers may share an X/Y footprint (they cannot
        physically collide).

        Seeding: by default the solver refines the board's CURRENT
        positions (small, sensible nudges). Pass ``reseed_grid=True`` for
        a from-scratch re-place (ignores current positions) -- use only
        on an unplaced or badly-scrambled board.

        Args:
            designators: If given, ONLY these components are moved; every
                other placed component is held fixed but still attracts
                its nets and blocks overlaps. If omitted, all components
                are candidates (minus ``fixed``).
            fixed: Designators to pin in place (connectors, mounting
                holes, anything already positioned). Naming-agnostic --
                you choose which; the tool does not guess by prefix.
            region: Placement bounds ``{x1, y1, x2, y2}`` in mils. If
                omitted, the board outline's bounding rectangle is used.
            iterations: Global-placement relaxation steps (default 400).
                More iterations = better convergence, longer runtime.
            grid_mils: Snap grid for final positions (default 5).
            clearance_mils: Extra copper-to-copper breathing room added
                to each pair's half-extents (default 15).
            max_net_fanout: Skip nets touching more than this many
                components (default 0 = keep all). Set e.g. 20 to ignore
                power/ground planes that do not usefully guide placement.
            exclude_nets: Explicit net names to ignore (e.g. ``["GND"]``).
            reseed_grid: Re-place from a fresh grid instead of refining
                current positions (default False).
            optimize_rotation: Also choose part orientation to shorten
                pin-level wirelength (default True). Set False to keep
                every part's current rotation.
            apply: When True, commit the moves to the board. Default
                False (dry-run).

        Returns:
            Dict with:
              - ``dry_run``: bool (True unless ``apply`` and moves exist)
              - ``component_count``, ``movable_count``, ``fixed_count``
              - ``net_count`` (nets used after filtering)
              - ``pin_count`` (pads mapped to components for rotation)
              - ``hpwl_before``, ``hpwl_after``, ``hpwl_improvement_pct``
              - ``overlap_pairs_before``, ``overlap_pairs_after``
              - ``moved_count``, ``rotated_count`` and ``moves`` (each
                ``{designator, from: {x, y, rotation}, to: {x, y,
                rotation}}`` in mils/degrees; ``to.rotation`` is null
                when the orientation was unchanged)
              - ``region`` used, ``notes`` from the solver, and
                ``apply_result`` when ``apply=True``.
        """
        bridge = get_bridge()

        comp_resp = await bridge.send_command_async("pcb.get_components", {})
        raw_comps = comp_resp.get("components") if isinstance(comp_resp, dict) else None
        if not raw_comps:
            return {
                "error": "NO_COMPONENTS",
                "reason": "pcb.get_components returned no components",
                "raw": comp_resp,
            }

        # Resolve placement region from the board outline if not given.
        region_used: dict[str, float]
        if region and all(k in region for k in ("x1", "y1", "x2", "y2")):
            region_used = {k: float(region[k]) for k in ("x1", "y1", "x2", "y2")}
        else:
            outline = await bridge.send_command_async("pcb.get_board_outline", {})
            br = outline.get("bounding_rect") if isinstance(outline, dict) else None
            if not br:
                return {
                    "error": "NO_BOARD_OUTLINE",
                    "reason": "No board outline; pass an explicit region "
                    "{x1, y1, x2, y2} in mils.",
                    "raw": outline,
                }
            region_used = {
                "x1": float(br.get("left", 0)),
                "y1": float(br.get("bottom", 0)),
                "x2": float(br.get("right", 0)),
                "y2": float(br.get("top", 0)),
            }

        # Build the solver's component list. Capture each part's centroid,
        # origin, current rotation, bbox (for the pad->component spatial
        # join), and C0 = the centroid's offset from the origin at
        # rotation 0 -- needed to convert a solved (centroid, rotation)
        # back to an Altium move (which sets the origin and rotates about
        # it).
        def _is_orthogonal(deg: float) -> bool:
            return abs((deg % 90.0)) < 0.5 or abs((deg % 90.0) - 90.0) < 0.5

        fixed_set = {str(d).strip() for d in (fixed or []) if str(d).strip()}
        selected = {str(d).strip() for d in (designators or []) if str(d).strip()}
        geom: dict[str, dict[str, Any]] = {}
        place_comps: list[PlaceComp] = []
        comp_by_ref: dict[str, PlaceComp] = {}
        all_designators: list[str] = []
        for c in raw_comps:
            ref = str(c.get("designator", "")).strip()
            if not ref:
                continue
            all_designators.append(ref)
            bbox = c.get("bbox") or {}
            try:
                w = max(1.0, float(bbox.get("width", 0)))
                h = max(1.0, float(bbox.get("height", 0)))
                x1 = float(bbox.get("x1", 0))
                y1 = float(bbox.get("y1", 0))
                x2 = float(bbox.get("x2", 0))
                y2 = float(bbox.get("y2", 0))
                ox = float(c.get("x", 0))
                oy = float(c.get("y", 0))
                rot = float(c.get("rotation", 0) or 0)
            except (TypeError, ValueError):
                continue
            cx = (x1 + x2) / 2.0
            cy = (y1 + y2) / 2.0
            layer = str(c.get("layer", "Top")) or "Top"
            # C0 = back-rotated centroid-from-origin vector (rotation 0).
            c0 = rotate_offset(cx - ox, cy - oy, -rot)
            geom[ref] = {
                "cx": cx, "cy": cy, "ox": ox, "oy": oy, "rot": rot,
                "c0": c0,
                "bbox": (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)),
                "layer": layer,
            }
            # Fixed if explicitly pinned, or (when a selection is given)
            # not part of the selected set.
            is_fixed = ref in fixed_set or (bool(selected) and ref not in selected)
            pc = PlaceComp(
                ref=ref,
                w=w,
                h=h,
                cx=cx,
                cy=cy,
                layer=layer,
                fixed=is_fixed,
                rotation=rot,
            )
            place_comps.append(pc)
            comp_by_ref[ref] = pc

        if not place_comps:
            return {"error": "NO_PLACEABLE_COMPONENTS", "raw": comp_resp}

        # Build the net graph from the compiled netlist. One batch IPC.
        net_resp = await bridge.send_command_async(
            "project.get_connectivity_batch",
            {"designators": "~~".join(all_designators)},
            timeout=180.0,
        )
        exclude = {str(n).strip().upper() for n in (exclude_nets or [])}
        net_members: dict[str, list[str]] = {}
        comps_conn = net_resp.get("components") if isinstance(net_resp, dict) else None
        for c in comps_conn or []:
            ref = str(c.get("designator", "")).strip()
            if not ref:
                continue
            for pin in c.get("pins") or []:
                net = str(pin.get("net", "")).strip()
                if not net or net.upper() in exclude:
                    continue
                members = net_members.setdefault(net, [])
                if ref not in members:
                    members.append(ref)

        place_nets: list[PlaceNet] = []
        for net, members in net_members.items():
            if len(members) < 2:
                continue
            if max_net_fanout and len(members) > max_net_fanout:
                continue
            place_nets.append(PlaceNet(tuple(members), name=net))

        # Pin geometry: one board-wide pad query, then a spatial join (pad
        # center -> smallest containing component bbox) + back-rotation to
        # recover rotation-0 local offsets. Needed for rotation
        # optimization AND -- crucially -- to derive the net graph when
        # there is no compiled schematic netlist (a PCB-only / synced-
        # placement board, where connectivity lives on the pads, not in a
        # schematic). Only top-side, orthogonally-placed parts are made
        # rotatable.
        pin_count = 0
        netgraph_from_pads = False
        if optimize_rotation or not net_members:
            pad_resp = await bridge.send_command_async(
                "generic.query_objects",
                {"object_type": "ePadObject", "properties": "X,Y,Net",
                 "scope": "active_doc", "filter": ""},
                timeout=120.0,
            )
            pads = pad_resp.get("objects") if isinstance(pad_resp, dict) else None
            bbox_list = [(ref, geom[ref]["bbox"]) for ref in geom]
            pins_by_ref: dict[str, list[PlacePin]] = {}
            pad_net_members: dict[str, list[str]] = {}
            for pad in pads or []:
                try:
                    px = float(pad.get("X"))
                    py = float(pad.get("Y"))
                except (TypeError, ValueError):
                    continue
                net = str(pad.get("Net", "")).strip()
                if not net or net.upper() in exclude:
                    continue
                owner = None
                owner_area = None
                for ref, (bx1, by1, bx2, by2) in bbox_list:
                    if bx1 <= px <= bx2 and by1 <= py <= by2:
                        area = (bx2 - bx1) * (by2 - by1)
                        if owner_area is None or area < owner_area:
                            owner_area = area
                            owner = ref
                if owner is None:
                    continue
                g = geom[owner]
                lx, ly = rotate_offset(px - g["cx"], py - g["cy"], -g["rot"])
                pins_by_ref.setdefault(owner, []).append(PlacePin(lx, ly, net))
                m = pad_net_members.setdefault(net, [])
                if owner not in m:
                    m.append(owner)
            if optimize_rotation:
                for ref, pins in pins_by_ref.items():
                    pc = comp_by_ref[ref]
                    pc.pins = tuple(pins)
                    pc.rotatable = (
                        pc.layer.lower().startswith("top")
                        and _is_orthogonal(geom[ref]["rot"])
                    )
                    pin_count += len(pins)
            # No schematic netlist -> build the net graph from PCB pad nets
            # so the spring placement (and rotation) actually optimize.
            if not net_members and pad_net_members:
                net_members = pad_net_members
                netgraph_from_pads = True
                place_nets = []
                for net, members in net_members.items():
                    if len(members) < 2:
                        continue
                    if max_net_fanout and len(members) > max_net_fanout:
                        continue
                    place_nets.append(PlaceNet(tuple(members), name=net))

        options = PlaceOptions(
            iterations=max(0, int(iterations)),
            grid_mils=float(grid_mils),
            clearance_mils=float(clearance_mils),
            reseed_grid=bool(reseed_grid),
            optimize_rotation=bool(optimize_rotation),
        )
        region_obj = BoardRegion(
            region_used["x1"], region_used["y1"],
            region_used["x2"], region_used["y2"],
        )
        result = plan_placement(place_comps, place_nets, region_obj, options)

        # Convert each solved (centroid, rotation) back to an Altium move.
        # Altium sets the origin (Comp.x/y) and rotates the body about it,
        # so new_origin = target_centroid - R(newRot) * C0, where C0 is
        # the centroid's offset from the origin at rotation 0. Emit a move
        # when the part shifted past half the snap grid OR was re-oriented.
        threshold = max(1.0, float(grid_mils) / 2.0)
        moves: list[dict[str, Any]] = []
        for ref, (ncx, ncy) in result.positions.items():
            comp = comp_by_ref.get(ref)
            if comp is None or comp.fixed:
                continue
            g = geom[ref]
            new_rot = float(result.rotations.get(ref, g["rot"]))
            rotated = ref in result.rotated
            c0x, c0y = g["c0"]
            r0x, r0y = rotate_offset(c0x, c0y, new_rot)
            new_ox = int(round(ncx - r0x))
            new_oy = int(round(ncy - r0y))
            old_ox = int(round(g["ox"]))
            old_oy = int(round(g["oy"]))
            moved_xy = (abs(new_ox - old_ox) >= threshold
                        or abs(new_oy - old_oy) >= threshold)
            if not moved_xy and not rotated:
                continue
            moves.append({
                "designator": ref,
                "from": {"x": old_ox, "y": old_oy, "rotation": g["rot"]},
                "to": {"x": new_ox, "y": new_oy,
                       "rotation": new_rot if rotated else None},
            })

        hpwl_before = result.hpwl_before
        hpwl_after = result.hpwl_after
        improvement = (
            round((hpwl_before - hpwl_after) / hpwl_before * 100.0, 2)
            if hpwl_before > 0 else 0.0
        )
        movable_count = sum(1 for c in place_comps if not c.fixed)
        rotated_count = sum(1 for m in moves if m["to"]["rotation"] is not None)
        summary: dict[str, Any] = {
            "dry_run": True,
            "component_count": len(place_comps),
            "movable_count": movable_count,
            "fixed_count": len(place_comps) - movable_count,
            "net_count": len(place_nets),
            "net_graph_source": "pcb_pads" if netgraph_from_pads else "schematic",
            "pin_count": pin_count,
            "hpwl_before": round(hpwl_before, 1),
            "hpwl_after": round(hpwl_after, 1),
            "hpwl_improvement_pct": improvement,
            "overlap_pairs_before": result.overlap_pairs_before,
            "overlap_pairs_after": result.overlap_pairs_after,
            "moved_count": len(moves),
            "rotated_count": rotated_count,
            "moves": moves,
            "region": region_used,
            "notes": result.notes,
        }

        if apply and moves:
            # Pack as designator,x,y,rotation (empty rotation field leaves
            # the orientation unchanged).
            ops = []
            for m in moves:
                rot = m["to"]["rotation"]
                rot_str = "" if rot is None else str(rot)
                ops.append(
                    f"{m['designator']},{m['to']['x']},{m['to']['y']},{rot_str}"
                )
            apply_result = await bridge.send_command_async(
                "pcb.batch_move_components",
                {"moves": "|".join(ops)},
            )
            summary["dry_run"] = False
            summary["apply_result"] = apply_result
        elif apply and not moves:
            summary["apply_result"] = {"moves_applied": 0,
                                       "reason": "no moves to apply"}

        return summary

    @mcp.tool()
    async def pcb_copy_component_placement(
        mapping: dict[str, str],
        include_designator: bool = True,
        include_comment: bool = True,
    ) -> dict[str, Any]:
        """Clone placement from source components onto destination components.

        For each ``src -> dst`` pair, copy the source's PCB placement
        (layer, X, Y, rotation) onto the destination. With the optional
        flags also clone the designator-text placement (X/Y offset from
        component centre, rotation, size, width, layer, on/off) and the
        comment-text placement.

        Real-world use: a board with N identical channels (filter / amp /
        switching stages). Lay out channel 1 once, then call this tool
        with a mapping like ``{"R1": "R11", "C1": "C11", "U1": "U2"}``
        and channel 2 picks up channel 1's layout instantly. Saves the
        manual click-and-drag-drag-drag of replicating identical layouts.

        Mapping-driven rather than relying on sort-order matching of
        selections -- the agent-callable equivalent of a manual replicate.

        Args:
            mapping: ``{source_designator: dest_designator}``. Each
                source must already be placed somewhere; each dest gets
                overwritten with the source's placement.
            include_designator: Also copy designator-text placement
                (offset, rotation, size, layer, visibility). Default
                True.
            include_comment: Also copy comment-text placement. Default
                True.

        Returns:
            Dict with:
              - ``applied``: pairs that succeeded
              - ``failed``: pairs that failed (src/dst missing or apply
                exception)
              - ``items``: per-pair ``{src, dst, ok, error}``
        """
        if not mapping:
            return {"applied": 0, "failed": 0, "items": [],
                    "error": "mapping is empty"}
        pairs = "|".join(f"{s}={d}" for s, d in mapping.items())
        bridge = get_bridge()
        return await bridge.send_command_async(
            "pcb.copy_component_placement",
            {
                "mapping": pairs,
                "include_designator": "true" if include_designator else "false",
                "include_comment": "true" if include_comment else "false",
            },
        )

    @mcp.tool()
    async def pcb_place_stitching_vias(
        net: str,
        x1_mils: int,
        y1_mils: int,
        x2_mils: int,
        y2_mils: int,
        spacing_mils: int = 50,
        via_size_mils: int = 30,
        via_hole_mils: int = 14,
        clearance_mils: int = 10,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Place a grid of stitching vias on a named net within a rectangle.

        Standard RF / EMC tool: place GND stitch vias around high-speed
        traces so the return current has a low-inductance path between
        reference planes. Also used near connectors / clock generators
        / EMI-sensitive areas.

        Algorithm: walk a regular grid inside the rectangle; for each
        gridpoint, spatial-iterate within ``via_size_mils/2 +
        clearance_mils``. Skip the gridpoint if any non-same-net
        primitive (pad / via / track / arc) is in range. Otherwise
        create a top→bottom through-via on the target net.

        **The default is dry-run** because mutating a board with N×M
        new vias is risky. Confirm the ``placed`` / ``skipped`` count
        looks right, then call again with ``dry_run=False``.

        Args:
            net: Target net (must already exist on the board).
            x1_mils, y1_mils, x2_mils, y2_mils: Inclusive rectangle
                where the grid is placed. PCB origin is bottom-left
                by Altium convention.
            spacing_mils: Grid spacing (default 50). For GND stitching
                near a high-speed bus, /4 of the wavelength of the
                highest harmonic is a common rule of thumb.
            via_size_mils: Via pad diameter (default 30).
            via_hole_mils: Via drill diameter (default 14).
            clearance_mils: Min gap to any non-same-net existing
                primitive (default 10).
            dry_run: When True (default), only count would-be
                placements without mutating the board.

        Returns:
            Dict with ``{net, dry_run, placed, skipped, spacing_mils,
            clearance_mils}``.
        """
        bridge = get_bridge()
        return await bridge.send_command_async(
            "pcb.place_stitching_vias",
            {
                "net": net,
                "x1_mils": str(int(x1_mils)),
                "y1_mils": str(int(y1_mils)),
                "x2_mils": str(int(x2_mils)),
                "y2_mils": str(int(y2_mils)),
                "spacing_mils": str(int(spacing_mils)),
                "via_size_mils": str(int(via_size_mils)),
                "via_hole_mils": str(int(via_hole_mils)),
                "clearance_mils": str(int(clearance_mils)),
                "dry_run": "true" if dry_run else "false",
            },
            timeout=60.0,
        )

    @mcp.tool()
    async def pcb_calc_impedance(
        geometry: str,
        width_mils: float,
        dielectric_height_mils: float,
        dielectric_constant: float = 4.2,
        copper_oz: float = 1.0,
        spacing_mils: float = 0.0,
    ) -> dict[str, Any]:
        """Characteristic impedance of a PCB trace via IPC-2141 / Wadell
        closed-form approximations. Pure math, no Altium hit.

        Use this BEFORE routing a high-speed signal to pick the track
        width that matches your transceiver's target impedance (USB
        90 Ω diff, HDMI 100 Ω diff, single-ended PCIe 50 Ω, MIPI 100 Ω
        diff, etc). For controlled-impedance fab spec sheets, the
        accuracy of these closed-form formulas is ± ~10% -- good
        enough for picking widths but fab houses will refine the
        actual stackup. For real controlled-impedance boards, send
        the stackup to your fab and use their Polar Si9000 / similar
        report values.

        Chaining with the layer stack: call ``pcb_get_layer_stackup``
        first to read the actual dielectric heights and εr values
        from the project, then feed those into this calculator. This
        is essential -- guessing FR-4 at εr=4.2 ignores the stackup
        the user actually picked and can give a 15-20% off answer
        if they're using Megtron 6 or Rogers.

        Supported geometries:
          - ``"microstrip"`` -- single-ended outer-layer track with
            reference plane below. IPC-2141 microstrip formula.
          - ``"microstrip_diff"`` -- differential pair on outer
            layer. Adds Wadell's mutual-coupling correction.
          - ``"stripline"`` -- single-ended inner-layer track between
            two reference planes.
          - ``"stripline_diff"`` -- differential pair on an inner
            layer between two reference planes.

        Formula sources:
          - IPC-2141 microstrip:
            Z₀ = (87 / √(εr + 1.41)) × ln(5.98 × h / (0.8 × w + t))
          - Symmetric stripline:
            Z₀ = (60 / √εr) × ln(4 × b / (0.67 × π × (0.8 × w + t)))
          - Differential variants apply the standard Wadell
            correction: Zdiff ≈ 2 × Z₀ × (1 - 0.48 × exp(-0.96 × s/h))
            for microstrip, similarly with empirical coefficients
            for stripline.

        Args:
            geometry: One of ``"microstrip"``, ``"microstrip_diff"``,
                ``"stripline"``, ``"stripline_diff"``.
            width_mils: Track width in mils. For differential, this
                is the width of EACH conductor (not the pair).
            dielectric_height_mils: For microstrip, the dielectric
                thickness BELOW the trace (track-to-reference-plane).
                For stripline, this is the TOTAL dielectric thickness
                between the two reference planes (``b`` in IPC).
            dielectric_constant: εr of the dielectric. Common: 4.2
                (FR-4 at 1 GHz), 3.5 (Megtron 6), 3.0 (Rogers 4350B),
                2.2 (PTFE). Default 4.2.
            copper_oz: Copper weight in oz/ft² (used for ``t``,
                trace thickness). 1 oz = 1.378 mils. Default 1.0.
            spacing_mils: Edge-to-edge gap between conductors of a
                differential pair, in mils. Ignored for single-ended.

        Returns:
            Dict with:
              - ``geometry``, ``width_mils``, ``dielectric_height_mils``,
                ``dielectric_constant``, ``thickness_mils``,
                ``spacing_mils`` (when diff)
              - ``z0_ohms``: single-ended characteristic impedance
              - ``zdiff_ohms`` (diff geometries only): differential
                pair impedance
              - ``propagation_delay_ps_per_inch``: propagation delay,
                derived from the effective εr (microstrip uses
                εr_eff = (εr + 1)/2; stripline uses εr directly).
        """
        import math
        geometry = geometry.strip().lower()
        valid = ("microstrip", "microstrip_diff",
                 "stripline", "stripline_diff")
        if geometry not in valid:
            return {"ok": False,
                    "reason": "geometry must be one of " + ", ".join(valid)}
        if width_mils <= 0 or dielectric_height_mils <= 0:
            return {"ok": False,
                    "reason": "width_mils and dielectric_height_mils must be > 0"}
        if dielectric_constant <= 0:
            return {"ok": False,
                    "reason": "dielectric_constant must be > 0"}
        is_diff = geometry.endswith("_diff")
        if is_diff and spacing_mils <= 0:
            return {"ok": False,
                    "reason": "spacing_mils must be > 0 for differential geometries"}

        t = copper_oz * 1.378
        w = float(width_mils)
        h = float(dielectric_height_mils)
        er = float(dielectric_constant)

        if geometry.startswith("microstrip"):
            # IPC-2141 microstrip.
            z0 = (87.0 / math.sqrt(er + 1.41)) * \
                 math.log(5.98 * h / (0.8 * w + t))
            er_eff = (er + 1.0) / 2.0
        else:
            # Symmetric stripline (trace centered between two planes,
            # h is the FULL dielectric thickness between planes).
            z0 = (60.0 / math.sqrt(er)) * \
                 math.log(4.0 * h / (0.67 * math.pi * (0.8 * w + t)))
            er_eff = er

        # Propagation delay: c0 = 11.8 in/ns in vacuum;
        # tpd = sqrt(er_eff) / c0 = sqrt(er_eff) * 1000 / 11.8 ps/in
        tpd = math.sqrt(er_eff) * 1000.0 / 11.8

        out: dict[str, Any] = {
            "ok": True,
            "geometry": geometry,
            "width_mils": w,
            "dielectric_height_mils": h,
            "dielectric_constant": er,
            "thickness_mils": round(t, 3),
            "z0_ohms": round(z0, 1),
            "propagation_delay_ps_per_inch": round(tpd, 2),
        }
        if is_diff:
            s = float(spacing_mils)
            out["spacing_mils"] = s
            if geometry == "microstrip_diff":
                # Wadell microstrip diff approximation.
                zdiff = 2.0 * z0 * (1.0 - 0.48 * math.exp(-0.96 * s / h))
            else:
                # Stripline diff approximation.
                zdiff = 2.0 * z0 * (1.0 - 0.347 * math.exp(-2.9 * s / h))
            out["zdiff_ohms"] = round(zdiff, 1)
        return out

    @mcp.tool()
    async def pcb_calc_track_current_capacity(
        width_mils: float,
        copper_oz: float = 1.0,
        layer: str = "external",
        length_mils: float = 0.0,
    ) -> dict[str, Any]:
        """IPC-2221 current-carrying capacity for a PCB track.

        Pure math, no Altium hit -- safe to call without an attached
        session. Returns the current the track can sustain at each
        of several temperature rises (1, 5, 10, 20, 30 °C above
        ambient), plus DC resistance if a length is supplied.

        Formula (IPC-2221, also IPC-2152's curve-fit predecessor):
          ``I = k × (ΔT)^0.44 × (h × w)^0.725``
        where ``h`` and ``w`` are in mils, ``ΔT`` in °C, and:
          ``k = 0.048`` for external layers (top, bottom)
          ``k = 0.024`` for internal layers (mid)
        Internal-layer derating is the dominant correction; running a
        signal that draws 5 A on TopLayer might be fine but on an
        inner plane the same width only handles ~3 A at the same
        ΔT because heat dissipates more slowly.

        Copper thickness conversion: 1 oz/ft² ≈ 1.378 mils. So
        ``copper_oz=1.0`` → ``h = 1.378 mils``, ``copper_oz=2.0`` →
        ``h = 2.756 mils``.

        Resistance: ``R = ρ × L / (h × w)`` with ρ = 6.7 × 10⁻⁷ Ω-mil
        (annealed copper, 25 °C). Multiply current × resistance to
        get voltage drop across the track.

        Exposes the track-current math so the agent can apply it
        across an entire power net after pulling track widths from
        ``pcb_get_trace_lengths``.

        Args:
            width_mils: Track width in mils.
            copper_oz: Copper weight in oz/ft². Common values:
                0.5 oz (HDI inner), 1.0 oz (standard), 2.0 oz (power
                planes), 3.0 oz (heavy-current).
            layer: ``"external"`` (top / bottom) or ``"internal"``
                (mid / inner planes). Defaults to external.
            length_mils: If > 0, also returns DC resistance and
                voltage drop at each temperature rise.

        Returns:
            Dict with:
              - ``width_mils``, ``copper_oz``, ``thickness_mils``, ``layer``
              - ``current_amps`` (dict, keys ``1c``, ``5c``, ``10c``,
                ``20c``, ``30c``)
              - ``resistance_mohm`` (only if length_mils > 0)
              - ``voltage_drop_mv`` (only if length_mils > 0, dict
                keyed same as ``current_amps``)
        """
        if width_mils <= 0:
            return {"ok": False, "reason": "width_mils must be > 0"}
        if copper_oz <= 0:
            return {"ok": False, "reason": "copper_oz must be > 0"}
        layer_norm = layer.strip().lower()
        if layer_norm not in ("external", "internal"):
            return {"ok": False,
                    "reason": "layer must be 'external' or 'internal'"}

        thickness_mils = copper_oz * 1.378
        k = 0.024 if layer_norm == "internal" else 0.048
        b = 0.44
        c = 0.725
        hw_c = (thickness_mils * width_mils) ** c

        rises = (1, 5, 10, 20, 30)
        current = {f"{t}c": round(k * (t ** b) * hw_c, 3) for t in rises}
        out: dict[str, Any] = {
            "ok": True,
            "width_mils": width_mils,
            "copper_oz": copper_oz,
            "thickness_mils": round(thickness_mils, 3),
            "layer": layer_norm,
            "current_amps": current,
        }
        if length_mils > 0:
            rho = 6.7e-7
            r_ohm = rho * length_mils / (thickness_mils * width_mils)
            out["resistance_mohm"] = round(r_ohm * 1000.0, 4)
            out["voltage_drop_mv"] = {
                k_: round(current[k_] * r_ohm * 1000.0, 3) for k_ in current
            }
        return out

    @mcp.tool()
    async def pcb_add_testpoints_for_net_class(
        net_class: str,
        type: str = "smd",
        pad_size_mils: int = 40,
        hole_size_mils: int = 20,
        fab_top: bool = False,
        fab_bottom: bool = False,
        assy_top: bool = True,
        assy_bottom: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """For each net in a netclass that does NOT already have a
        testpoint, place a new pad above the board outline with the
        net assigned and the standard ``IsTestpoint`` /
        ``IsAssyTestpoint`` flags set.

        Typical workflow:
          1. Set up a netclass in Altium called ``TestPoints`` and
             add the signals you want probed (clocks, power rails,
             reset, debug signals).
          2. Call this tool with ``net_class="TestPoints"`` and the
             pad geometry your bed-of-nails fixture requires.
          3. Open the PCB; the new pads sit in a row above the
             board, with the right net + testpoint flags already set.
          4. Drag them into their final positions (or run
             ``pcb_move_components`` if you've pre-computed where
             they go).

        Coverage detection: a net is treated as "already covered" if
        any pad or via on it carries ANY of the four testpoint flags
        (so DFM tools, fab-side bed-of-nails vendors, and assembly-
        side probe vendors all see the same coverage). Pass
        ``force=True`` to override and place anyway.

        Agent-friendly: no GUI, results returned for follow-up.

        Args:
            net_class: Netclass name to scan.
            type: ``"smd"`` (top-layer SMD pad, no hole) or
                ``"through_hole"`` (multi-layer pad with drill).
            pad_size_mils: Outer pad size in mils. Default 40.
            hole_size_mils: Drill diameter, only used for through-hole
                testpoints. Default 20.
            fab_top: Set ``IsTestpoint_Top`` (top-side bed-of-nails).
            fab_bottom: Set ``IsTestpoint_Bottom``.
            assy_top: Set ``IsAssyTestpoint_Top`` (top-side assembly
                probe). Default True.
            assy_bottom: Set ``IsAssyTestpoint_Bottom``.
            force: Ignore existing testpoint coverage and always
                place a new pad.

        Returns:
            Dict with ``{net_class, type, placed, skipped_already_covered,
            placed_nets[], skipped_nets[]}``.
        """
        bridge = get_bridge()
        return await bridge.send_command_async(
            "pcb.add_testpoints_for_net_class",
            {
                "net_class": net_class,
                "type": type,
                "pad_size_mils": str(int(pad_size_mils)),
                "hole_size_mils": str(int(hole_size_mils)),
                "fab_top": "true" if fab_top else "false",
                "fab_bottom": "true" if fab_bottom else "false",
                "assy_top": "true" if assy_top else "false",
                "assy_bottom": "true" if assy_bottom else "false",
                "force": "true" if force else "false",
            },
            timeout=60.0,
        )

    @mcp.tool()
    async def pcb_make_paste_grid(
        designator: str,
        pad_name: str,
        min_grid_size_mils: int = 15,
        min_gap_mils: int = 5,
        min_coverage_pct: float = 60.0,
    ) -> dict[str, Any]:
        """Split a single pad's solder-paste opening into a grid of
        smaller fills. The classic use case is the central thermal
        pad on a QFN / DFN / QFP -- a full-area paste opening makes
        the IC "swim" sideways during reflow as the molten solder
        pool reduces friction. Splitting the opening into smaller
        squares totalling ~50-75% coverage gives the IC something
        to bond to while letting flux gases escape.

        Standard datasheet recommendations:
          - QFN / DFN exposed pad: 50-75% coverage, 0.3-0.5 mm
            squares with ≥ 0.2 mm gap (≈ 15 mils / 8 mils)
          - QFP heat-spreader: 60-70% coverage
          - Large connector body pads: 60% is usually enough

        ALWAYS consult the IC manufacturer's recommended stencil
        pattern before defaulting to this script -- some packages
        specify a star, diagonal, or windowpane pattern; this tool
        only does a regular grid.

        Args:
            designator: Component reference designator, e.g. ``"U5"``.
            pad_name: Pad identifier on that component. For QFN /
                DFN exposed pads this is usually ``"0"`` or the
                largest-numbered pad. Check the footprint first.
            min_grid_size_mils: Starting grid block size, in mils.
                Bumped up in 5-mil steps if coverage % is below
                target. Default 15 mils (≈ 0.38 mm).
            min_gap_mils: Minimum spacing between grid blocks, in
                mils. Default 5 mils (≈ 0.13 mm).
            min_coverage_pct: Target paste coverage as a percentage
                of pad area, 0-100. Default 60. Iterates grid size
                upward until met.

        Returns:
            Dict with ``{designator, pad_name, grid_x, grid_y,
            grid_size_mils, gap_x_mils, gap_y_mils, fills_placed,
            coverage_pct}``. The existing full-area paste opening is
            suppressed via ``PasteMaskExpansion = -pad_dimension`` on
            the pad cache before the grid fills are laid down.
        """
        bridge = get_bridge()
        return await bridge.send_command_async(
            "pcb.make_paste_grid",
            {
                "designator": designator,
                "pad_name": pad_name,
                "min_grid_size_mils": str(int(min_grid_size_mils)),
                "min_gap_mils": str(int(min_gap_mils)),
                "min_coverage_pct": str(float(min_coverage_pct)),
            },
            timeout=30.0,
        )

    @mcp.tool()
    async def pcb_get_differential_pairs() -> dict[str, Any]:
        """Enumerate every ``IPCB_DifferentialPair`` on the active PCB
        and report each pair's two halves with length skew.

        Length mismatch between the two halves of a diff pair is one
        of the most common high-speed routing bugs. Transceiver
        specs:
          - **USB 2.0**: skew < 150 mils
          - **USB 3.x SuperSpeed**: skew < 5 mils per pair
          - **HDMI**: skew < 200 mils within pair, < 800 mils between
          - **MIPI D-PHY (high-rate)**: skew < 5 mils
          - **PCIe Gen3+**: skew < 5 mils within lane
          - **Ethernet PHY**: typically < 50 mils
        Catching skew violations pre-fab saves a respin -- the agent
        should call this near the end of a layout review.

        Returns one row per pair with:
          - ``name``: pair name (e.g. ``"USB_DATA"`` if the SCH had
            ``.DiffPair`` directives ``USB_DATA_P`` / ``USB_DATA_N``)
          - ``positive_net`` / ``negative_net``: the two member nets
          - ``pos_length_mils`` / ``neg_length_mils``: summed track +
            arc length for each half (signal layers only)
          - ``skew_mils``: absolute difference, the number you compare
            against the transceiver spec
          - ``both_routed``: false means one half is still ratsnest-
            only (lengths are unreliable in that state)

        Uses the PCB API ``IPCB_DifferentialPair`` interface.

        Returns:
            Dict with ``{count, pairs: [...]}``.
        """
        bridge = get_bridge()
        return await bridge.send_command_async(
            "pcb.get_differential_pairs", {}, timeout=30.0,
        )

    @mcp.tool()
    async def pcb_clear_source_footprint_library(
        designator_filter: list[str] | None = None,
    ) -> dict[str, Any]:
        """Clear ``Comp.SourceFootprintLibrary`` on board components.

        Background: when a project is created from an Integrated
        Library, every placed component remembers WHICH library it
        came from. If the user later renames, moves, or consolidates
        that library, ECO and "Update From Library" start failing
        with "library not found" warnings because the component
        still points at the stale path. Clearing
        ``SourceFootprintLibrary`` unpins it so Altium re-matches by
        library-reference name from whatever's currently in
        Available Libraries.

        This is a standard step when consolidating multiple legacy
        libraries into a single curated library: drop the new lib
        into Available Libraries, call this tool, then run an ECO --
        the components now bind to the new library.

        Generalized with an optional filter.

        Args:
            designator_filter: Restrict to these designators
                (e.g. ``["U1", "U5"]``). ``None`` / empty clears all
                components on the board. Match is case-sensitive
                whole-string.

        Returns:
            Dict with ``{total, cleared}``. ``total`` counts components
            inspected after filter; ``cleared`` counts ones that
            actually had a non-empty SourceFootprintLibrary cleared.
        """
        bridge = get_bridge()
        params: dict[str, str] = {}
        if designator_filter:
            params["designator_filter"] = "|".join(
                str(d) for d in designator_filter)
        return await bridge.send_command_async(
            "pcb.clear_source_footprint_library", params, timeout=30.0,
        )

    @mcp.tool()
    async def pcb_get_fab_stats() -> dict[str, Any]:
        """DFM (Design For Manufacturing) summary -- the numbers fab
        houses ask for on their quote forms.

        One IPC call returns:
          - ``board_width_mm``, ``board_height_mm``, ``board_area_mm2``
          - ``num_copper_layers``
          - ``vias_total``, ``vias_through``, ``vias_blind``,
            ``vias_buried``
          - ``pads_plated``, ``pads_unplated``, ``pads_slotted``
          - ``min_annular_ring_mils`` -- across all plated holes
            (vias + plated pads). Most fabs need ≥ 4 mil for the
            standard process; sub-4-mil bumps to HDI.
          - ``min_track_width_mils`` -- minimum across all copper
            layers. Standard fabs offer 4-5 mil; sub-3 mil is HDI.
          - ``smallest_hole_mils``, ``largest_hole_mils``,
            ``distinct_hole_count`` -- the drill bit story. Fab
            houses charge per distinct drill diameter; collapsing
            from 7 distinct sizes to 4 can shave noticeable cost.

        Useful before sending gerbers to the fab: the agent can spot
        sub-process-minimum values and warn the user. Aspect-ratio
        check (depth / drill) is NOT included here because it
        requires layer-stack walking; if you need it, fetch
        ``pcb_get_layer_stackup`` separately.

        Returns:
            Dict with all keys above. All linear measurements use the
            stated unit suffix (``_mm`` vs ``_mils``).
        """
        bridge = get_bridge()
        return await bridge.send_command_async("pcb.get_fab_stats", {})

    @mcp.tool()
    async def pcb_lock_net_routing(
        nets: list[str],
        lock: bool,
        lock_components: bool = False,
    ) -> dict[str, Any]:
        """Lock or unlock track / arc / via primitives on a list of nets.

        Standard workflow: before letting the autorouter touch the board,
        lock the power / ground / clock / oscillator nets so a partial
        re-route pass doesn't undo your careful hand-routing on them.
        Set ``lock=False`` afterwards to free everything up.

        Locked primitives have ``Moveable=False``; the autorouter and
        the interactive editor respect that flag when DXP Preferences →
        PCB Editor → General → "Protect Locked Objects" is enabled.

        The net-list-driven MCP equivalent of a cursor-driven lock.

        Args:
            nets: Net names to lock / unlock (e.g. ``["VCC", "GND",
                "CLK_24M"]``). Case-sensitive.
            lock: True to lock (sets Moveable=False), False to unlock.
            lock_components: Also lock any component with at least one
                pad on a matched net. Useful to freeze the connectors /
                regulators that anchor a hand-routed power section.
                Default False.

        Returns:
            Dict with:
              - ``locked``: True / False mirror of the input
              - ``matched_primitives``: track + arc + via objects whose
                net matched
              - ``updated_primitives``: how many actually changed state
              - ``matched_components``: components touched (only filled
                when ``lock_components=True``)
              - ``updated_components``: components whose Moveable flipped
        """
        if not nets:
            return {"ok": False, "reason": "nets list is empty"}
        bridge = get_bridge()
        return await bridge.send_command_async(
            "pcb.lock_net_routing",
            {
                "nets": "|".join(str(n) for n in nets),
                "lock": "true" if lock else "false",
                "lock_components": "true" if lock_components else "false",
            },
        )

    @mcp.tool()
    async def pcb_set_text_visibility(
        designators: bool | None = None,
        comments: bool | None = None,
        filter: list[str] | None = None,
    ) -> dict[str, Any]:
        """Bulk-toggle component designator and / or comment visibility.

        For the common workflows:
          - Hide all designators before generating an assembly PDF.
          - Show comments during a value-review pass.
          - Hide just the noisy 0402 designators while keeping IC labels.

        Args:
            designators: If True/False, set ``Component.NameOn`` for
                matched components. ``None`` leaves designator visibility
                alone.
            comments: If True/False, set ``Component.CommentOn``. ``None``
                leaves comment visibility alone.
            filter: Restrict to these designator names (e.g. ``["U1",
                "U2", "C5"]``). ``None`` applies to every component.
                Match is case-sensitive whole-string.

        At least one of ``designators`` / ``comments`` must be supplied.

        Returns:
            Dict with:
              - ``matched``: components inspected (after filter)
              - ``updated_names``: components whose NameOn flipped
              - ``updated_comments``: components whose CommentOn flipped
        """
        if designators is None and comments is None:
            return {"ok": False,
                    "reason": ("supply at least one of designators / "
                               "comments")}
        bridge = get_bridge()
        params: dict[str, str] = {}
        if designators is not None:
            params["designators"] = "true" if designators else "false"
        if comments is not None:
            params["comments"] = "true" if comments else "false"
        if filter:
            params["filter"] = "|".join(str(d) for d in filter)
        return await bridge.send_command_async(
            "pcb.set_text_visibility", params,
        )

    @mcp.tool()
    async def pcb_check_placement_collision(
        designator: str,
        x: int,
        y: int,
        rotation: Optional[float] = None,
        margin_mils: int = 0,
    ) -> dict[str, Any]:
        """Dry-run check whether moving a component to (x, y[, rotation])
        would overlap any other placed component on the same side.

        DOES NOT actually move the component. Computes the predicted
        axis-aligned bounding box at the proposed pose, then AABB-tests
        against every other component's current bounding rect. Use this
        BEFORE every `pcb_move_component` / `pcb_move_components` call
        when placing parts on a board that already has placed parts.

        Args:
            designator: Component to test (must exist on the board).
            x: Proposed X position in mils (component reference point).
            y: Proposed Y position in mils.
            rotation: Proposed rotation in degrees. When None the target's
                current rotation is used. Quarter-turn deltas (~+/-90 from
                current) swap the AABB dimensions; other angles use the
                current-orientation bbox as an approximation.
            margin_mils: Extra clearance to require around the target.
                Default 0 (touching bounding boxes count as collision).

        Returns:
            Dict with:
              - designator: target.
              - proposed: {x, y, rotation, bbox:{x1,y1,x2,y2}, margin_mils}.
              - clear: True if no collisions, False otherwise.
              - colliding_count: number of collisions.
              - colliding: list of {designator, bbox:{...}} per collider.

        Caveats:
            - AABB only; rotated non-square footprints will report an
              inflated bbox.
            - Same-side check is automatic; the target's bbox is compared
              only against components on the same layer (Top vs Bottom).
            - Does not detect courtyard violations or pad-to-pad clearances,
              only solid-box overlap. Use DRC for actual clearance rules.
        """
        bridge = get_bridge()
        params: dict[str, Any] = {
            "designator": designator,
            "x": str(int(x)),
            "y": str(int(y)),
            "margin_mils": str(int(margin_mils)),
        }
        if rotation is not None:
            params["rotation"] = str(rotation)
        return await bridge.send_command_async(
            "pcb.check_placement_collision", params,
        )

    @mcp.tool()
    async def pcb_get_trace_lengths(
        net: str = "",
    ) -> dict[str, Any]:
        """Get total routed track length per net on the active PCB.

        Sums all track segment lengths for each net. Useful for length
        matching analysis and checking differential pair balance.

        Args:
            net: Optional net name filter. If provided, only returns the
                 length for that specific net. Empty = all nets.

        Returns:
            Dictionary with "trace_lengths" array (each with net name and
            length_mils) and "net_count"
        """
        bridge = get_bridge()
        params: dict[str, Any] = {}
        if net:
            params["net"] = net
        result = await bridge.send_command_async("pcb.get_trace_lengths", params)
        return result

    @mcp.tool()
    async def pcb_get_layer_stackup() -> dict[str, Any]:
        """Get the full PCB layer stackup information.

        Returns copper layers with thickness, dielectric type/height/constant,
        and board name.

        Returns:
            Dictionary with "layers" array (each with name, order,
            copper_thickness_mils, dielectric_type, dielectric_height_mils,
            dielectric_constant), "layer_count", and "board_name"
        """
        bridge = get_bridge()
        result = await bridge.send_command_async("pcb.get_layer_stackup", {})
        return result

    @mcp.tool()
    async def pcb_add_layer(layer: str) -> dict[str, Any]:
        """Insert a copper layer into the PCB layer stack.

        Calls IPCB_LayerStack.InsertLayer with the requested TLayer enum.
        Valid names include MidLayer1..MidLayer30 (signal layers) and
        InternalPlane1..InternalPlane16 (power / ground planes). Top / Bottom
        are always present, they cannot be added.

        Args:
            layer: Layer name, e.g. "MidLayer1", "InternalPlane1"

        Returns:
            Dictionary confirming the layer was inserted.
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.add_layer", {"layer": layer}
        )
        return result

    @mcp.tool()
    async def pcb_remove_layer(layer: str) -> dict[str, Any]:
        """Remove a copper layer from the PCB layer stack.

        Calls IPCB_LayerStack.RemoveFromStack on the requested layer.
        Does nothing if the layer is not currently in the stack.

        Args:
            layer: Layer name, e.g. "MidLayer1", "InternalPlane2"

        Returns:
            Dictionary confirming the layer was removed.
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.remove_layer", {"layer": layer}
        )
        return result

    @mcp.tool()
    async def pcb_modify_layer(
        layer: str,
        name: str = "",
        copper_thickness_mils: int = 0,
        dielectric_type: str = "",
        dielectric_height_mils: int = 0,
        dielectric_constant: float = 0.0,
        dielectric_material: str = "",
    ) -> dict[str, Any]:
        """Tune properties on an existing copper layer.

        Every optional parameter is applied only if provided (non-empty
        string / non-zero number). Maps to:
          name                   IPCB_LayerObject.Name
          copper_thickness_mils  IPCB_LayerObject.CopperThickness
          dielectric_type        Dielectric.DielectricType
                                 (one of "none", "core", "prepreg", "surface")
          dielectric_height_mils Dielectric.DielectricHeight
          dielectric_constant    Dielectric.DielectricConstant
          dielectric_material    Dielectric.DielectricMaterial

        Args:
            layer: Target layer name, e.g. "MidLayer1"
            name: New layer name (optional)
            copper_thickness_mils: New copper thickness in mils (optional)
            dielectric_type: "none" | "core" | "prepreg" | "surface" (optional)
            dielectric_height_mils: New dielectric height in mils (optional)
            dielectric_constant: New Dk value (optional)
            dielectric_material: New dielectric material string (optional)

        Returns:
            Dictionary confirming the changes.
        """
        bridge = get_bridge()
        params: dict[str, str] = {"layer": layer}
        if name:
            params["name"] = name
        if copper_thickness_mils:
            params["copper_thickness_mils"] = str(copper_thickness_mils)
        if dielectric_type:
            params["dielectric_type"] = dielectric_type
        if dielectric_height_mils:
            params["dielectric_height_mils"] = str(dielectric_height_mils)
        if dielectric_constant:
            params["dielectric_constant"] = str(dielectric_constant)
        if dielectric_material:
            params["dielectric_material"] = dielectric_material
        result = await bridge.send_command_async("pcb.modify_layer", params)
        return result

    @mcp.tool()
    async def pcb_get_board_outline() -> dict[str, Any]:
        """Get the board outline vertices and bounding rectangle.

        Returns outline geometry as a list of vertices (line segments and
        arcs) plus the bounding rectangle dimensions.

        Returns:
            Dictionary with "point_count", "vertices" array (each with
            index, kind, x, y, and optionally cx, cy, angle1, angle2 for arcs),
            and "bounding_rect" (left, bottom, right, top in mils)
        """
        bridge = get_bridge()
        result = await bridge.send_command_async("pcb.get_board_outline", {})
        return result

    @mcp.tool()
    async def pcb_get_selected_objects(
        properties: str = "ObjectId,X,Y,Layer,Net",
    ) -> dict[str, Any]:
        """Get properties of currently selected objects on the active PCB.

        Args:
            properties: Comma-separated property names to return.
                Available: ObjectId, X, Y, X1, Y1, X2, Y2, Layer, Net,
                Width, Name, Rotation, HoleSize, TopXSize, TopYSize,
                Size, Pattern, SourceDesignator, Text, Descriptor, Selected

        Returns:
            Dictionary with "objects" array and "count"
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.get_selected_objects",
            {"properties": properties},
        )
        return result

    @mcp.tool()
    async def pcb_set_layer_visibility(
        layer: str,
        visible: bool = True,
    ) -> dict[str, Any]:
        """Show or hide a specific PCB layer.

        Args:
            layer: Layer name string, e.g.:
                Copper: "TopLayer", "BottomLayer", "MidLayer1"-"MidLayer30"
                Overlay: "TopOverlay", "BottomOverlay"
                Mask: "TopPaste", "BottomPaste", "TopSolder", "BottomSolder"
                Plane: "InternalPlane1"-"InternalPlane16"
                Other: "DrillGuide", "DrillDrawing", "MultiLayer",
                       "KeepOutLayer", "Mechanical1"-"Mechanical16"
            visible: True to show, False to hide

        Returns:
            Dictionary with layer name and visibility state
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.set_layer_visibility",
            {"layer": layer, "visible": "true" if visible else "false"},
        )
        return result

    @mcp.tool()
    async def pcb_repour_polygons() -> dict[str, Any]:
        """Repour all polygon pours on the active PCB.

        Triggers a full repour of all polygon copper pours, which
        recalculates thermal reliefs and clearances.

        Returns:
            Dictionary confirming repour completed
        """
        bridge = get_bridge()
        result = await bridge.send_command_async("pcb.repour_polygons", {})
        return result

    @mcp.tool()
    async def pcb_set_board_shape(
        x1: int,
        y1: int,
        x2: int,
        y2: int,
    ) -> dict[str, Any]:
        """Define the physical PCB board outline as a rectangle.

        Overwrites the current board shape. Use this right after creating a
        new PCB document to establish the board size before placing parts.
        Coordinates are in mils; (x1,y1) and (x2,y2) are opposite corners in
        any order.

        Args:
            x1: First corner X in mils
            y1: First corner Y in mils
            x2: Opposite corner X in mils
            y2: Opposite corner Y in mils

        Returns:
            Dictionary confirming the new outline rectangle
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.set_board_shape",
            {"x1": str(x1), "y1": str(y1), "x2": str(x2), "y2": str(y2)},
        )
        return result

    @mcp.tool()
    async def pcb_place_polygon_rect(
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        net: str = "",
        layer: str = "TopLayer",
        pour_over: bool = True,
    ) -> dict[str, Any]:
        """Drop a copper polygon pour on a rectangular area.

        Useful for placing a ground plane or power plane: pass the board's
        corners, the layer, and the net name (typically "GND"). Set
        pour_over=False to force the pour around same-net tracks/pads
        instead of covering them.

        Args:
            x1: First corner X in mils
            y1: First corner Y in mils
            x2: Opposite corner X in mils
            y2: Opposite corner Y in mils
            net: Net name to assign (empty = no-net fill, unusual)
            layer: Copper layer (default "TopLayer")
            pour_over: Pour over same-net objects (default True)

        Returns:
            Dictionary confirming the polygon placement
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.place_polygon_rect",
            {
                "x1": str(x1),
                "y1": str(y1),
                "x2": str(x2),
                "y2": str(y2),
                "net": net,
                "layer": layer,
                "pour_over": "true" if pour_over else "false",
            },
        )
        return result

    @mcp.tool()
    async def pcb_place_via_array(
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        pitch: int = 50,
        net: str = "",
        size: int = 30,
        hole_size: int = 12,
        low_layer: str = "TopLayer",
        high_layer: str = "BottomLayer",
    ) -> dict[str, Any]:
        """Stitch vias in a regular grid across a rectangle.

        Typical use: GND stitching between top and bottom layer copper
        pours. Places vias at every (pitch × pitch) grid intersection
        inside the rectangle.

        Args:
            x1: First corner X in mils
            y1: First corner Y in mils
            x2: Opposite corner X in mils
            y2: Opposite corner Y in mils
            pitch: Grid spacing in mils (default 50)
            net: Net to assign (typically "GND"; empty = no net)
            size: Via pad diameter in mils (default 30)
            hole_size: Drill hole diameter in mils (default 12)
            low_layer: Start layer (default "TopLayer")
            high_layer: End layer (default "BottomLayer")

        Returns:
            Dictionary with count of vias placed and rectangle/pitch echo
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.place_via_array",
            {
                "x1": str(x1),
                "y1": str(y1),
                "x2": str(x2),
                "y2": str(y2),
                "pitch": str(pitch),
                "net": net,
                "size": str(size),
                "hole_size": str(hole_size),
                "low_layer": low_layer,
                "high_layer": high_layer,
            },
        )
        return result

    @mcp.tool()
    async def pcb_place_via(
        x: int,
        y: int,
        net: str = "",
        size: int = 50,
        hole_size: int = 28,
        low_layer: str = "TopLayer",
        high_layer: str = "BottomLayer",
    ) -> dict[str, Any]:
        """Place a via at specific coordinates on the active PCB.

        Args:
            x: Via X position in mils
            y: Via Y position in mils
            net: Net name to assign (optional, empty = no net)
            size: Via pad diameter in mils (default 50)
            hole_size: Drill hole diameter in mils (default 28)
            low_layer: Start layer (default "TopLayer")
            high_layer: End layer (default "BottomLayer")

        Returns:
            Dictionary with placed via position and size
        """
        bridge = get_bridge()
        params: dict[str, Any] = {
            "x": str(x),
            "y": str(y),
            "size": str(size),
            "hole_size": str(hole_size),
            "low_layer": low_layer,
            "high_layer": high_layer,
        }
        if net:
            params["net"] = net
        result = await bridge.send_command_async("pcb.place_via", params)
        return result

    @mcp.tool()
    async def pcb_place_track(
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        width: int = 10,
        layer: str = "TopLayer",
        net_name: str = "",
    ) -> dict[str, Any]:
        """Place ONE track segment on the active PCB.

        IMPORTANT: If you are about to place more than one segment
        (multi-segment manhattan routes, a whole net, a batch of
        traces), use `pcb_place_tracks` instead, it takes a list of
        segments and runs them in a single IPC round-trip, which is
        dramatically faster than calling this tool repeatedly.

        Args:
            x1: Start X position in mils
            y1: Start Y position in mils
            x2: End X position in mils
            y2: End Y position in mils
            width: Track width in mils (default 10)
            layer: PCB layer name (default "TopLayer"). Options:
                "TopLayer", "BottomLayer", "MidLayer1"-"MidLayer30"
            net_name: Net name to assign (optional, empty = no net)

        Returns:
            Dictionary with placed track coordinates, width, and layer
        """
        bridge = get_bridge()
        params: dict[str, Any] = {
            "x1": str(x1),
            "y1": str(y1),
            "x2": str(x2),
            "y2": str(y2),
            "width": str(width),
            "layer": layer,
        }
        if net_name:
            params["net_name"] = net_name
        result = await bridge.send_command_async("pcb.place_track", params)
        hint = BulkHintTracker.record_and_hint("pcb_place_track")
        if hint and isinstance(result, dict):
            result["_hint_bulk"] = hint
        return result

    @mcp.tool()
    async def pcb_place_tracks(
        tracks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Place many track segments on the active PCB in ONE IPC round-trip.

        PREFER THIS over looping `pcb_place_track` whenever you have
        more than one segment to place. The whole batch is wrapped in
        a single PreProcess/PostProcess and a single save, so 50
        tracks take roughly the same wall time as 1. Typical uses:
        routing a full net, laying down a whole stitch pattern,
        replicating a motif, drawing a keepout rectangle.

        Args:
            tracks: List of track dicts. Each dict supports:
                x1, y1, x2, y2 (required, mils)
                width (default 10), layer (default "TopLayer"),
                net_name (optional, empty = no net)

            Example:
                [
                  {"x1": 5010, "y1": 4785, "x2": 5070, "y2": 4785,
                   "width": 10, "net_name": "NetC8_2"},
                  {"x1": 5070, "y1": 4785, "x2": 5070, "y2": 4862,
                   "width": 10, "net_name": "NetC8_2"},
                ]

        Returns:
            Dictionary with "placed" and "failed" counts
        """
        parts = []
        for t in tracks:
            x1 = int(t["x1"])
            y1 = int(t["y1"])
            x2 = int(t["x2"])
            y2 = int(t["y2"])
            width = int(t.get("width", 10))
            layer = str(t.get("layer", "TopLayer"))
            net = str(t.get("net_name", ""))
            parts.append(f"{x1},{y1},{x2},{y2},{width},{layer},{net}")
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.place_tracks", {"tracks": "|".join(parts)}
        )
        return result

    @mcp.tool()
    async def pcb_place_arc(
        x_center: int,
        y_center: int,
        radius: int,
        start_angle: float = 0,
        end_angle: float = 360,
        width: int = 10,
        layer: str = "TopLayer",
    ) -> dict[str, Any]:
        """Place an arc on the active PCB.

        Creates a circular arc segment defined by center, radius, and
        angular range.

        Args:
            x_center: Arc center X position in mils
            y_center: Arc center Y position in mils
            radius: Arc radius in mils
            start_angle: Start angle in degrees (default 0)
            end_angle: End angle in degrees (default 360 = full circle)
            width: Arc line width in mils (default 10)
            layer: PCB layer name (default "TopLayer")

        Returns:
            Dictionary with placed arc geometry and layer
        """
        bridge = get_bridge()
        params: dict[str, Any] = {
            "x_center": str(x_center),
            "y_center": str(y_center),
            "radius": str(radius),
            "start_angle": str(start_angle),
            "end_angle": str(end_angle),
            "width": str(width),
            "layer": layer,
        }
        result = await bridge.send_command_async("pcb.place_arc", params)
        return result

    @mcp.tool()
    async def pcb_place_text(
        text: str,
        x: int,
        y: int,
        layer: str = "TopOverlay",
        height: int = 60,
        rotation: float = 0,
    ) -> dict[str, Any]:
        """Place a text string on the active PCB.

        Args:
            text: Text content to place
            x: Text X position in mils
            y: Text Y position in mils
            layer: PCB layer name (default "TopOverlay"). Common choices:
                "TopOverlay", "BottomOverlay", "TopLayer", "BottomLayer",
                "Mechanical1"-"Mechanical16"
            height: Text height in mils (default 60)
            rotation: Rotation angle in degrees (default 0)

        Returns:
            Dictionary with placed text properties
        """
        bridge = get_bridge()
        params: dict[str, Any] = {
            "text": text,
            "x": str(x),
            "y": str(y),
            "layer": layer,
            "height": str(height),
            "rotation": str(rotation),
        }
        result = await bridge.send_command_async("pcb.place_text", params)
        return result

    @mcp.tool()
    async def pcb_place_fill(
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        layer: str = "TopLayer",
        net_name: str = "",
    ) -> dict[str, Any]:
        """Place a rectangular copper fill on the active PCB.

        Creates a solid copper rectangle. Useful for thermal pads,
        ground planes, and copper pours in specific areas.

        Args:
            x1: First corner X in mils
            y1: First corner Y in mils
            x2: Second corner X in mils
            y2: Second corner Y in mils
            layer: PCB layer name (default "TopLayer")
            net_name: Net name to assign (optional, empty = no net)

        Returns:
            Dictionary with placed fill coordinates and layer
        """
        bridge = get_bridge()
        params: dict[str, Any] = {
            "x1": str(x1),
            "y1": str(y1),
            "x2": str(x2),
            "y2": str(y2),
            "layer": layer,
        }
        if net_name:
            params["net_name"] = net_name
        result = await bridge.send_command_async("pcb.place_fill", params)
        return result

    @mcp.tool()
    async def pcb_start_polygon_placement(
        layer: str = "TopLayer",
        net_name: str = "",
    ) -> dict[str, Any]:
        """Start INTERACTIVE polygon pour placement on the active PCB.

        This is an interactive command, it launches Altium's polygon
        placement mode. The user must then draw the polygon boundary
        interactively in Altium Designer (clicks define vertices, right-click
        or Escape completes). It does NOT create a polygon programmatically
        from coordinates.

        Args:
            layer: Target copper layer (default "TopLayer")
            net_name: Net to assign to the polygon pour (optional)

        Returns:
            Dictionary confirming polygon placement mode was initiated
        """
        bridge = get_bridge()
        params: dict[str, Any] = {"layer": layer}
        if net_name:
            params["net_name"] = net_name
        result = await bridge.send_command_async("pcb.start_polygon_placement", params)
        return result

    @mcp.tool()
    async def pcb_create_design_rule(
        name: str,
        rule_type: str = "clearance",
        value: int = 10,
        max_value: Optional[int] = None,
        favored_value: Optional[int] = None,
        max_uncoupled_length: Optional[int] = None,
        scope: str = "",
        net_scope: str = "different_nets",
    ) -> dict[str, Any]:
        """Create a new design rule on the active PCB.

        Args:
            name: Rule name (e.g., "Min Clearance 6mil").
            rule_type: Type of rule to create. Options:
                ``"clearance"`` - Electrical clearance (value = gap in mils).
                ``"width"`` - Track width constraint (value = min width
                    in mils; max_value / favored_value optional).
                ``"via_size"`` - Hole size constraint (value = min hole
                    in mils; max_value optional).
                ``"differential_pairs"`` - Differential-pair routing rule
                    (value = min gap, max_value = max gap,
                    favored_value = preferred gap, max_uncoupled_length =
                    max uncoupled length in mils).
            value: Rule's primary value in mils. For width / via_size /
                differential_pairs this is the MIN side. Default 10.
            max_value: For width / via_size / differential_pairs. When
                supplied, sets the MAX side independently of value. When
                None, falls back to 5x value (legacy default).
            favored_value: For width / differential_pairs. Sets the
                preferred value. Defaults to value when None.
            max_uncoupled_length: For differential_pairs only.
                MaxUncoupledLength in mils (single scalar, not per-layer).
                Defaults to 1000 mils when None.
            scope: Optional query expression for Scope1. Pick the
                predicate that matches the rule kind:
                  - net-based rules (clearance, width, via_size):
                    ``"InNet('GND')"``, ``"InNetClass('Power')"``,
                    ``"All"``.
                  - differential_pairs: ``"InDifferentialPair('USB')"``,
                    ``"InDifferentialPairClass('HighSpeed')"``,
                    ``"IsDifferentialPair"`` (matches any diff pair),
                    or ``"All"``.
                Mixing predicates across kinds (e.g., ``InNet`` on a
                differential_pairs rule) creates a rule that never matches.
            net_scope: Which nets the rule applies between. Options:
                ``"different_nets"`` (default) for Clearance rules;
                ``"any_net"`` for all-pairs; ``"same_net"`` for same-net
                only. Has no effect on differential_pairs.

        Returns:
            Dictionary with created rule details.

        Note: Creating ComponentClearance, HoleToHoleClearance,
        BoardOutlineClearance, and other newer rule kinds is not
        supported on this Altium build because the relevant symbolic
        constants (eRule_ComponentClearance, ...) are not exposed in
        DelphiScript. Existing rules of those kinds can be UPDATED via
        ``pcb_set_rule_properties`` (gap_mils field), which dispatches
        through Rule.RuleKind without needing the symbol.
        """
        bridge = get_bridge()
        params: dict[str, Any] = {
            "name": name,
            "rule_type": rule_type,
            "value": str(value),
            "net_scope": net_scope,
        }
        if max_value is not None:
            params["max_value"] = str(max_value)
        if favored_value is not None:
            params["favored_value"] = str(favored_value)
        if max_uncoupled_length is not None:
            params["max_uncoupled_length"] = str(max_uncoupled_length)
        if scope:
            params["scope"] = scope
        result = await bridge.send_command_async("pcb.create_design_rule", params)
        return result

    @mcp.tool()
    async def pcb_delete_design_rule(
        name: str,
    ) -> dict[str, Any]:
        """Delete a design rule by name from the active PCB.

        Args:
            name: Exact name of the design rule to delete

        Returns:
            Dictionary confirming deletion
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.delete_design_rule",
            {"name": name},
        )
        return result

    @mcp.tool()
    async def pcb_get_component_pads(
        designator: str,
    ) -> dict[str, Any]:
        """Get all pads of a specific PCB component.

        Returns detailed pad information including pin name, position,
        net assignment, size, and hole information.

        DATASHEET DISCIPLINE: Pad name -> pin function mapping comes
        from the footprint, which can be wrong (especially the thermal
        pad on QFN/DFN, which is rarely numbered consistently across
        vendors). Before stating which pin a pad corresponds to,
        cross-check the manufacturer datasheet's mechanical /
        recommended-land-pattern section. The response carries
        `_datasheet_guidance` + `_datasheet_parts`.

        Args:
            designator: Component reference designator (e.g., "U1", "J3")

        Returns:
            Dictionary with "designator", "pads" array (each with name,
            x, y, net, layer, hole_size, top_x_size, top_y_size,
            rotation), "pad_count", plus `_datasheet_guidance` +
            `_datasheet_parts`.
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.get_component_pads",
            {"designator": designator},
        )
        if isinstance(result, dict):
            explicit = [{
                "manufacturer": "",
                "part_number": "",
                "designators": designator,
            }]
            return tag_response(
                result,
                explicit_parts=explicit,
                context="pcb_get_component_pads",
            )
        return result

    @mcp.tool()
    async def pcb_flip_component(
        designator: str,
    ) -> dict[str, Any]:
        """Flip a component to the other side of the board (top to bottom
        or bottom to top).

        Args:
            designator: Component reference designator (e.g., "U1", "R5")

        Returns:
            Dictionary with designator, old_layer, and new_layer
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.flip_component",
            {"designator": designator},
        )
        return result

    @mcp.tool()
    async def pcb_align_components(
        designators: str,
        alignment: str = "left",
    ) -> dict[str, Any]:
        """Align multiple PCB components along a common edge or center.

        Args:
            designators: Comma-separated component reference designators
                (e.g., "R1,R2,R3,R4")
            alignment: Alignment mode. Options:
                "left" - Align to leftmost X
                "right" - Align to rightmost X
                "top" - Align to topmost Y
                "bottom" - Align to bottommost Y
                "center_x" - Center horizontally
                "center_y" - Center vertically

        Returns:
            Dictionary with alignment result and component count
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.align_components",
            {"designators": designators, "alignment": alignment},
        )
        return result

    @mcp.tool()
    async def pcb_snap_to_grid(
        designator: str,
        grid_size: int = 50,
    ) -> dict[str, Any]:
        """Snap a component to the nearest grid point.

        Rounds the component's X and Y position to the nearest multiple
        of the specified grid size.

        Args:
            designator: Component reference designator (e.g., "U1", "R5")
            grid_size: Grid spacing in mils (default 50)

        Returns:
            Dictionary with designator, old and new positions, and grid_size
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.snap_to_grid",
            {"designator": designator, "grid_size": str(grid_size)},
        )
        return result

    @mcp.tool()
    async def pcb_get_diff_pair_rules() -> dict[str, Any]:
        """Get all differential pair routing rules from PCB design rules.

        Returns design rules of kind eRule_DifferentialPairsRouting, these
        are routing rules, NOT IPCB_DifferentialPair pair objects on the board.

        Returns:
            Dictionary with "diff_pair_rules" array (each with name, enabled,
            scope_1, scope_2, comment, descriptor) and "count"
        """
        bridge = get_bridge()
        result = await bridge.send_command_async("pcb.get_diff_pair_rules", {})
        return result

    @mcp.tool()
    async def pcb_get_vias() -> dict[str, Any]:
        """Get all vias on the active PCB board.

        Returns via position, pad size, hole size, net assignment, and
        start/end layer for every via on the board.

        Returns:
            Dictionary with "vias" array (each with x, y, size, hole_size,
            net, low_layer, high_layer) and "count"
        """
        bridge = get_bridge()
        result = await bridge.send_command_async("pcb.get_vias", {})
        return result

    @mcp.tool()
    async def pcb_calc_polygon_area(
        net: str = "",
        layer: str = "",
    ) -> dict[str, Any]:
        """Report the area of each copper polygon on the active board.

        Returns each polygon's overall boundary area in square mils and
        square millimetres, optionally filtered by net and/or layer.
        Useful for estimating copper coverage / plane area.

        Args:
            net: Only polygons on this net (optional).
            layer: Only polygons on this layer name (optional).

        Returns:
            Dict with ``polygons`` (each: name, net, layer, area_sq_mils,
            area_sq_mm) and ``count``.
        """
        bridge = get_bridge()
        params: dict[str, Any] = {}
        if net:
            params["net"] = net
        if layer:
            params["layer"] = layer
        return await bridge.send_command_async("pcb.calc_polygon_area", params)

    @mcp.tool()
    async def pcb_set_via_soldermask_relief(
        expansion_mils: int = 4,
        net: str = "",
    ) -> dict[str, Any]:
        """Open soldermask over via barrels (barrel relief).

        Sets each via's soldermask expansion-from-hole-edge so the via
        barrel gets a soldermask opening, optionally limited to one net.
        This is a common fab requirement that design rules don't expose
        directly per-via.

        Args:
            expansion_mils: Soldermask expansion from the hole edge, in
                mils (default 4).
            net: Only vias on this net (optional; default all vias).

        Returns:
            Dict with success, modified (count), expansion_mils.
        """
        bridge = get_bridge()
        params: dict[str, Any] = {"expansion_mils": str(expansion_mils)}
        if net:
            params["net"] = net
        return await bridge.send_command_async(
            "pcb.set_via_soldermask_relief", params
        )

    @mcp.tool()
    async def pcb_get_mech_layer_names() -> dict[str, Any]:
        """List the enabled mechanical layers on the active board with names.

        Returns each displayed/enabled mechanical layer's internal layer id
        and its custom name (e.g. "Assembly Top", "Courtyard"), so an agent
        can target the right mechanical layer by name rather than guessing
        "Mechanical 1".

        Returns:
            Dict with ``mechanical_layers`` (each: layer, name) and ``count``.
        """
        bridge = get_bridge()
        return await bridge.send_command_async("pcb.get_mech_layer_names", {})

    @mcp.tool()
    async def pcb_delete_object(
        x: int,
        y: int,
        layer: str = "TopLayer",
        object_type: str = "track",
    ) -> dict[str, Any]:
        """Delete a PCB object closest to specific coordinates on a layer.

        Finds the nearest matching object within 100 mils of the given
        coordinates and removes it from the board.

        Args:
            x: Target X position in mils
            y: Target Y position in mils
            layer: PCB layer name (default "TopLayer")
            object_type: Type of object to delete. Options:
                "track" - Track segment
                "via" - Via
                "fill" - Copper fill
                "text" - Text string
                "pad" - Free pad
                "arc" - Arc
                "polygon" - Copper polygon pour
                "region" - Region / split-plane
                "component" - Placed component
                (polygon/region/component/arc are matched by bounding-box
                centre; for bulk/filter-based deletes use ``delete_objects``)

        Returns:
            Dictionary with deleted status, object_type, and distance_mils
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.delete_object",
            {"x": str(x), "y": str(y), "layer": layer, "object_type": object_type},
        )
        return result

    @mcp.tool()
    async def pcb_get_pad_properties(
        net: str = "",
        designator: str = "",
    ) -> dict[str, Any]:
        """Get detailed pad information filtered by net or component.

        Returns pad shape, size, hole, thermal relief, and solder/paste
        mask expansion details. Provide at least one filter (net or
        designator) to avoid returning all pads on the board.

        Args:
            net: Filter by net name (e.g., "GND", "VCC"). Optional.
            designator: Filter by component designator (e.g., "U1"). Optional.

        Returns:
            Dictionary with "pads" array (each with name, component, x, y,
            net, layer, shape, top_x_size, top_y_size, hole_size, rotation,
            is_smd, solder_mask_expansion, paste_mask_expansion) and "count"
        """
        bridge = get_bridge()
        params: dict[str, Any] = {}
        if net:
            params["net"] = net
        if designator:
            params["designator"] = designator
        result = await bridge.send_command_async("pcb.get_pad_properties", params)
        return result

    @mcp.tool()
    async def pcb_set_track_width(
        net_name: str,
        width_mils: int,
    ) -> dict[str, Any]:
        """Modify track width for all tracks on a specific net.

        Changes the width of every routed track segment assigned to
        the given net. Useful for adjusting power or signal trace widths.

        Args:
            net_name: Name of the net whose tracks to modify (e.g., "VCC")
            width_mils: New track width in mils (e.g., 10, 20, 50)

        Returns:
            Dictionary with net_name, width_mils, and tracks_modified count
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.set_track_width",
            {"net_name": net_name, "width_mils": str(width_mils)},
        )
        return result

    @mcp.tool()
    async def pcb_get_unrouted_nets() -> dict[str, Any]:
        """Get list of nets with unrouted connections (ratsnest lines).

        Identifies nets that still have ratsnest lines, meaning they are
        not fully routed. Useful for checking routing completion status.

        Returns:
            Dictionary with "unrouted_nets" array (each with net name and
            unrouted_connections count), "net_count", and "total_unrouted"
        """
        bridge = get_bridge()
        result = await bridge.send_command_async("pcb.get_unrouted_nets", {})
        return result

    @mcp.tool()
    async def pcb_get_polygons() -> dict[str, Any]:
        """Get all polygon pours on the active PCB with copper area.

        Each polygon row carries:
          - ``index``, ``name``: positional + Altium-assigned name
          - ``net``: assigned net (often ``GND`` / a power rail)
          - ``layer``: ``TopLayer``, ``InternalPlane1``, etc.
          - ``hatch_style``: ``Solid`` / ``45Degree`` / ``Horizontal`` ...
          - ``pour_over``, ``remove_dead_copper``: pour-policy flags
          - ``area_sqmils`` / ``area_mm2``: ACTUAL copper area after
            the pour has been computed (excludes thermal-relief and
            clearance cutouts). Use this for current-capacity audits
            (multiply by copper thickness for cubic copper, then
            apply IPC-2152 / IPC-2221).
          - ``bbox_mm2``: bounding-rectangle area; ratio
            ``area_mm2 / bbox_mm2`` shows how much of the outline is
            real copper. Low ratio = pour is fighting with cutouts.
          - ``vertex_count``: a smooth pour has 4-8 vertices; many
            more usually means hand-editing that may have introduced
            narrow necks.

        Pair with ``pcb_calc_track_current_capacity`` for
        per-net current-carrying analysis.

        Returns:
            Dict with ``{polygons: [...], count: N}``.
        """
        bridge = get_bridge()
        result = await bridge.send_command_async("pcb.get_polygons", {})
        return result

    @mcp.tool()
    async def pcb_calc_track_current_capacity(
        width_mils: float,
        copper_oz: float = 1.0,
        layer: str = "external",
        length_mils: float = 0.0,
    ) -> dict[str, Any]:
        """IPC-2221 current-carrying capacity for a PCB track.

        Pure math, no Altium hit -- safe to call without an attached
        session. Returns the current the track can sustain at each
        of several temperature rises (1, 5, 10, 20, 30 °C above
        ambient), plus DC resistance if a length is supplied.

        Formula (IPC-2221, also IPC-2152's curve-fit predecessor):
          ``I = k × (ΔT)^0.44 × (h × w)^0.725``
        where ``h`` and ``w`` are in mils, ``ΔT`` in °C, and:
          ``k = 0.048`` for external layers (top, bottom)
          ``k = 0.024`` for internal layers (mid)
        Internal-layer derating is the dominant correction; running a
        signal that draws 5 A on TopLayer might be fine but on an
        inner plane the same width only handles ~3 A at the same
        ΔT because heat dissipates more slowly.

        Copper thickness conversion: 1 oz/ft² ≈ 1.378 mils. So
        ``copper_oz=1.0`` → ``h = 1.378 mils``, ``copper_oz=2.0`` →
        ``h = 2.756 mils``.

        Resistance: ``R = ρ × L / (h × w)`` with ρ = 6.7 × 10⁻⁷ Ω-mil
        (annealed copper, 25 °C). Multiply current × resistance to
        get voltage drop across the track.

        Exposes the track-current math so the agent can apply it
        across an entire power net after pulling track widths from
        ``pcb_get_trace_lengths``.

        Args:
            width_mils: Track width in mils.
            copper_oz: Copper weight in oz/ft². Common values:
                0.5 oz (HDI inner), 1.0 oz (standard), 2.0 oz (power
                planes), 3.0 oz (heavy-current).
            layer: ``"external"`` (top / bottom) or ``"internal"``
                (mid / inner planes). Defaults to external.
            length_mils: If > 0, also returns DC resistance and
                voltage drop at each temperature rise.

        Returns:
            Dict with:
              - ``width_mils``, ``copper_oz``, ``thickness_mils``, ``layer``
              - ``current_amps``: dict, keys ``1c``, ``5c``, ``10c``,
                ``20c``, ``30c``
              - ``resistance_mohm`` (only if ``length_mils`` > 0)
              - ``voltage_drop_mv`` (only if ``length_mils`` > 0, dict
                keyed the same as ``current_amps``)
        """
        if width_mils <= 0:
            return {"ok": False, "reason": "width_mils must be > 0"}
        if copper_oz <= 0:
            return {"ok": False, "reason": "copper_oz must be > 0"}
        layer_norm = layer.strip().lower()
        if layer_norm not in ("external", "internal"):
            return {"ok": False,
                    "reason": "layer must be 'external' or 'internal'"}

        thickness_mils = copper_oz * 1.378
        k = 0.024 if layer_norm == "internal" else 0.048
        b = 0.44
        c = 0.725
        hw_c = (thickness_mils * width_mils) ** c

        rises = (1, 5, 10, 20, 30)
        current = {f"{t}c": round(k * (t ** b) * hw_c, 3) for t in rises}
        out: dict[str, Any] = {
            "ok": True,
            "width_mils": width_mils,
            "copper_oz": copper_oz,
            "thickness_mils": round(thickness_mils, 3),
            "layer": layer_norm,
            "current_amps": current,
        }
        if length_mils > 0:
            rho = 6.7e-7
            r_ohm = rho * length_mils / (thickness_mils * width_mils)
            out["resistance_mohm"] = round(r_ohm * 1000.0, 4)
            out["voltage_drop_mv"] = {
                k_: round(current[k_] * r_ohm * 1000.0, 3) for k_ in current
            }
        return out

    @mcp.tool()
    async def pcb_modify_polygon(
        index: int,
        net: str = "",
        layer: str = "",
        hatch_style: str = "",
    ) -> dict[str, Any]:
        """Modify a polygon pour's properties.

        Changes net, layer, or hatching style of an existing polygon pour.
        Use pcb_get_polygons first to find the polygon index.

        Args:
            index: Polygon index (from pcb_get_polygons output)
            net: New net name to assign (optional, empty = no change)
            layer: New layer name (optional, empty = no change)
            hatch_style: New hatch style (optional). Options:
                "Solid" - Solid copper fill
                "45Degree" - 45-degree crosshatch
                "90Degree" - 90-degree crosshatch
                "Horizontal" - Horizontal lines
                "Vertical" - Vertical lines

        Returns:
            Dictionary with modified status, index, and polygon name
        """
        bridge = get_bridge()
        params: dict[str, Any] = {"index": str(index)}
        if net:
            params["net"] = net
        if layer:
            params["layer"] = layer
        if hatch_style:
            params["hatch_style"] = hatch_style
        result = await bridge.send_command_async("pcb.modify_polygon", params)
        return result

    @mcp.tool()
    async def pcb_get_room_rules() -> dict[str, Any]:
        """Get all room-like rules (confinement constraint design rules).

        Returns design rules of kind eRule_ConfinementConstraint, these are
        NOT physical IPCB_Room objects on the board. The rule bounding rect
        is reported as x1/y1/x2/y2 in mils.

        Returns:
            Dictionary with "room_rules" array (each with name, enabled, kind,
            scope_1, comment, x1, y1, x2, y2) and "count"
        """
        bridge = get_bridge()
        result = await bridge.send_command_async("pcb.get_room_rules", {})
        return result

    @mcp.tool()
    async def pcb_create_room(
        name: str,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        components: str = "",
    ) -> dict[str, Any]:
        """Create a room for component grouping on the active PCB.

        Creates a confinement constraint rule that defines a rectangular
        region. Components can be assigned via scope expression.

        Args:
            name: Room name (e.g., "Power Section", "USB Block")
            x1: First corner X in mils
            y1: First corner Y in mils
            x2: Second corner X in mils
            y2: Second corner Y in mils
            components: Comma-separated component designators to confine
                (e.g., "U1,U2,R1,R2"). Optional; empty = applies to all.

        Returns:
            Dictionary with created status, name, coordinates, and scope
        """
        bridge = get_bridge()
        params: dict[str, Any] = {
            "name": name,
            "x1": str(x1),
            "y1": str(y1),
            "x2": str(x2),
            "y2": str(y2),
        }
        if components:
            params["components"] = components
        result = await bridge.send_command_async("pcb.create_room", params)
        return result

    @mcp.tool()
    async def pcb_get_board_statistics() -> dict[str, Any]:
        """Get comprehensive statistics for the active PCB board.

        Returns counts of all object types, total trace length, board
        dimensions, and layer count. Useful for design reviews and
        progress tracking.

        Returns:
            Dictionary with track_count, via_count, pad_count,
            component_count, fill_count, text_count, polygon_count,
            unrouted_connections, total_trace_length_mils,
            board_width_mils, board_height_mils, board_area_sq_mils,
            layer_count, and board_name
        """
        bridge = get_bridge()
        result = await bridge.send_command_async("pcb.get_board_statistics", {})
        return result

    @mcp.tool()
    async def pcb_export_coordinates() -> dict[str, Any]:
        """Export component placement coordinates, same as pcb_get_components but formatted for pick-and-place.

        Returns designator, footprint, comment, position (x, y),
        rotation, layer, and side (Top/Bottom) for every component.
        Useful for manufacturing pick-and-place machine programming.

        Returns:
            Dictionary with "placements" array (each with designator,
            footprint, comment, x, y, rotation, layer, side) and "count"
        """
        bridge = get_bridge()
        result = await bridge.send_command_async("pcb.export_coordinates", {})
        return result

    @mcp.tool()
    async def pcb_create_diff_pair(
        positive_net: str,
        negative_net: str,
        name: str = "",
    ) -> dict[str, Any]:
        """Create a differential pair object from two existing nets.

        The two nets must already exist on the board (typically present
        after update_pcb / ECO). The diff-pair object lets Altium apply
        differential routing constraints and the interactive router to
        honour impedance / matched-length rules between the pair.

        Args:
            positive_net: Positive-side net name (e.g. "USB_DP")
            negative_net: Negative-side net name (e.g. "USB_DM")
            name: Optional diff-pair name (defaults to "<pos>_<neg>")

        Returns:
            Dictionary confirming creation with name, positive_net, negative_net
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.create_diff_pair",
            {
                "positive_net": positive_net,
                "negative_net": negative_net,
                "name": name,
            },
        )
        return result

    @mcp.tool()
    async def pcb_place_region(
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        layer: str = "TopLayer",
        net: str = "",
    ) -> dict[str, Any]:
        """Place a solid copper region on a rectangular area.

        Regions are solid primitives; unlike polygons, they don't participate
        in the connectivity engine unless you assign a net. Use for
        mechanical copper zones, thermal pads, or solder-mask openings.
        For a true ground plane with ratsnest tracking, prefer
        pcb_place_polygon_rect.

        Args:
            x1, y1, x2, y2: Opposite corners in mils (any order)
            layer: Copper or mech layer (default "TopLayer")
            net: Optional net assignment

        Returns:
            Dictionary confirming the region placement
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.place_region",
            {
                "x1": str(x1),
                "y1": str(y1),
                "x2": str(x2),
                "y2": str(y2),
                "layer": layer,
                "net": net,
            },
        )
        return result

    @mcp.tool()
    async def pcb_place_dimension(
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        layer: str = "TopOverlay",
        orientation: str = "",
    ) -> dict[str, Any]:
        """Place a linear dimension between two points.

        Horizontal dimension measures delta-X, vertical measures delta-Y.
        Orientation auto-detects from the larger axis delta if not given.

        Args:
            x1, y1: First reference point in mils
            x2, y2: Second reference point in mils
            layer: Layer to draw the dimension on (default "TopOverlay")
            orientation: "horizontal" or "vertical" ("" = auto)

        Returns:
            Dictionary confirming the dimension placement
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.place_dimension",
            {
                "x1": str(x1),
                "y1": str(y1),
                "x2": str(x2),
                "y2": str(y2),
                "layer": layer,
                "orientation": orientation,
            },
        )
        return result

    @mcp.tool()
    async def pcb_place_pad(
        x: int,
        y: int,
        name: str = "",
        net: str = "",
        shape: str = "round",
        x_size: int = 60,
        y_size: int = 60,
        hole_size: int = 0,
        layer: str = "TopLayer",
    ) -> dict[str, Any]:
        """Place a standalone pad on the active PCB.

        Not part of any component. Use for fiducials, test points,
        mounting holes. Set hole_size=0 for surface-mount pads,
        nonzero for through-hole.

        Args:
            x, y: Position in mils
            name: Pad designator / label (optional)
            net: Net to connect to (optional)
            shape: "round" (default) / "rect" / "oct"
            x_size, y_size: Pad dimensions in mils
            hole_size: Drill diameter in mils (0 = SMD)
            layer: Copper layer (default "TopLayer")

        Returns:
            Dictionary confirming pad placement
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.place_pad",
            {
                "x": str(x),
                "y": str(y),
                "name": name,
                "net": net,
                "shape": shape,
                "x_size": str(x_size),
                "y_size": str(y_size),
                "hole_size": str(hole_size),
                "layer": layer,
            },
        )
        return result

    @mcp.tool()
    async def pcb_place_component(
        footprint: str,
        library_path: str,
        x: int,
        y: int,
        designator: str = "",
        lib_reference: str = "",
        comment: str = "",
        rotation: float = 0,
        layer: str = "TopLayer",
        unique_id: str = "",
        pad_nets: Optional[dict[str, str]] = None,
        board_path: str = "",
    ) -> dict[str, Any]:
        """Place a footprint from a PcbLib directly onto the board — a
        scriptable substitute for ECO / *Design ▸ Update PCB Document*.

        To place MANY footprints, prefer the batch `pcb_place_components`
        (one IPC round-trip, board resolved once) over looping this.

        ``board_path``: when several PcbDocs are open, set this to the
        absolute .PcbDoc path to target a SPECIFIC board — otherwise the
        focused/current board is used, which may be the wrong one.

        Why this exists: Altium's ECO is not dialog-free (it always raises a
        modal), so for unattended board population this drops a footprint
        straight onto the board. With ``unique_id`` + ``pad_nets`` it can
        produce a **synced, connected** part; without them it is geometry
        only.

        Two modes:

        * **Geometry only** (no ``unique_id``/``pad_nets``): places the
          footprint with a designator. No schematic↔PCB link, no pad nets —
          pads are unconnected (DRC flags them), and a later real ECO treats
          the part as "extra in PCB". Fine for artwork, panelization,
          fiducials, mechanical/placement studies, or testing.
        * **Synced** (pass ``unique_id`` + ``pad_nets``): also stamps the
          schematic component's UniqueId onto the PCB part (so a later ECO
          sees it as MATCHED, not extra) and creates/assigns each pad's net
          (so the board has real connectivity — ratsnest + DRC). This is the
          programmatic-ECO path: populate a board from a compiled schematic
          with NO dialog. Read ``unique_id`` from the schematic component
          (e.g. ``query_objects(eSchComponent, "Designator.Text,UniqueId")``)
          and ``pad_nets`` from the compiled netlist
          (``get_connectivity_many`` → pad number → net).

        Footprints come from a **.PcbLib**, not the .SchLib.

        Args:
            footprint: Footprint name inside the PcbLib (e.g. "SOIC-8").
            library_path: Absolute path to the .PcbLib holding it.
            x, y: Placement position in mils.
            designator: Reference designator to assign (e.g. "U1"). Match
                the schematic for the netlist join.
            lib_reference: Source library reference; defaults to
                ``footprint`` when omitted.
            comment: Comment / value text (optional).
            rotation: Orientation in degrees (default 0).
            layer: Placement layer (default "TopLayer"; "BottomLayer"
                for bottom-side).
            unique_id: The schematic component's UniqueId, to link this PCB
                part to its schematic counterpart (so a later ECO matches it
                instead of flagging it as extra).
            pad_nets: ``{pad_name: net_name}`` (e.g. ``{"1": "VCC",
                "2": "GND"}``). Each named net is created on the board if
                missing and assigned to that pad, giving real connectivity.

        Returns:
            Dict with ``placed``, ``footprint``, ``designator``, ``x``,
            ``y``, ``rotation``, ``layer``, ``linked`` (UniqueId stamped),
            ``nets_assigned`` (pad count wired) — or an error.
        """
        pad_nets_str = ""
        if pad_nets:
            pad_nets_str = "|".join(
                f"{str(k).strip()}={str(v).strip()}"
                for k, v in pad_nets.items()
                if str(k).strip() and str(v).strip()
            )
        bridge = get_bridge()
        return await bridge.send_command_async(
            "pcb.place_component",
            {
                "footprint": footprint,
                "library_path": library_path,
                "lib_reference": lib_reference,
                "designator": designator,
                "comment": comment,
                "x": str(int(x)),
                "y": str(int(y)),
                "rotation": str(rotation),
                "layer": layer,
                "unique_id": unique_id,
                "pad_nets": pad_nets_str,
                "board_path": board_path,
            },
        )

    @mcp.tool()
    async def pcb_place_components(
        placements: list[dict[str, Any]],
        board_path: str = "",
    ) -> dict[str, Any]:
        """Place MANY footprints from PcbLibs onto the board in ONE call.

        PREFER THIS over looping `pcb_place_component` — it resolves the
        board once and places every footprint in a single Altium
        transaction (one IPC round-trip instead of N). Same synced-mode
        capability per component (UniqueId link + pad-net creation).

        ``board_path``: target a specific .PcbDoc when several are open
        (resolved once for the whole batch). Otherwise the focused board
        is used.

        Args:
            placements: List of placement dicts, each with:
                footprint (required), library_path (required, .PcbLib),
                x, y (mils), designator, lib_reference, comment,
                rotation, layer, unique_id, and pad_nets
                (``{pad_name: net_name}``). See `pcb_place_component` for
                the meaning of each (geometry-only vs synced).
            board_path: Absolute .PcbDoc path to place onto (optional).

        Returns:
            Dict with ``placed`` (count), ``failed`` (count), ``total``.
        """
        recs: list[str] = []
        for p in placements:
            fp = str(p.get("footprint", "")).strip()
            lib = str(p.get("library_path", "")).strip()
            if not fp or not lib:
                continue
            fields = [f"footprint=={fp}", f"library_path=={lib}"]
            for key in ("lib_reference", "designator", "comment",
                        "layer", "unique_id"):
                v = str(p.get(key, "") or "").strip()
                if v:
                    fields.append(f"{key}=={v}")
            fields.append(f"x=={int(p.get('x', 0))}")
            fields.append(f"y=={int(p.get('y', 0))}")
            if p.get("rotation") is not None:
                fields.append(f"rotation=={p.get('rotation')}")
            pn = p.get("pad_nets")
            if pn:
                pn_str = "|".join(
                    f"{str(k).strip()}={str(v).strip()}"
                    for k, v in pn.items()
                    if str(k).strip() and str(v).strip()
                )
                if pn_str:
                    fields.append(f"pad_nets=={pn_str}")
            recs.append(";;".join(fields))

        if not recs:
            return {"error": "No valid placements (need footprint + "
                    "library_path)", "placed": 0}

        bridge = get_bridge()
        return await bridge.send_command_async(
            "pcb.place_components",
            {"placements": "~~".join(recs), "board_path": board_path},
        )

    @mcp.tool()
    async def pcb_place_angular_dimension(
        center_x: int,
        center_y: int,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        radius: int = 100,
        layer: str = "TopOverlay",
    ) -> dict[str, Any]:
        """Place an angular dimension (angle between two reference directions).

        Args:
            center_x, center_y: Vertex of the angle in mils
            x1, y1: First reference direction endpoint in mils
            x2, y2: Second reference direction endpoint in mils
            radius: Arc radius at which to draw the dimension in mils
            layer: Layer (default "TopOverlay")

        Returns:
            Dictionary confirming the angular dimension
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.place_angular_dimension",
            {
                "center_x": str(center_x),
                "center_y": str(center_y),
                "x1": str(x1),
                "y1": str(y1),
                "x2": str(x2),
                "y2": str(y2),
                "radius": str(radius),
                "layer": layer,
            },
        )
        return result

    @mcp.tool()
    async def pcb_place_radial_dimension(
        center_x: int,
        center_y: int,
        radius: int,
        layer: str = "TopOverlay",
    ) -> dict[str, Any]:
        """Place a radial dimension around a center point with a given radius.

        Args:
            center_x, center_y: Center point in mils
            radius: Radius to dimension in mils
            layer: Layer (default "TopOverlay")

        Returns:
            Dictionary confirming the radial dimension
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.place_radial_dimension",
            {
                "center_x": str(center_x),
                "center_y": str(center_y),
                "radius": str(radius),
                "layer": layer,
            },
        )
        return result

    @mcp.tool()
    async def pcb_distribute_components(
        designators: str,
        axis: str = "x",
        start: int = 0,
        end: int = 1000,
    ) -> dict[str, Any]:
        """Evenly space components along an axis.

        Moves each named component so its X (or Y) coordinate lands at
        equally-spaced stops from `start` to `end`. Order follows the
        designators list, designators="R1,R2,R3" with start=0 end=200
        places R1 at 0, R2 at 100, R3 at 200 on the chosen axis. Y (or X)
        is untouched.

        Args:
            designators: Comma-separated list of component designators
            axis: "x" or "y" (default "x")
            start: First position in mils
            end: Last position in mils

        Returns:
            Dictionary with distribution result
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "pcb.distribute_components",
            {
                "designators": designators,
                "axis": axis,
                "start": str(start),
                "end": str(end),
            },
        )
        return result

    @mcp.tool()
    async def pcb_get_rule_properties(name: str) -> dict[str, Any]:
        """Read properties of a named PCB design rule.

        Returns metadata (name, rule_kind, enabled, priority) plus a
        ``descriptor`` string that contains the rule's constraint values
        in human-readable form, e.g.:

            "Width Constraint (Min=0.102mm) (Max=5.08mm) (Preferred=0.127mm) (All)"
            "Clearance Constraint (Gap=0.127mm) (All),(All)"
            "Hole Size Constraint (Min=0.1mm) (Max=4mm) (All)"
            "Routing Via (Templates Used To Check Via: v30h10m0mx0, ...) (All)"

        Constraint values live on per-kind subtype interfaces
        (IPCB_ClearanceConstraint, IPCB_MaxMinWidthConstraint, ...) which
        cannot be safely accessed from a base IPCB_Rule reference in
        DelphiScript, so we surface the descriptor string instead. Parse
        it client-side if you need numeric values.

        Args:
            name: Design rule name (e.g., "Clearance", "Width", "RoutingVias").

        Returns:
            Dict with name, rule_kind, enabled, priority, descriptor.
        """
        bridge = get_bridge()
        return await bridge.send_command_async(
            "pcb.get_rule_properties", {"name": name}
        )

    @mcp.tool()
    async def pcb_set_rule_properties(
        name: str,
        enabled: bool | None = None,
        scope1: str | None = None,
        scope2: str | None = None,
        comment: str | None = None,
        gap_mils: int | None = None,
        min_width_mils: int | None = None,
        max_width_mils: int | None = None,
        favored_width_mils: int | None = None,
        min_hole_size_mils: int | None = None,
        max_hole_size_mils: int | None = None,
    ) -> dict[str, Any]:
        """Update metadata AND constraint values of a named PCB design rule.

        Metadata fields always apply. Constraint fields are dispatched
        by the rule's underlying RuleKind:

        - Clearance (kind 0), ComponentClearance (kind 24), and
          HoleToHoleClearance (kind 52): ``gap_mils``. All three share
          the Gap property on IPCB_ClearanceConstraint.
        - Width (kind 2): ``min_width_mils`` / ``max_width_mils`` /
          ``favored_width_mils`` (applied to every layer).
        - HoleSize (kind 42): ``min_hole_size_mils`` / ``max_hole_size_mils``.

        Pass only the parameters you want to change; everything else
        stays untouched. Each successful field write increments
        ``properties_updated`` in the response.

        NOTE: Priority is NOT writable from this tool. ``IPCB_Rule.Priority``
        is a read-only function in the PCB scripting API
        (``Function Priority : TRulePrecedence``), not a property; there
        is no ``SetState_Priority`` or ``SetPriority`` method either.
        Assigning to it crashes the DelphiScript engine. To reorder rule
        priorities, drag them in Altium's UI (PCB > Rules and Constraints
        Editor; rules earlier in a category have higher priority).

        Args:
            name: Rule name to update.
            enabled: Whether DRC enforces the rule.
            scope1 / scope2: Rule scope query expressions.
            comment: Free-form comment.
            gap_mils: Clearance gap. Applies to Clearance,
                ComponentClearance, and HoleToHoleClearance rules.
            min_width_mils / max_width_mils / favored_width_mils:
                Width constraint values (applied to every layer).
            min_hole_size_mils / max_hole_size_mils: HoleSize
                constraint limits.

        Returns:
            Dict with name, rule_kind, and properties_updated count.
        """
        params: dict[str, Any] = {"name": name}
        for key, value in [
            ("scope1", scope1),
            ("scope2", scope2),
            ("comment", comment),
            ("gap_mils", gap_mils),
            ("min_width_mils", min_width_mils),
            ("max_width_mils", max_width_mils),
            ("favored_width_mils", favored_width_mils),
            ("min_hole_size_mils", min_hole_size_mils),
            ("max_hole_size_mils", max_hole_size_mils),
        ]:
            if value is not None:
                params[key] = str(value)
        if enabled is not None:
            params["enabled"] = "true" if enabled else "false"

        bridge = get_bridge()
        return await bridge.send_command_async(
            "pcb.set_rule_properties", params
        )

    @mcp.tool()
    async def pcb_place_embedded_board(
        child_path: str,
        x: int,
        y: int,
        rows: int = 1,
        cols: int = 1,
        row_spacing_mils: int = 0,
        col_spacing_mils: int = 0,
        mirror: bool = False,
        layer: str = "TopLayer",
    ) -> dict[str, Any]:
        """Place an embedded-board array (panel) referencing a child PCB.

        An embedded board is a grid of copies of a child .PcbDoc file,
        used for panelization (multi-up arrays, step-and-repeat).

        Args:
            child_path: Full path to the child .PcbDoc file.
            x, y: Bottom-left corner of the array in mils.
            rows, cols: Grid size (default 1x1).
            row_spacing_mils, col_spacing_mils: Gap between array cells.
            mirror: Flip the child board over when True.
            layer: Placement layer for the embedded-board primitive.

        Returns:
            Dict with success, child_path, rows, cols, x, y.
        """
        bridge = get_bridge()
        return await bridge.send_command_async(
            "pcb.place_embedded_board",
            {
                "child_path": child_path,
                "x": str(x),
                "y": str(y),
                "rows": str(rows),
                "cols": str(cols),
                "row_spacing_mils": str(row_spacing_mils),
                "col_spacing_mils": str(col_spacing_mils),
                "mirror": "true" if mirror else "false",
                "layer": layer,
            },
        )

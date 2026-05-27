# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Library management tools for Altium Designer MCP Server."""

from typing import Any, Optional
from ..bridge import get_bridge
from ..bridge.exceptions import InvalidParameterError
from .bulk_hints import BulkHintTracker
from .datasheet_hints import tag_response
from ..config import get_config


# Schematic symbol grid is 100 mils. Every pin Location and every
# rectangle/line corner authored via the lib_add_* tools is rounded to
# this grid before the bridge call. Off-grid pins break wire routing
# in placed instances; off-grid rectangle corners produce blurry-looking
# bodies and prevent the Altium snap mechanism from aligning them.
# This is a hard invariant, not a hint -- every coord goes through here.
_SCHEMATIC_GRID_MILS = 100


def _snap(value: int) -> int:
    """Round a mil coordinate to the schematic 100-mil grid.

    Banker's-style: nearest-50 rounds away from zero. The placement
    pipelines all snap downward (// 100 * 100), but the symbol author
    tools accept user-supplied coords that may be off by a few mils
    (e.g., a hand-typed 503) -- rounding is more forgiving than
    truncating in that case.
    """
    if value >= 0:
        return ((value + _SCHEMATIC_GRID_MILS // 2)
                // _SCHEMATIC_GRID_MILS) * _SCHEMATIC_GRID_MILS
    return -(((-value + _SCHEMATIC_GRID_MILS // 2)
              // _SCHEMATIC_GRID_MILS) * _SCHEMATIC_GRID_MILS)


def register_library_tools(mcp):
    """Register library tools with the MCP server."""

    # =========================================================================
    # Symbol Creation
    # =========================================================================

    @mcp.tool()
    async def lib_create_symbol(
        name: str,
        designator_prefix: str = "U",
        description: str = "",
    ) -> dict[str, Any]:
        """Create a new schematic symbol in the active library.

        Args:
            name: Component name
            designator_prefix: Default designator prefix (e.g., "U", "R", "C")
            description: Component description

        Returns:
            Dictionary with created symbol information
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "library.create_symbol",
            {
                "name": name,
                "designator_prefix": designator_prefix,
                "description": description,
            },
        )
        return result

    @mcp.tool()
    async def lib_set_current_component(
        component_name: str,
    ) -> dict[str, Any]:
        """Make a named component the editor's current selection in the
        active SchLib.

        Required before bulk-editing a specific component's pins,
        rectangle, or parameters via ``modify_objects`` / ``batch_modify``
        on a SchLib. The asymmetry it fixes: ``lib_get_component_details``
        is a read-only fetch and does NOT update the editor's current
        component, so subsequent ``modify_objects`` on the SchLib's
        ePin / eRectangle / eParameter iterators silently hits whatever
        component was last UI-selected -- usually NOT the one you just
        read.

        Use this between switching components:
            lib_set_current_component("MyIC")
            modify_objects("ePin", scope="active_doc",
                           filter="Location.X=200", set="Orientation=2")
            lib_set_current_component("MyOtherPart")
            modify_objects("ePin", scope="active_doc",
                           filter="Location.X=200", set="Orientation=2")

        Args:
            component_name: Component name (LibRef) in the active SchLib.

        Returns:
            Dict with ``success`` + ``name``, or an error if no SchLib
            is active or the component name isn't found in it.
        """
        bridge = get_bridge()
        return await bridge.send_command_async(
            "library.set_current_component",
            {"name": component_name},
        )

    @mcp.tool()
    async def lib_add_pin(
        designator: str,
        name: str,
        x: int,
        y: int,
        length: int = 200,
        rotation: int = 0,
        electrical_type: str = "passive",
        hidden: bool = False,
    ) -> dict[str, Any]:
        """Add a pin to the current symbol.

        IMPORTANT, if you need to add more than one pin, use
        `lib_add_pins` (batch) instead. Creating a new symbol with 20+
        pins via this singular tool is the biggest wall-time sink in
        library workflows: each pin is a full LLM turn. The batch
        version does all pins in one PreProcess/PostProcess + one save.

        Args:
            designator: Pin designator (e.g., "1", "2", "VCC")
            name: Pin name
            x: X coordinate in mils
            y: Y coordinate in mils
            length: Pin length in mils
            rotation: Pin rotation in degrees (0, 90, 180, 270)
            electrical_type: Electrical type:
                - "input", "output", "bidirectional", "passive"
                - "open_collector", "open_emitter", "power", "hiz"
            hidden: Whether to hide the pin

        Returns:
            Dictionary confirming pin addition
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "library.add_pin",
            {
                "designator": designator,
                "name": name,
                "x": _snap(int(x)),
                "y": _snap(int(y)),
                "length": _snap(int(length)),
                "rotation": rotation,
                "electrical_type": electrical_type,
                "hidden": hidden,
            },
        )
        hint = BulkHintTracker.record_and_hint("lib_add_pin")
        if hint and isinstance(result, dict):
            result["_hint_bulk"] = hint
        return result

    @mcp.tool()
    async def lib_add_pins(
        pins: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Add MANY pins to the current symbol in ONE call.

        PREFER THIS over looping `lib_add_pin`. A 48-pin IC symbol
        built one pin at a time is 48 LLM turns; with this tool it's
        one turn + one PreProcess/PostProcess + one save.

        Args:
            pins: List of pin dicts, each with:
                - designator (str, required)
                - name       (str, required)
                - x, y       (int, mils), pin endpoint
                - length     (int, mils, default 200)
                - rotation   (int, default 0), 0/90/180/270
                - electrical_type (str, default "passive"), one of
                  input/output/bidirectional/passive/open_collector/
                  open_emitter/power/hiz/io
                - hidden     (bool, default False)

        Example, a 4-pin dual op-amp stage:
            lib_add_pins(pins=[
                {"designator": "1", "name": "OUT1",  "x": 0,   "y": 0,
                 "rotation": 180, "electrical_type": "output"},
                {"designator": "2", "name": "IN1-",  "x": 0,   "y": 100,
                 "rotation": 180, "electrical_type": "input"},
                {"designator": "3", "name": "IN1+",  "x": 0,   "y": 200,
                 "rotation": 180, "electrical_type": "input"},
                {"designator": "4", "name": "GND",   "x": 0,   "y": 300,
                 "rotation": 180, "electrical_type": "power"},
            ])

        Returns:
            Dict with added, failed, total counts.
        """
        op_strs: list[str] = []
        for p in pins:
            desig = str(p.get("designator", "")).strip()
            name = str(p.get("name", "")).strip()
            if not desig:
                continue
            fields = [
                f"designator={desig}",
                f"name={name}",
                f"x={_snap(int(p.get('x', 0)))}",
                f"y={_snap(int(p.get('y', 0)))}",
                f"length={_snap(int(p.get('length', 200)))}",
                f"rotation={int(p.get('rotation', 0))}",
                f"electrical_type={p.get('electrical_type', 'passive')}",
                f"hidden={'true' if p.get('hidden') else 'false'}",
            ]
            op_strs.append(";".join(fields))

        if not op_strs:
            return {"error": "No valid pins", "added": 0}

        bridge = get_bridge()
        return await bridge.send_command_async(
            "library.add_pins",
            {"pins": "~~".join(op_strs)},
        )

    @mcp.tool()
    async def lib_add_symbol_rectangle(
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        fill_color: int = -1,
        border_color: int = 0,
    ) -> dict[str, Any]:
        """Add a rectangle to the current symbol body.

        Args:
            x1: First corner X in mils
            y1: First corner Y in mils
            x2: Opposite corner X in mils
            y2: Opposite corner Y in mils
            fill_color: Fill color index (-1 = no fill)
            border_color: Border color index

        Returns:
            Dictionary confirming rectangle addition
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "library.add_symbol_rectangle",
            {
                "x1": _snap(int(x1)),
                "y1": _snap(int(y1)),
                "x2": _snap(int(x2)),
                "y2": _snap(int(y2)),
                "fill_color": fill_color,
                "border_color": border_color,
            },
        )
        return result

    @mcp.tool()
    async def lib_add_symbol_lines(
        lines: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Add MANY lines to the current symbol body in ONE call.

        PREFER THIS over looping ``lib_add_symbol_line``. A 12-line LED
        diode glyph drawn line-by-line is 12 LLM turns + 12 IPC
        round-trips + 12 redraw passes; this tool does it in one turn
        with a single PreProcess/PostProcess pair and one editor
        redraw at the end.

        Args:
            lines: list of dicts, each with keys ``x1``, ``y1``,
                ``x2``, ``y2`` (mils, int), ``width`` (int 0-3,
                default 1). Coords are snapped to the 100-mil grid.

        Returns:
            Dict with ``added``, ``failed``, ``total`` counts.
        """
        op_strs: list[str] = []
        for line in lines:
            fields = [
                f"x1={_snap(int(line.get('x1', 0)))}",
                f"y1={_snap(int(line.get('y1', 0)))}",
                f"x2={_snap(int(line.get('x2', 0)))}",
                f"y2={_snap(int(line.get('y2', 0)))}",
                f"width={int(line.get('width', 1))}",
            ]
            op_strs.append(";".join(fields))
        if not op_strs:
            return {"error": "No lines provided", "added": 0}
        bridge = get_bridge()
        return await bridge.send_command_async(
            "library.add_symbol_lines",
            {"lines": "~~".join(op_strs)},
        )

    @mcp.tool()
    async def lib_add_symbol_line(
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        width: int = 1,
    ) -> dict[str, Any]:
        """Add a line to the current symbol.

        Args:
            x1: Start X in mils
            y1: Start Y in mils
            x2: End X in mils
            y2: End Y in mils
            width: Line width

        Returns:
            Dictionary confirming line addition
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "library.add_symbol_line",
            {"x1": _snap(int(x1)), "y1": _snap(int(y1)),
             "x2": _snap(int(x2)), "y2": _snap(int(y2)),
             "width": width},
        )
        return result

    # =========================================================================
    # Footprint Creation
    # =========================================================================

    @mcp.tool()
    async def lib_create_footprint(
        name: str,
        description: str = "",
    ) -> dict[str, Any]:
        """Create a new PCB footprint in the active library.

        Args:
            name: Footprint name
            description: Footprint description

        Returns:
            Dictionary with created footprint information
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "library.create_footprint",
            {"name": name, "description": description},
        )
        return result

    @mcp.tool()
    async def lib_add_footprint_pad(
        designator: str,
        x: int,
        y: int,
        x_size: int = 60,
        y_size: int = 60,
        hole_size: int = 0,
        shape: str = "rectangular",
        layer: str = "TopLayer",
        rotation: int = 0,
    ) -> dict[str, Any]:
        """Add a pad to the current footprint.

        Args:
            designator: Pad designator (e.g., "1", "2")
            x: X coordinate in mils
            y: Y coordinate in mils
            x_size: Pad X size in mils
            y_size: Pad Y size in mils
            hole_size: Drill hole size in mils (0 for SMD)
            shape: Pad shape ("round", "rectangular", "octagonal")
            layer: Layer ("TopLayer", "BottomLayer", "MultiLayer")
            rotation: Pad rotation in degrees

        Returns:
            Dictionary confirming pad addition
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "library.add_footprint_pad",
            {
                "designator": designator,
                "x": x,
                "y": y,
                "x_size": x_size,
                "y_size": y_size,
                "hole_size": hole_size,
                "shape": shape,
                "layer": layer,
                "rotation": rotation,
            },
        )
        return result

    @mcp.tool()
    async def lib_add_footprint_track(
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        width: int = 10,
        layer: str = "TopOverlay",
    ) -> dict[str, Any]:
        """Add a track to the current footprint (for silkscreen/courtyard).

        Args:
            x1: Start X in mils
            y1: Start Y in mils
            x2: End X in mils
            y2: End Y in mils
            width: Track width in mils
            layer: Layer (typically TopOverlay for silkscreen)

        Returns:
            Dictionary confirming track addition
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "library.add_footprint_track",
            {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "width": width, "layer": layer},
        )
        return result

    @mcp.tool()
    async def lib_add_footprint_arc(
        x_center: int,
        y_center: int,
        radius: int,
        start_angle: float = 0,
        end_angle: float = 360,
        width: int = 10,
        layer: str = "TopOverlay",
    ) -> dict[str, Any]:
        """Add an arc to the current footprint.

        Args:
            x_center: Center X in mils
            y_center: Center Y in mils
            radius: Arc radius in mils
            start_angle: Start angle in degrees
            end_angle: End angle in degrees
            width: Line width in mils
            layer: Layer for the arc

        Returns:
            Dictionary confirming arc addition
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "library.add_footprint_arc",
            {
                "x_center": x_center,
                "y_center": y_center,
                "radius": radius,
                "start_angle": start_angle,
                "end_angle": end_angle,
                "width": width,
                "layer": layer,
            },
        )
        return result

    @mcp.tool()
    async def lib_add_footprint_text(
        text: str,
        x: int = 0,
        y: int = 0,
        size: int = 50,
        width: int = 8,
        rotation: int = 0,
        layer: str = "TopOverlay",
        use_ttfont: bool = False,
        library_path: Optional[str] = None,
        component_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Add a text primitive to a PcbLib footprint.

        Trap baked into this handler: in a PcbLib, adding a primitive
        only to the footprint does NOT register it with the placement
        editor -- the text shows up only after a save+reload. This
        tool adds the text to BOTH the footprint and the underlying
        board, then broadcasts ``PCBM_BoardRegisteration`` to both, per
        the reference DelphiScript (``PcbLib/FootPrintText_2.pas``).

        Args:
            text: The string to place. Required.
            x, y: Coordinates in mils, relative to the board origin.
            size: Text height in mils. 50 is a common silkscreen size;
                drop to 30-40 for tight footprints.
            width: Stroke width in mils. 8 reads cleanly at 50 mil
                size; scale with ``size``.
            rotation: Rotation in degrees (0, 90, 180, 270 typical).
            layer: Layer name resolved by GetLayerFromString
                (``TopOverlay``, ``BottomOverlay``, ``TopSolder``,
                ``BottomSolder``, ``Mechanical1`` ... ``Mechanical32``).
            use_ttfont: ``True`` for TrueType; default ``False`` is the
                vector stroke font that fab houses prefer.
            library_path: Optional .PcbLib to focus before adding.
                Defaults to the active document.
            component_name: Optional footprint name to switch to before
                adding. Defaults to the currently active footprint.

        Returns:
            Dict with ``success``, ``footprint``, ``text``, ``layer``,
            ``x``, ``y``.
        """
        if not text:
            raise InvalidParameterError("text is required")
        bridge = get_bridge()
        params: dict[str, Any] = {
            "text": text,
            "x": x, "y": y,
            "size": size, "width": width,
            "rotation": rotation, "layer": layer,
        }
        if use_ttfont:
            params["use_ttfont"] = "true"
        if library_path:
            params["library_path"] = library_path
        if component_name:
            params["component_name"] = component_name
        result = await bridge.send_command_async(
            "library.add_footprint_text", params,
        )
        return result

    @mcp.tool()
    async def lib_extract_intlib(
        intlib_path: str,
    ) -> dict[str, Any]:
        """Extract .SchLib + .PcbLib sources from an .IntLib.

        Opens the integrated library in Altium, runs the editor's
        ``Extract Sources`` command, then probes Altium's conventional
        output locations (a sibling folder named after the IntLib base
        name, falling back to the IntLib's own directory) to report
        which source files actually appeared on disk.

        After this returns with ``sch_lib_found`` and / or
        ``pcb_lib_found`` true, the produced files can be opened with
        the rest of the library toolset -- ``lib_get_components``,
        ``lib_get_footprints``, ``lib_copy_component``, etc. -- by
        passing the reported ``sch_lib_path`` / ``pcb_lib_path`` as
        the ``library_path`` argument.

        Args:
            intlib_path: Absolute path to the .IntLib file.

        Returns:
            Dict with ``intlib_path``, ``extract_dir``, ``sch_lib_path``,
            ``sch_lib_found``, ``pcb_lib_path``, ``pcb_lib_found``. A
            ``_found`` flag of False means Altium did not produce that
            source -- either the extract command name needs adjusting
            for the local Altium build, the IntLib does not contain that
            kind of source, or write permissions prevented the dump.
        """
        if not intlib_path:
            raise InvalidParameterError("intlib_path is required")
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "library.extract_intlib",
            {"intlib_path": intlib_path},
            timeout=60.0,
        )
        return result or {}

    @mcp.tool()
    async def lib_get_footprints(
        library_path: Optional[str] = None,
    ) -> dict[str, Any]:
        """Enumerate every footprint in a PcbLib.

        PcbLib counterpart to ``lib_get_components`` for SchLibs. Walks
        the PCB library with ``IPCB_Library.LibraryIterator_Create``
        and returns one entry per footprint with its name and
        description.

        Args:
            library_path: Optional .PcbLib path to focus first.
                Defaults to the focused document.

        Returns:
            Dict with ``library_path``, ``count`` and ``footprints``
            (a list of ``{name, description}``).
        """
        bridge = get_bridge()
        params: dict[str, Any] = {}
        if library_path:
            params["library_path"] = library_path
        result = await bridge.send_command_async(
            "library.get_footprints", params,
        )
        return result

    # =========================================================================
    # Component Linking
    # =========================================================================

    @mcp.tool()
    async def lib_link_footprint(
        component_name: str,
        footprint_name: str,
        footprint_library: str = "",
    ) -> dict[str, Any]:
        """Link a footprint to a schematic component.

        NOTE: Uses the current active library component, not the specified
        component_name. Open/focus the target component in the SchLib editor
        before calling this.

        Args:
            component_name: Name of the schematic component (currently ignored,
                see note above)
            footprint_name: Name of the footprint to link
            footprint_library: Library containing the footprint (optional if same library)

        Returns:
            Dictionary confirming link
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "library.link_footprint",
            {
                "component_name": component_name,
                "footprint_name": footprint_name,
                "library_name": footprint_library,
            },
        )
        return result

    @mcp.tool()
    async def lib_link_3d_model(
        component_name: str,
        model_path: str,
        offset_x: float = 0,
        offset_y: float = 0,
        offset_z: float = 0,
        rotation_x: float = 0,
        rotation_y: float = 0,
        rotation_z: float = 0,
    ) -> dict[str, Any]:
        """Link a 3D model to a footprint.

        NOTE: offset and rotation parameters are currently ignored by Altium;
        set them manually in the library after linking.

        Args:
            component_name: Name of the footprint
            model_path: Path to the 3D model file (.step, .stp)
            offset_x: X offset in mils (ignored, see note)
            offset_y: Y offset in mils (ignored, see note)
            offset_z: Z offset in mils (ignored, see note)
            rotation_x: X rotation in degrees (ignored, see note)
            rotation_y: Y rotation in degrees (ignored, see note)
            rotation_z: Z rotation in degrees (ignored, see note)

        Returns:
            Dictionary confirming link
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "library.link_3d_model",
            {
                "component_name": component_name,
                "model_path": model_path,
                "offset_x": offset_x,
                "offset_y": offset_y,
                "offset_z": offset_z,
                "rotation_x": rotation_x,
                "rotation_y": rotation_y,
                "rotation_z": rotation_z,
            },
        )
        return result

    # =========================================================================
    # Library Search and Information
    # =========================================================================

    @mcp.tool()
    async def lib_get_components(
        library_path: Optional[str] = None,
        with_parameters: bool = False,
    ) -> dict[str, Any]:
        """Get all components in a library.

        Default fast path returns only the metadata that the
        ``ILibCompInfoReader`` exposes directly: name, alias_name,
        part_count, description. That path scales linearly with file IO
        and finishes in well under a second on typical libraries.

        Setting ``with_parameters=True`` adds each component's full
        parameter dict (Manufacturer, Value, Footprint, etc.) to the
        result. That branch calls ``GetState_SchComponentByLibRef`` once
        per symbol and iterates parameters, which is O(N) in the live
        SchLib document and is what makes the call slow on libraries
        with many hundreds of components. Use it when you need the
        parameters; for a single symbol's parameters, prefer
        ``lib_get_component_details``.

        Args:
            library_path: Path to library (uses active library if not specified)
            with_parameters: If True, include each component's parameter
                dict (slow on large libraries). Default False.

        Returns:
            Dictionary with ``count`` and ``components`` list. Each
            component carries name, alias_name, part_count, description,
            and (only when with_parameters is True) parameters.
        """
        bridge = get_bridge()
        params: dict[str, Any] = {}
        if library_path:
            params["library_path"] = library_path
        if with_parameters:
            params["with_parameters"] = "true"
        result = await bridge.send_command_async("library.get_components", params)
        if isinstance(result, dict):
            return tag_response(
                result, components=result, context="lib_get_components"
            )
        return result or {}

    @mcp.tool()
    async def lib_search(
        query: str,
        search_type: str = "all",
        library_path: Optional[str] = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Search open SchLib documents for components.

        Case-insensitive substring match. Walks every .SchLib that is
        a member of any open project, plus every standalone .SchLib in
        the workspace's free-documents area. Each library is read via
        ``CreateLibCompInfoReader`` so the search is fast even with
        many libraries open: it only loads symbols when ``search_type``
        is ``"parameters"``.

        DATASHEET DISCIPLINE: Matches carry `_datasheet_guidance`.
        Before recommending any matched part as a replacement or
        answer, fetch its datasheet (WebSearch + WebFetch). Do not
        recommend based on symbol metadata alone.

        Args:
            query: Substring to match against component name / alias /
                description (case-insensitive).
            search_type: ``"all"`` (default, matches name / alias /
                description), ``"name"``, ``"description"``, or
                ``"parameters"`` (slow, also walks each candidate's
                parameter dict via the live symbol).
            library_path: Optional path to a single .SchLib to restrict
                the search to. When omitted, searches every open
                library.
            limit: Cap on returned matches (default 100).

        Returns:
            Dict with ``query``, ``search_type``, ``count``, ``limit``,
            ``truncated`` (True when count == limit), and ``results`` —
            a list of {name, alias_name, description, library_path,
            part_count} per match — plus `_datasheet_guidance` +
            `_datasheet_parts`.
        """
        bridge = get_bridge()
        params: dict[str, Any] = {
            "query": query,
            "search_type": search_type,
            "limit": str(limit),
        }
        if library_path:
            params["library_path"] = library_path
        result = await bridge.send_command_async("library.search", params)
        if isinstance(result, list):
            result = {"results": result}
        if isinstance(result, dict):
            synthetic = {"components": (
                result.get("results") or result.get("components") or []
            )}
            return tag_response(
                result, components=synthetic, context="lib_search"
            )
        return result

    @mcp.tool()
    async def lib_get_component_details(
        component_name: str,
        library_path: Optional[str] = None,
    ) -> dict[str, Any]:
        """Get full inspection of one library component in a single call.

        Returns metadata, every pin, every parameter (as a flat dict),
        AND visual-style records for the designator, comment, pins,
        and each parameter (font_id, color, is_hidden, x, y,
        orientation, justification). The integer ``font_id`` can be
        expanded to {name, size, bold, italic} via ``get_font_spec``
        when style detail is needed; the round-trip default keeps it
        compact.

        If ``library_path`` is provided and isn't already focused, the
        library is opened (focus changes), so the next ``lib_*`` call
        operates on it without an explicit open. Saves are deferred,
        opening doesn't write anything to disk.

        DATASHEET DISCIPLINE: This response is the highest-density
        device-fact surface in the library API (pins, parameters,
        Manufacturer/MPN). Treat every value as a hint to find the
        manufacturer datasheet, not as ground truth. The response
        carries `_datasheet_guidance` and `_datasheet_parts`, fetch
        the PDF and cite a section/page before stating any pin or
        rating.

        Args:
            component_name: Component LibRef as it appears in the .SchLib.
            library_path: Optional .SchLib full path. When omitted the
                currently focused library is used.

        Returns:
            Dict with:
              - name, library_path, description, alias_name,
                part_count, pin_count
              - designator: {text, font_id, color, is_hidden, x, y,
                orientation, justification} - the on-canvas designator
                label (NOT just the prefix string).
              - comment: {text, font_id, color, is_hidden, x, y,
                orientation, justification} - the on-canvas comment /
                value label.
              - pins: list of {designator, name, electrical_type, x, y,
                orientation, hidden, label_hidden}. Pin font / color
                are not exposed by the Altium SDK on ISch_Pin and
                therefore not surfaced here.
              - parameters: flat dict of name -> value (cheap lookups).
              - parameter_styles: list of {name, value, style:{font_id,
                color, is_hidden, x, y, orientation, justification}}
                in the same order parameters appear on the symbol.
              - `_datasheet_guidance` + `_datasheet_parts`.
        """
        bridge = get_bridge()
        params: dict[str, Any] = {"component_name": component_name}
        if library_path:
            params["library_path"] = library_path
        result = await bridge.send_command_async(
            "library.get_component_details", params,
        )
        if isinstance(result, dict):
            mfr = ""
            mpn = ""
            params = result.get("parameters") or {}
            if isinstance(params, dict):
                mfr = str(
                    params.get("Manufacturer")
                    or params.get("manufacturer")
                    or ""
                ).strip()
                mpn = str(
                    params.get("ManufacturerPartNumber")
                    or params.get("Manufacturer Part Number")
                    or params.get("Partnumber")
                    or params.get("PartNumber")
                    or params.get("Comment")
                    or ""
                ).strip()
            if not mpn:
                mpn = str(result.get("name") or component_name or "").strip()
            explicit = (
                [{"manufacturer": mfr, "part_number": mpn, "designators": ""}]
                if mpn
                else []
            )
            return tag_response(
                result,
                explicit_parts=explicit,
                context="lib_get_component_details",
            )
        return result

    @mcp.tool()
    async def lib_audit_styles(
        library_path: Optional[str] = None,
        with_comment: bool = False,
        with_parameters: bool = False,
        with_pins: bool = False,
        expect_designator_font_id: Optional[int] = None,
        expect_designator_color: Optional[int] = None,
        limit: int = 5000,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        """Bulk visual-style audit across every component in a library.

        Walks the focused .SchLib (or one specified by ``library_path``)
        component-by-component and emits each component's designator
        style record. Comment / parameter_styles / pins are opt-in via
        the ``with_*`` flags so the default response stays compact:
        designator alone is ~120 bytes per component, so a 2000-symbol
        library is ~240 KB without filters.

        Filter mode: pass ``expect_designator_font_id`` and/or
        ``expect_designator_color`` and the response only contains
        components whose designator does NOT match the expected style.
        That makes the audit case (find every symbol that doesn't use
        Times New Roman 10pt navy) a single round-trip with bounded
        output.

        ``timeout`` overrides the bridge default. A 2000-symbol audit
        with no opt-in flags finishes well under the 10s default; pass
        a larger value if you flip on ``with_parameters`` and the lib
        has heavy parameter dicts.

        Args:
            library_path: .SchLib path. Defaults to focused doc.
            with_comment: Include comment style record per component.
            with_parameters: Include parameter_styles array per component.
            with_pins: Include pins array per component.
            expect_designator_font_id: Filter; trim components where
                designator.font_id equals this value.
            expect_designator_color: Filter; trim components where
                designator.color equals this BGR int (e.g. 8388608 for
                navy / 0x000080 in BGR-packed form).
            limit: Cap on emitted entries. Default 5000.
            timeout: Per-call bridge poll timeout override (seconds).

        Returns:
            Dict with library_path, count (emitted), mismatch_count
            (subset that failed the filter), limit, truncated,
            filter_applied, and components: list of
            {name, designator:{...}, mismatched, comment?:{...},
             pins?:[...], parameter_styles?:[...]}.
        """
        bridge = get_bridge()
        params: dict[str, Any] = {"limit": str(limit)}
        if library_path:
            params["library_path"] = library_path
        if with_comment:
            params["with_comment"] = "true"
        if with_parameters:
            params["with_parameters"] = "true"
        if with_pins:
            params["with_pins"] = "true"
        if expect_designator_font_id is not None:
            params["expect_designator_font_id"] = str(expect_designator_font_id)
        if expect_designator_color is not None:
            params["expect_designator_color"] = str(expect_designator_color)
        result = await bridge.send_command_async(
            "library.audit_styles", params, timeout=timeout,
        )
        return result or {}

    @mcp.tool()
    async def lib_set_label_format(
        target: str = "designator",
        font_id: Optional[int] = None,
        color: Optional[int] = None,
        is_hidden: Optional[bool] = None,
        orientation: Optional[int] = None,
        justification: Optional[int] = None,
        component_name: Optional[str] = None,
        library_path: Optional[str] = None,
        only_mismatched: bool = True,
        limit: int = 5000,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        """Bulk or single-component label-style writer for SchLib symbols.

        Sets any subset of {font_id, color, is_hidden, orientation,
        justification} on a target ISch_Label (designator, comment, or
        a specific named parameter) for either one component
        (``component_name`` set) or every component in the library
        (``component_name`` omitted).

        Symmetric counterpart to lib_audit_styles' filter mode: with
        ``only_mismatched=True`` (default), components whose target
        label already matches every supplied field are skipped, so
        re-running the call after a partial application is idempotent.

        The whole edit batch is wrapped in a single Altium undo step
        and saves are deferred (`save_all` flushes the .SchLib).

        Args:
            target: Which label to format. ``"designator"`` (default),
                ``"comment"``, or ``"parameter:<Name>"`` (e.g.,
                ``"parameter:Manufacturer"``).
            font_id: New font ID. Resolve via get_font_id /
                get_font_spec if you need to convert from
                {name, size, bold, italic}.
            color: New BGR-packed color int. Navy ``#000080`` is
                ``8388608`` (0x800000 in BGR).
            is_hidden: Hide / show the label.
            orientation: 0/90/180/270 (Altium's TRotationBy90 enum).
            justification: Altium label justification enum.
            component_name: When set, applies only to that one
                component. Omit for bulk-walk.
            library_path: .SchLib path. Defaults to focused doc.
            only_mismatched: When True (default) skip components
                already matching the target style. Set False to
                rewrite unconditionally.
            limit: Cap on processed components in bulk mode.
            timeout: Per-call bridge poll timeout override.

        Returns:
            Dict with library_path, target, scope ("single"|"bulk"),
            total, modified, already_compliant, missing_target,
            failed, limit, truncated.
        """
        if (font_id is None and color is None and is_hidden is None
                and orientation is None and justification is None):
            raise InvalidParameterError(
                "At least one of font_id / color / is_hidden / "
                "orientation / justification must be supplied"
            )
        bridge = get_bridge()
        params: dict[str, Any] = {
            "target": target,
            "limit": str(limit),
        }
        if font_id is not None:
            params["font_id"] = str(font_id)
        if color is not None:
            params["color"] = str(color)
        if is_hidden is not None:
            params["is_hidden"] = "true" if is_hidden else "false"
        if orientation is not None:
            params["orientation"] = str(orientation)
        if justification is not None:
            params["justification"] = str(justification)
        if component_name:
            params["component_name"] = component_name
        if library_path:
            params["library_path"] = library_path
        if not only_mismatched:
            params["only_mismatched"] = "false"
        result = await bridge.send_command_async(
            "library.set_label_format", params, timeout=timeout,
        )
        return result or {}

    @mcp.tool()
    async def lib_set_label_formats(
        ops: list[dict[str, Any]],
        component_name: Optional[str] = None,
        library_path: Optional[str] = None,
        only_mismatched: bool = True,
        limit: int = 5000,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        """Multi-target label-style writer for SchLib symbols.

        Same job as ``lib_set_label_format`` but applies SEVERAL
        target/style ops in one IPC round-trip. The library is
        opened once and walked once; every op is applied to each
        component in turn. Five sequential single-target calls
        collapse into one trip plus one library walk -- which is
        the dominant cost on large libraries (each individual call
        runs an IPC, a workspace lookup, a doc-focus check and a
        CompInfoReader walk).

        Each ``op`` is a dict with the same shape as the
        single-target tool's args -- no field has a built-in default,
        only the ones you supply get written:

            {"target": "designator",         # required
             "font_id": <int>,               # optional
             "color":   <int>,               # optional, BGR-packed
             "is_hidden": False,             # optional
             "orientation": <int>,           # optional, 0/90/180/270
             "justification": <int>}         # optional

        At least one style field must be set per op. Targets are
        ``"designator"``, ``"comment"``, or ``"parameter:<Name>"``.
        Use ``get_font_id`` / ``get_font_spec`` to resolve the font_id
        for {name, size, bold, italic} from the active library's font
        table; that keeps the call neutral to any particular library's
        style choices.

        Args:
            ops: List of per-target style ops. Must be non-empty.
                Targets may not contain the wire separators ``;``
                or ``~~``.
            component_name: When set, applies only to that one
                component. Omit for bulk-walk across the library.
            library_path: .SchLib path. Defaults to focused doc.
            only_mismatched: When True (default) skip labels
                already matching the target style. Applies
                globally to every op.
            limit: Cap on processed components in bulk mode.
            timeout: Per-call bridge poll timeout override.

        Returns:
            Dict with library_path, scope ("single"|"bulk"),
            total, limit, truncated, and an ``ops`` array each
            with target, modified, already_compliant,
            missing_target, failed.
        """
        if not ops:
            raise InvalidParameterError("ops must be a non-empty list")
        encoded_ops: list[str] = []
        for i, op in enumerate(ops):
            if not isinstance(op, dict):
                raise InvalidParameterError(f"ops[{i}] must be a dict")
            target = op.get("target", "designator")
            if (not isinstance(target, str) or ";" in target
                    or "~~" in target):
                raise InvalidParameterError(
                    f"ops[{i}].target must be a string without "
                    "';' or '~~'"
                )
            parts = [f"target={target}"]
            style_set = False
            if op.get("font_id") is not None:
                parts.append(f"font_id={int(op['font_id'])}")
                style_set = True
            if op.get("color") is not None:
                parts.append(f"color={int(op['color'])}")
                style_set = True
            if op.get("is_hidden") is not None:
                parts.append(
                    "is_hidden="
                    + ("true" if op["is_hidden"] else "false")
                )
                style_set = True
            if op.get("orientation") is not None:
                parts.append(f"orientation={int(op['orientation'])}")
                style_set = True
            if op.get("justification") is not None:
                parts.append(f"justification={int(op['justification'])}")
                style_set = True
            if not style_set:
                raise InvalidParameterError(
                    f"ops[{i}] must set at least one of font_id / "
                    "color / is_hidden / orientation / justification"
                )
            encoded_ops.append(";".join(parts))
        bridge = get_bridge()
        params: dict[str, Any] = {
            "ops": "~~".join(encoded_ops),
            "limit": str(limit),
        }
        if component_name:
            params["component_name"] = component_name
        if library_path:
            params["library_path"] = library_path
        if not only_mismatched:
            params["only_mismatched"] = "false"
        result = await bridge.send_command_async(
            "library.set_label_formats", params, timeout=timeout,
        )
        return result or {}

    @mcp.tool()
    async def lib_batch_set_params(
        assignments: list[dict[str, str]],
        library_path: Optional[str] = None,
    ) -> dict[str, Any]:
        """Batch set parameters on library components.

        Each assignment sets one parameter on one component.
        If the parameter exists it is updated; if not it is created.

        Args:
            assignments: List of dicts with keys:
                - component_name: Name of the component in the library
                - param_name: Parameter name (e.g., "Partnumber", "Manufacturer")
                - param_value: Value to set
            library_path: Path to library (uses active library if not specified)

        Returns:
            Dictionary with counts of updated, created, and failed assignments
        """
        config = get_config()
        config.ensure_workspace()
        batch_path = config.workspace_dir / "batch_params.txt"

        # Validate keys and values before writing
        required_keys = {"component_name", "param_name", "param_value"}
        for i, a in enumerate(assignments):
            missing = required_keys - set(a.keys())
            if missing:
                raise InvalidParameterError(
                    f"Assignment {i} is missing required keys: {', '.join(sorted(missing))}"
                )
            for key in required_keys:
                if "|" in str(a[key]):
                    raise InvalidParameterError(
                        f"Assignment {i}: '{key}' value contains pipe character '|' which would corrupt the batch file"
                    )

        with open(batch_path, "w", encoding="latin-1") as f:
            for a in assignments:
                f.write(f"{a['component_name']}|{a['param_name']}|{a['param_value']}\n")

        bridge = get_bridge()
        params = {"batch_file": str(batch_path)}
        if library_path:
            params["library_path"] = library_path
        result = await bridge.send_command_async(
            "library.batch_set_params", params, timeout=120.0
        )
        return result

    @mcp.tool()
    async def lib_batch_rename(
        assignments: list[dict[str, str]],
        library_path: Optional[str] = None,
    ) -> dict[str, Any]:
        """Batch rename components in a schematic library.

        Each assignment renames one component from old_name to new_name.

        Args:
            assignments: List of dicts with keys:
                - old_name: Current name of the component in the library
                - new_name: New name for the component
            library_path: Path to library (uses active library if not specified)

        Returns:
            Dictionary with counts of renamed and failed assignments
        """
        config = get_config()
        config.ensure_workspace()
        batch_path = config.workspace_dir / "batch_rename.txt"

        # Validate keys and values before writing
        required_keys = {"old_name", "new_name"}
        for i, a in enumerate(assignments):
            missing = required_keys - set(a.keys())
            if missing:
                raise InvalidParameterError(
                    f"Assignment {i} is missing required keys: {', '.join(sorted(missing))}"
                )
            for key in required_keys:
                if "|" in str(a[key]):
                    raise InvalidParameterError(
                        f"Assignment {i}: '{key}' value contains pipe character '|' which would corrupt the batch file"
                    )

        with open(batch_path, "w", encoding="latin-1") as f:
            for a in assignments:
                f.write(f"{a['old_name']}|{a['new_name']}\n")

        bridge = get_bridge()
        params = {"batch_file": str(batch_path)}
        if library_path:
            params["library_path"] = library_path
        result = await bridge.send_command_async(
            "library.batch_rename", params, timeout=120.0
        )
        return result

    @mcp.tool()
    async def lib_diff_libraries(
        library_a: str,
        library_b: str,
    ) -> dict[str, Any]:
        """Compare two schematic libraries and report differences.

        Returns which components are only in library A, only in B, or shared.

        Args:
            library_a: Full path to the first SchLib file
            library_b: Full path to the second SchLib file

        Returns:
            Dictionary with only_in_a, only_in_b, common arrays,
            and count_a, count_b, only_a, only_b, shared counts
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "library.diff_libraries",
            {"library_a": library_a, "library_b": library_b},
            timeout=60.0,
        )
        return result

    @mcp.tool()
    async def lib_add_symbol_arc(
        x_center: int,
        y_center: int,
        radius: int,
        start_angle: float = 0,
        end_angle: float = 360,
        width: int = 1,
    ) -> dict[str, Any]:
        """Add an arc to the current library symbol.

        Args:
            x_center: Center X coordinate in mils
            y_center: Center Y coordinate in mils
            radius: Arc radius in mils
            start_angle: Start angle in degrees (0 = right, 90 = up)
            end_angle: End angle in degrees
            width: Line width (0=zero, 1=small, 2=medium, 3=large)

        Returns:
            Dictionary confirming arc addition
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "library.add_symbol_arc",
            {
                "x_center": _snap(int(x_center)),
                "y_center": _snap(int(y_center)),
                "radius": _snap(int(radius)),
                "start_angle": start_angle,
                "end_angle": end_angle,
                "width": width,
            },
        )
        return result

    @mcp.tool()
    async def lib_add_symbol_polygon(
        vertices: str,
    ) -> dict[str, Any]:
        """Add a polygon (filled shape) to the current library symbol.

        Args:
            vertices: Comma-separated x,y coordinate pairs in mils.
                Example: "0,0,100,0,100,100,0,100" creates a square with
                vertices at (0,0), (100,0), (100,100), (0,100).
                Minimum 3 vertices (6 values) required.

        Returns:
            Dictionary confirming polygon addition with vertex count
        """
        # Snap each (x, y) vertex pair to the schematic 100-mil grid.
        # Off-grid polygon vertices look ragged and don't align with
        # the pin grid the rest of the symbol uses.
        try:
            coords = [int(v.strip()) for v in vertices.split(",") if v.strip()]
            if len(coords) >= 6 and len(coords) % 2 == 0:
                snapped = [_snap(c) for c in coords]
                vertices = ",".join(str(c) for c in snapped)
        except (ValueError, AttributeError):
            # Pass through; bridge will reject malformed vertices itself.
            pass
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "library.add_symbol_polygon",
            {"vertices": vertices},
        )
        return result

    @mcp.tool()
    async def lib_set_component_description(
        component_name: str,
        description: str,
    ) -> dict[str, Any]:
        """Set the description field on a library component.

        Args:
            component_name: Name of the component in the active library
            description: New description text

        Returns:
            Dictionary confirming the description was set
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "library.set_component_description",
            {"component_name": component_name, "description": description},
        )
        return result

    @mcp.tool()
    async def lib_get_pin_list() -> dict[str, Any]:
        """Get all pins of the current library component.

        DATASHEET DISCIPLINE: Pin name + electrical_type from the
        symbol can be wrong, especially on libraries that have been
        edited by hand or imported from third-party sources. Before
        relying on a pin's function for any decision, fetch the
        manufacturer datasheet and verify against its pin-description
        table. The response carries `_datasheet_guidance` +
        `_datasheet_parts`.

        Returns:
            Dictionary with "count", "component" name, and "pins" array.
            Each pin has: designator, name, electrical_type, x, y,
            orientation, hidden. Plus `_datasheet_guidance` +
            `_datasheet_parts`.
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "library.get_pin_list", {}
        )
        if isinstance(result, dict):
            comp = str(result.get("component") or "").strip()
            explicit = (
                [{"manufacturer": "", "part_number": comp, "designators": ""}]
                if comp
                else []
            )
            return tag_response(
                result, explicit_parts=explicit, context="lib_get_pin_list"
            )
        return result

    @mcp.tool()
    async def lib_copy_component(
        source_name: str,
        new_name: Optional[str] = None,
        source_library: Optional[str] = None,
        dest_library: Optional[str] = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Copy a component WITHIN or BETWEEN schematic libraries.

        Replicates the source component (all pins, graphics, parameters)
        and adds it to the destination library. When ``dest_library`` is
        omitted or equals ``source_library`` this behaves as the original
        same-library duplicate. When ``dest_library`` differs, the
        component is copied across libraries -- the source library is
        focused for the replicate, then the destination is focused and
        the clone is added there. The destination ends focused with the
        new component selected. Save is deferred (call ``save_all`` to
        flush).

        Args:
            source_name: lib_ref of the component to copy.
            new_name: lib_ref for the clone. Defaults to ``source_name``
                (natural choice for cross-library copies that should
                keep their identity).
            source_library: .SchLib path to read from. Defaults to the
                currently focused document.
            dest_library: .SchLib path to write to. Omit (or pass the
                same path as ``source_library``) for a same-library
                duplicate.
            overwrite: When True, a component already named ``new_name``
                in the destination is removed first. Default False
                returns ``NAME_EXISTS`` and changes nothing.

        Returns:
            Dictionary with ``success``, ``source``, ``new_name``,
            ``source_library``, ``dest_library``, ``same_library`` and
            ``overwrote`` flags.
        """
        bridge = get_bridge()
        params: dict[str, Any] = {"source_name": source_name}
        if new_name:
            params["new_name"] = new_name
        if source_library:
            params["source_library"] = source_library
        if dest_library:
            params["dest_library"] = dest_library
        if overwrite:
            params["overwrite"] = "true"
        result = await bridge.send_command_async(
            "library.copy_component", params,
        )
        return result

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Schematic / PCB rendering tools.

Independent of any third-party Altium parser. Geometry is pulled from
the running Altium session via the existing DelphiScript bridge, then
rendered to SVG in pure Python. Output is interactive-ready (``data-*``
attributes on every group) so a dashboard or LLM client can hook
hover / click / cross-probe events without re-parsing.

v1 surface:
  - ``sch_render_svg`` : active SchDoc -> SVG file.

Queued for follow-up turns (no stubs shipped to keep the surface honest):
  - ``pcb_render_svg`` : per-layer PCB rendering.
  - ``sch_render_symbols``: walk each component's primitives for full
    symbol fidelity (current v1 draws labelled bounding-box bodies
    with pin stubs).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..bridge import get_bridge
from ..config import get_config
from ..render import (
    render_sch_svg, SchRenderOptions,
    render_pcb_svg, PcbRenderOptions,
)
from ..render.bom_html import render_bom_html


def register_render_tools(mcp):
    """Register rendering tools with the MCP server."""

    @mcp.tool()
    async def sch_render_svg(
        output_path: Optional[str] = None,
        margin_mils: int = 200,
        flip_y: bool = True,
    ) -> dict[str, Any]:
        """Render the active SchDoc to an SVG file (in-house renderer).

        Pulls every component, pin, wire, junction, net label, port and
        power port from the active schematic via the bridge, then emits
        a self-contained SVG with ``data-designator``, ``data-net``,
        ``data-pin`` attributes on every meaningful group so a downstream
        consumer (the web dashboard, an LLM tool, a CI check) can hook
        interaction without re-parsing.

        v1 draws components as labelled bounding-box bodies with pin
        stubs from the body to the electrical end. Full symbol-internal
        primitives (rectangles / lines / arcs inside each symbol) are
        deferred to v2 -- the v1 output is recognisable and ready for
        interactive overlay but does not yet draw the actual symbol art.

        Args:
            output_path: Where to write the SVG. Defaults to
                ``<workspace>/<docname>.svg``.
            margin_mils: Padding around the geometry bbox in mils
                (default 200).
            flip_y: When True (default) flip the Y axis so the output
                reads in Altium's conventional bottom-left-origin space
                rather than SVG's top-left-origin.

        Returns:
            Dict with ``ok``, ``svg_path``, ``doc``, ``counts``
            (one entry per primitive type).
        """
        bridge = get_bridge()
        geometry = await bridge.send_command_async(
            "generic.get_sch_geometry", {}, timeout=60.0,
        )
        if not isinstance(geometry, dict):
            return {"ok": False, "reason": "no geometry returned"}
        opts = SchRenderOptions(margin=margin_mils, flip_y=flip_y)
        svg = render_sch_svg(geometry, opts)

        doc = geometry.get("doc") or "schematic"
        # Strip directory components if Altium handed back a full path;
        # we write under the workspace by default.
        doc_name = Path(doc).name or "schematic"
        if not doc_name.lower().endswith(".svg"):
            doc_name = (
                Path(doc_name).with_suffix(".svg").name
                if doc_name else "schematic.svg"
            )

        if output_path:
            target = Path(output_path)
        else:
            target = get_config().workspace_dir / doc_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(svg, encoding="utf-8")

        return {
            "ok": True,
            "svg_path": str(target),
            "doc": geometry.get("doc"),
            "counts": geometry.get("counts") or {},
            "bytes": len(svg),
        }

    @mcp.tool()
    async def pcb_render_svg(
        output_path: Optional[str] = None,
        layers: Optional[list[str]] = None,
        margin_mils: int = 250,
        flip_y: bool = True,
        show_drills: bool = True,
        show_texts: bool = True,
        show_designators: bool = True,
        interactive_legend: bool = True,
        background: str = "#1f2937",
    ) -> dict[str, Any]:
        """Render the active PcbDoc to an SVG file (in-house renderer).

        Pulls the board outline, every track, arc, pad, via, and text
        primitive from the active PCB via the bridge, then emits a
        per-layer SVG. Layers are grouped in render order (KeepOut at
        bottom, then bottom copper, top copper, overlays, outline on
        top, drills last) and every track / arc / pad / via group
        carries ``data-net``, ``data-layer``, ``data-shape`` so a
        downstream dashboard or LLM tool can cross-probe or net-
        highlight without re-parsing.

        v1 surface: board outline + tracks + arcs + pads (round / rect /
        octagonal / rounded-rect) + vias + texts on overlays. Regions
        (polygon fills / copper pours) and component-body silkscreen
        outlines are deferred to v1.1.

        Args:
            output_path: Where to write the SVG. Defaults to
                ``<workspace>/<docname>.svg``.
            layers: Restrict to these Altium layer names (e.g.
                ``["TopLayer", "TopOverlay", "KeepOutLayer"]``). When
                ``None`` (default) every known layer is rendered.
                ``MultiLayer`` (vias) and the outline are always drawn.
            margin_mils: Padding around the board bbox in mils.
            flip_y: When True (default) flip Y so the SVG reads in
                Altium's bottom-left-origin space.
            show_drills: Punch hole circles through pads / vias.
            show_texts: Render text primitives on overlay layers.
            background: Board-background fill (defaults to a slate
                review colour; pass ``"none"`` for transparent).

        Returns:
            Dict with ``ok``, ``svg_path``, ``counts`` (per primitive
            kind), ``bbox`` (in mils), ``bytes``.
        """
        bridge = get_bridge()
        geometry = await bridge.send_command_async(
            "generic.get_pcb_geometry", {}, timeout=120.0,
        )
        if not isinstance(geometry, dict):
            return {"ok": False, "reason": "no geometry returned"}

        opts = PcbRenderOptions(
            margin_mils=margin_mils, flip_y=flip_y, layers=layers,
            background=background, show_drills=show_drills,
            show_texts=show_texts, show_designators=show_designators,
            interactive_legend=interactive_legend,
        )
        svg = render_pcb_svg(geometry, opts)

        if output_path:
            target = Path(output_path)
        else:
            target = get_config().workspace_dir / "pcb.svg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(svg, encoding="utf-8")

        return {
            "ok": True,
            "svg_path": str(target),
            "counts": geometry.get("counts") or {},
            "bbox": geometry.get("bbox") or {},
            "bytes": len(svg),
        }

    @mcp.tool()
    async def export_bom_html(
        output_path: Optional[str] = None,
        title: str = "Bill of Materials",
        limit: int = 5000,
    ) -> dict[str, Any]:
        """Export the project BOM as a self-contained interactive HTML file.

        Standalone HTML page — no external CSS / JS / Altium runtime
        dependency. Open it in any browser; sortable columns, free-text
        filter, toggle between grouped (one row per value+footprint) and
        per-component layouts. A self-contained one-shot static export.

        Useful for emailing the BOM to a manufacturer, archiving with a
        board release, or sharing with a non-Altium reviewer.

        Args:
            output_path: Where to write the HTML. Defaults to
                ``<workspace>/bom.html``.
            title: Heading + ``<title>`` for the page.
            limit: Max components to include (default 5000; the BOM call
                supports up to ~10000 cleanly).

        Returns:
            Dict with ``ok``, ``html_path``, ``components`` (count
            written), ``bytes``.
        """
        bridge = get_bridge()
        bom = await bridge.send_command_async(
            "project.get_bom",
            {"limit": str(limit)},
        )
        if not isinstance(bom, dict):
            return {"ok": False, "reason": "no BOM returned"}

        # Resolve project name for the sub-heading -- best-effort, the
        # focused-project lookup isn't critical for the export to work.
        project_name = ""
        try:
            focused = await bridge.send_command_async(
                "project.get_focused", {})
            if isinstance(focused, dict):
                project_name = (focused.get("project_name")
                                or focused.get("name") or "")
        except Exception:
            pass

        html_str = render_bom_html(bom, title=title, project=project_name)

        if output_path:
            target = Path(output_path)
        else:
            target = get_config().workspace_dir / "bom.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html_str, encoding="utf-8")

        return {
            "ok": True,
            "html_path": str(target),
            "components": len(bom.get("components") or []),
            "bytes": len(html_str),
        }

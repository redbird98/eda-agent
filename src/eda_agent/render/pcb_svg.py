# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""PCB SVG renderer.

Takes the geometry payload produced by the Pascal ``Gen_GetPcbGeometry``
handler (board outline + tracks + arcs + pads + vias + texts, in mils,
each tagged with its Altium layer name) and emits a per-layer SVG with
the layers grouped, z-ordered for review, and CSS-toggleable.

Interactive-ready: every track / arc / pad / via group carries
``data-net``, ``data-layer``, and ``data-shape`` so a downstream
dashboard / LLM tool can hook cross-probe and net-highlight without
having to re-parse the board.
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass, field
from typing import Any, Iterable


# Render z-order (bottom-up). Each entry maps an Altium layer name (as
# returned by GetLayerString) to a render bucket. Anything else falls
# through into an "other" group.
_LAYER_ORDER = [
    "KeepOutLayer",
    "BottomSolder",
    "BottomLayer",
    "TopLayer",
    "TopSolder",
    "BottomOverlay",
    "TopOverlay",
]

# Default per-layer fill / stroke. Tuned for a dark review background;
# overridable through SchRenderOptions.layer_colors. PCB convention uses
# red for top, blue for bottom, yellow for overlays.
_DEFAULT_COLORS: dict[str, str] = {
    "TopLayer":      "#c0392b",
    "BottomLayer":   "#2980b9",
    "TopOverlay":    "#f1c40f",
    "BottomOverlay": "#f39c12",
    "TopSolder":     "#27ae60",
    "BottomSolder":  "#16a085",
    "KeepOutLayer":  "#8e44ad",
    "MultiLayer":    "#bdc3c7",
    "Outline":       "#ecf0f1",
    "ViaPlated":     "#bdc3c7",
    "Drill":         "#000000",
}


@dataclass
class PcbRenderOptions:
    """Tuning knobs for the PCB SVG output."""
    margin_mils: int = 250
    flip_y: bool = True
    layers: list[str] | None = None        # None = render all known layers
    background: str = "#1f2937"            # board background fill
    outline_stroke_mils: float = 8.0
    layer_colors: dict[str, str] = field(default_factory=dict)
    fade_others_opacity: float = 0.35      # objects on un-z-ordered layers
    show_drills: bool = True
    show_texts: bool = True
    show_designators: bool = True          # render per-component designator labels
    pad_label_min_mils: float = 30.0       # below this size pad numbers are hidden
    interactive_legend: bool = True        # embed a click-to-toggle layer legend


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def render_pcb_svg(geometry: dict[str, Any],
                   options: PcbRenderOptions | None = None) -> str:
    """Render the PCB geometry payload to a self-contained SVG string."""
    opt = options or PcbRenderOptions()
    pieces: list[str] = []
    pieces.extend(_compute_viewbox_and_root(geometry, opt))
    pieces.append(_embedded_style(opt))
    pieces.append(_render_background(geometry, opt))

    # Bucket geometry by layer so we can z-order.
    by_layer = _bucket_by_layer(geometry, opt)

    layer_list = list(_LAYER_ORDER)
    # Append any unknown layers we saw (so we still draw them).
    for lay in by_layer.keys():
        if lay not in layer_list and lay != "_outline":
            layer_list.append(lay)

    requested = set(opt.layers) if opt.layers else None
    for lay in layer_list:
        if requested is not None and lay not in requested:
            continue
        bucket = by_layer.get(lay)
        if not bucket:
            continue
        pieces.append(_render_layer(lay, bucket, opt))

    # Board outline drawn on top of layers, beneath designators / drills.
    pieces.append(_render_outline(geometry.get("outline") or [], opt))

    # Per-component designator labels (virtual "Designators" pseudo-layer).
    if opt.show_designators:
        pieces.append(_render_designators(geometry.get("components") or [], opt))

    # Drills go last (white holes punch through everything).
    if opt.show_drills:
        pieces.append(_render_drills(geometry, opt))

    if opt.flip_y:
        pieces.append("</g>")  # close outer Y-flip wrapper

    # Interactive legend goes OUTSIDE the Y-flip wrapper so its HTML
    # checkboxes render upright. Modern browsers handle this fine;
    # static viewers ignore foreignObject + script and the SVG still
    # shows every layer.
    if opt.interactive_legend:
        pieces.append(_render_layer_legend(geometry, opt))

    pieces.append("</svg>")
    return "".join(p for p in pieces if p)


# ---------------------------------------------------------------------------
# viewBox + root
# ---------------------------------------------------------------------------


def _compute_viewbox_and_root(geometry: dict[str, Any],
                              opt: PcbRenderOptions) -> list[str]:
    bbox = geometry.get("bbox") or {}
    x1 = float(bbox.get("x1", 0))
    y1 = float(bbox.get("y1", 0))
    x2 = float(bbox.get("x2", 1000))
    y2 = float(bbox.get("y2", 1000))
    if x1 == x2 or y1 == y2:
        x1, y1, x2, y2 = -500, -500, 500, 500
    x1 -= opt.margin_mils
    y1 -= opt.margin_mils
    x2 += opt.margin_mils
    y2 += opt.margin_mils
    w = x2 - x1
    h = y2 - y1
    if opt.flip_y:
        view = f"{x1} {-y2} {w} {h}"
    else:
        view = f"{x1} {y1} {w} {h}"
    pieces: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}" '
        f'preserveAspectRatio="xMidYMid meet">'
    ]
    if opt.flip_y:
        pieces.append('<g transform="scale(1,-1)">')
    return pieces


# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------


def _color_for(layer: str, opt: PcbRenderOptions) -> str:
    return opt.layer_colors.get(layer) or _DEFAULT_COLORS.get(layer, "#7f8c8d")


def _embedded_style(opt: PcbRenderOptions) -> str:
    rules = [
        "  .layer { mix-blend-mode: screen; }",
        f"  .layer-other {{ opacity: {opt.fade_others_opacity}; }}",
        "  .outline { fill: none; }",
        "  .drill { fill: #000; }",
        "  .pad-num { font-family: 'JetBrains Mono', monospace; "
        "fill: rgba(255,255,255,0.85); pointer-events: none; }",
        "  .pcb-text { font-family: 'JetBrains Mono', monospace; "
        "pointer-events: none; }",
        "  .track:hover, .arc:hover, .pad:hover, .via:hover { "
        "filter: brightness(1.6); cursor: pointer; }",
    ]
    return "<style>" + "\n".join(rules) + "</style>"


def _render_background(geometry: dict[str, Any],
                       opt: PcbRenderOptions) -> str:
    bbox = geometry.get("bbox") or {}
    x1 = float(bbox.get("x1", 0)) - opt.margin_mils
    y1 = float(bbox.get("y1", 0)) - opt.margin_mils
    x2 = float(bbox.get("x2", 0)) + opt.margin_mils
    y2 = float(bbox.get("y2", 0)) + opt.margin_mils
    return (
        f'<rect class="bg" x="{x1}" y="{y1}" '
        f'width="{x2 - x1}" height="{y2 - y1}" '
        f'fill="{opt.background}"/>'
    )


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------


def _bucket_by_layer(geometry: dict[str, Any],
                     opt: PcbRenderOptions
                     ) -> dict[str, dict[str, list[dict[str, Any]]]]:
    out: dict[str, dict[str, list[dict[str, Any]]]] = {}

    def _push(layer: str, kind: str, item: dict[str, Any]) -> None:
        out.setdefault(layer, {}).setdefault(kind, []).append(item)

    # Regions go in first so they paint BENEATH tracks/arcs on the same
    # layer -- that's what gives the rendered board the proper "copper
    # fill" look instead of bare traces over background.
    for r in geometry.get("regions") or []:
        _push(r.get("layer", "Unknown"), "regions", r)
    for t in geometry.get("tracks") or []:
        _push(t.get("layer", "Unknown"), "tracks", t)
    for a in geometry.get("arcs") or []:
        _push(a.get("layer", "Unknown"), "arcs", a)
    for p in geometry.get("pads") or []:
        _push(p.get("layer", "Unknown"), "pads", p)
    if opt.show_texts:
        for tx in geometry.get("texts") or []:
            _push(tx.get("layer", "Unknown"), "texts", tx)
    # Vias span layers -- bucket them under "MultiLayer" so they always render.
    for v in geometry.get("vias") or []:
        _push("MultiLayer", "vias", v)
    return out


# ---------------------------------------------------------------------------
# Per-layer rendering
# ---------------------------------------------------------------------------


def _render_layer(layer: str, bucket: dict[str, list[dict[str, Any]]],
                  opt: PcbRenderOptions) -> str:
    color = _color_for(layer, opt)
    is_known = layer in _LAYER_ORDER or layer == "MultiLayer"
    klass = "layer" if is_known else "layer layer-other"
    out: list[str] = [f'<g class="{klass}" data-layer="{_xml(layer)}">']

    # Regions go first so tracks / pads / arcs paint over them.
    for r in bucket.get("regions", []):
        pts = " ".join(f"{p[0]},{p[1]}" for p in (r.get("pts") or []))
        if not pts:
            continue
        net = r.get("net", "")
        out.append(
            f'<polygon class="region" data-net="{_xml(net)}" '
            f'points="{pts}" fill="{color}" stroke="none" '
            f'fill-opacity="0.85"/>'
        )

    for t in bucket.get("tracks", []):
        net = t.get("net", "")
        out.append(
            f'<line class="track" data-net="{_xml(net)}" '
            f'x1="{t.get("x1", 0)}" y1="{t.get("y1", 0)}" '
            f'x2="{t.get("x2", 0)}" y2="{t.get("y2", 0)}" '
            f'stroke="{color}" stroke-width="{t.get("width", 6)}" '
            f'stroke-linecap="round"/>'
        )

    for a in bucket.get("arcs", []):
        net = a.get("net", "")
        path = _arc_path(a)
        if not path:
            continue
        out.append(
            f'<path class="arc" data-net="{_xml(net)}" '
            f'd="{path}" stroke="{color}" fill="none" '
            f'stroke-width="{a.get("width", 6)}" stroke-linecap="round"/>'
        )

    for p in bucket.get("pads", []):
        out.append(_render_pad(p, color, opt))

    for v in bucket.get("vias", []):
        out.append(_render_via(v, opt))

    for tx in bucket.get("texts", []):
        out.append(_render_text(tx, color, opt))

    out.append("</g>")
    return "".join(out)


def _render_pad(p: dict[str, Any], color: str, opt: PcbRenderOptions) -> str:
    x = float(p.get("x", 0))
    y = float(p.get("y", 0))
    xs = float(p.get("x_size", 0))
    ys = float(p.get("y_size", 0))
    rot = float(p.get("rotation", 0) or 0)
    shape = (p.get("shape") or "Round").lower()
    name = p.get("name", "") or ""
    net = p.get("net", "") or ""
    transform = f' transform="rotate({rot} {x} {y})"' if rot else ""

    if shape.startswith("round") and "rect" not in shape:
        # Round pad (XSize is the diameter).
        body = (
            f'<ellipse cx="{x}" cy="{y}" '
            f'rx="{xs / 2}" ry="{ys / 2}" fill="{color}"/>'
        )
    elif shape.startswith("oct"):
        # Regular octagon inscribed in the X/YSize rectangle.
        body = _octagon_path(x, y, xs, ys, color)
    elif shape.startswith("roundedrect"):
        rxy = min(xs, ys) * 0.2
        body = (
            f'<rect x="{x - xs / 2}" y="{y - ys / 2}" '
            f'width="{xs}" height="{ys}" rx="{rxy}" ry="{rxy}" '
            f'fill="{color}"/>'
        )
    else:  # Rectangular
        body = (
            f'<rect x="{x - xs / 2}" y="{y - ys / 2}" '
            f'width="{xs}" height="{ys}" fill="{color}"/>'
        )

    comp = pad.get("comp", "") or ""
    # data-designator on the pad <g> lets the dashboard's existing
    # click handler open the owning component's drawer on a PCB click.
    # Free pads (fiducials, mounting holes) have comp="" and stay
    # net-only -- they shouldn't open a drawer because no component
    # owns them.
    des_attr = f' data-designator="{_xml(comp)}"' if comp else ""
    out = (
        f'<g class="pad" data-net="{_xml(net)}" data-shape="{_xml(shape)}" '
        f'data-name="{_xml(name)}"{des_attr}{transform}>'
        + body
    )
    # Pad number label (only if pad is big enough to read).
    if name and min(xs, ys) >= opt.pad_label_min_mils:
        font = max(8.0, min(xs, ys) * 0.35)
        out += _flippable_text(
            x, y,
            f'class="pad-num" font-size="{font}" '
            f'text-anchor="middle" dominant-baseline="middle"',
            name, opt,
        )
    out += "</g>"
    return out


def _render_via(v: dict[str, Any], opt: PcbRenderOptions) -> str:
    x = float(v.get("x", 0))
    y = float(v.get("y", 0))
    size = float(v.get("size", 0))
    net = v.get("net", "") or ""
    return (
        f'<g class="via" data-net="{_xml(net)}">'
        f'<circle cx="{x}" cy="{y}" r="{size / 2}" '
        f'fill="{_color_for("ViaPlated", opt)}"/></g>'
    )


def _render_text(tx: dict[str, Any], color: str, opt: PcbRenderOptions) -> str:
    text = tx.get("text", "") or ""
    if not text:
        return ""
    x = float(tx.get("x", 0))
    y = float(tx.get("y", 0))
    size = float(tx.get("size", 60) or 60)
    rot = float(tx.get("rotation", 0) or 0)
    attrs = (
        f'class="pcb-text" font-size="{size}" '
        f'text-anchor="start" dominant-baseline="auto" '
        f'fill="{color}"'
    )
    if opt.flip_y:
        # Counter-flip Y plus optional in-plane rotation in Altium-native
        # space (where +ccw is up). The outer scale(1,-1) inverts visual
        # rotation, so emit -rot for the local transform.
        return (
            f'<g transform="translate({x},{y}) scale(1,-1) rotate({-rot})">'
            f'<text {attrs} x="0" y="0">{_xml(text)}</text></g>'
        )
    return (
        f'<text {attrs} x="{x}" y="{y}" '
        f'transform="rotate({rot} {x} {y})">{_xml(text)}</text>'
    )


def _render_outline(outline: Iterable[dict[str, Any]],
                    opt: PcbRenderOptions) -> str:
    """Render the board outline as a path -- arc segments emit real SVG
    arc commands so curved board shapes (rounded corners, mounting
    cutouts) draw correctly instead of being flattened to chord lines.

    Each segment's vertex (vx, vy) is the END of that segment. Arc
    segments additionally carry center + angles, so we build an A
    command from the previous endpoint to the current vertex.
    """
    segs = list(outline)
    if not segs:
        return ""
    color = _color_for("Outline", opt)

    # First segment's vertex is the polygon start.
    parts: list[str] = []
    x0 = float(segs[0].get("x", 0))
    y0 = float(segs[0].get("y", 0))
    parts.append(f"M {x0} {y0}")
    for seg in segs[1:]:
        sx = float(seg.get("x", 0))
        sy = float(seg.get("y", 0))
        if seg.get("kind") == "arc":
            r = float(seg.get("radius", 0) or 0)
            a1 = float(seg.get("angle1", 0) or 0)
            a2 = float(seg.get("angle2", 0) or 0)
            sweep_deg = a2 - a1
            if sweep_deg < 0:
                sweep_deg += 360.0
            large = 1 if sweep_deg > 180.0 else 0
            # Outer wrapper flips Y, so visual CCW becomes CW in user
            # space -- sweep-flag=1 makes Altium-positive arcs render
            # correctly.
            parts.append(f"A {r} {r} 0 {large} 1 {sx} {sy}")
        else:
            parts.append(f"L {sx} {sy}")
    parts.append("Z")
    d = " ".join(parts)
    return (
        f'<g class="layer outline" data-layer="Outline">'
        f'<path class="outline" d="{d}" '
        f'stroke="{color}" stroke-width="{opt.outline_stroke_mils}" '
        f'fill="none" stroke-linejoin="round"/></g>'
    )


def _render_designators(components: Iterable[dict[str, Any]],
                        opt: PcbRenderOptions) -> str:
    """Render per-component designator labels on a virtual 'Designators'
    pseudo-layer. The actual silkscreen art lives in each footprint's
    overlay primitives (already rendered); this adds the identifying
    text so a reviewer can locate every part by name without zooming
    into the overlay.
    """
    out: list[str] = ['<g class="layer designators" data-layer="Designators">']
    color = _color_for("TopOverlay", opt)
    for c in components:
        des = c.get("des", "") or ""
        if not des:
            continue
        x = float(c.get("x", 0))
        y = float(c.get("y", 0))
        rot = float(c.get("rotation", 0) or 0)
        # Small text floating above the component origin. Size scales
        # mildly so a dense top view stays readable; v1.1 of v1.1 could
        # tune by component bbox.
        attrs = (
            f'class="designator" font-size="40" '
            f'text-anchor="middle" dominant-baseline="middle" '
            f'fill="{color}" font-family="JetBrains Mono, monospace" '
            f'font-weight="700" pointer-events="none"'
        )
        if opt.flip_y:
            out.append(
                f'<g data-designator="{_xml(des)}" '
                f'transform="translate({x},{y}) scale(1,-1) rotate({-rot})">'
                f'<text {attrs} x="0" y="0">{_xml(des)}</text></g>'
            )
        else:
            out.append(
                f'<g data-designator="{_xml(des)}">'
                f'<text {attrs} x="{x}" y="{y}" '
                f'transform="rotate({rot} {x} {y})">{_xml(des)}</text></g>'
            )
    out.append("</g>")
    return "".join(out)


def _render_drills(geometry: dict[str, Any], opt: PcbRenderOptions) -> str:
    """Punch black holes through pads + vias. Pads honour their
    HoleType (Round / Square / Slot); vias are always round.
    """
    out: list[str] = ['<g class="drills">']
    for p in geometry.get("pads") or []:
        h = float(p.get("hole_size", 0) or 0)
        if h <= 0:
            continue
        x = float(p.get("x", 0))
        y = float(p.get("y", 0))
        htype = (p.get("hole_type") or "Round").lower()
        if htype == "square":
            side = h
            out.append(
                f'<rect class="drill" x="{x - side / 2}" y="{y - side / 2}" '
                f'width="{side}" height="{side}"/>'
            )
        elif htype == "slot":
            # Slot is rounded-end rectangle: length = hole_size,
            # width = hole_width, rotated by hole_rotation about (x,y).
            length = h
            width = float(p.get("hole_width", h) or h)
            if width <= 0:
                width = h
            rot = float(p.get("hole_rotation", 0) or 0)
            rx = width / 2.0
            transform = f' transform="rotate({rot} {x} {y})"' if rot else ""
            out.append(
                f'<rect class="drill" x="{x - length / 2}" y="{y - width / 2}" '
                f'width="{length}" height="{width}" '
                f'rx="{rx}" ry="{rx}"{transform}/>'
            )
        else:
            out.append(
                f'<circle class="drill" cx="{x}" cy="{y}" r="{h / 2}"/>'
            )
    for v in geometry.get("vias") or []:
        h = float(v.get("hole_size", 0) or 0)
        if h <= 0:
            continue
        out.append(
            f'<circle class="drill" '
            f'cx="{v.get("x", 0)}" cy="{v.get("y", 0)}" r="{h / 2}"/>'
        )
    out.append("</g>")
    return "".join(out)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _arc_path(a: dict[str, Any]) -> str:
    cx = float(a.get("cx", 0))
    cy = float(a.get("cy", 0))
    r = float(a.get("r", 0) or 0)
    if r <= 0:
        return ""
    sa = float(a.get("start", 0) or 0)
    ea = float(a.get("end", 0) or 0)
    if ea < sa:
        ea += 360.0
    sweep = ea - sa
    full = abs(sweep - 360.0) < 0.01 or sweep <= 0.01
    sa_r = math.radians(sa)
    ea_r = math.radians(ea)
    sx = cx + r * math.cos(sa_r)
    sy = cy + r * math.sin(sa_r)
    ex = cx + r * math.cos(ea_r)
    ey = cy + r * math.sin(ea_r)
    if full:
        # Full circle as two halves so SVG renders it.
        return (
            f"M {cx + r} {cy} "
            f"A {r} {r} 0 1 1 {cx - r} {cy} "
            f"A {r} {r} 0 1 1 {cx + r} {cy}"
        )
    large = 1 if sweep > 180.0 else 0
    return f"M {sx} {sy} A {r} {r} 0 {large} 1 {ex} {ey}"


def _octagon_path(cx: float, cy: float, xs: float, ys: float,
                  color: str) -> str:
    # Octagon inscribed in the xs x ys rectangle; corners cut at 30% of
    # the half-size for a recognisable shape.
    hx = xs / 2.0
    hy = ys / 2.0
    cx_o = min(hx, hy) * 0.41
    pts = [
        (cx - hx + cx_o, cy - hy),
        (cx + hx - cx_o, cy - hy),
        (cx + hx,        cy - hy + cx_o),
        (cx + hx,        cy + hy - cx_o),
        (cx + hx - cx_o, cy + hy),
        (cx - hx + cx_o, cy + hy),
        (cx - hx,        cy + hy - cx_o),
        (cx - hx,        cy - hy + cx_o),
    ]
    pts_str = " ".join(f"{p[0]},{p[1]}" for p in pts)
    return f'<polygon points="{pts_str}" fill="{color}"/>'


def _flippable_text(x: float, y: float, attrs: str, text: Any,
                    opt: PcbRenderOptions) -> str:
    safe = _xml(text)
    if opt.flip_y:
        return (
            f'<g transform="translate({x},{y}) scale(1,-1)">'
            f'<text {attrs} x="0" y="0">{safe}</text></g>'
        )
    return f'<text {attrs} x="{x}" y="{y}">{safe}</text>'


def _render_layer_legend(geometry: dict[str, Any],
                         opt: PcbRenderOptions) -> str:
    """Embed an interactive layer-toggle legend.

    Built as a foreignObject containing HTML checkboxes (one per layer
    that actually appears in the geometry) + an inline <script> that
    wires up click->toggle on the matching ``<g data-layer="…">`` groups.
    In a static SVG viewer the foreignObject + script are ignored and
    every layer still renders -- the legend is a browser-only
    enhancement, not a load-bearing feature.
    """
    # Build the set of layers actually present so the legend doesn't
    # advertise empty toggles.
    layers: list[str] = []
    seen: set[str] = set()
    for kind in ("regions", "tracks", "arcs", "pads", "texts"):
        for o in geometry.get(kind) or []:
            lay = o.get("layer")
            if isinstance(lay, str) and lay and lay not in seen:
                seen.add(lay)
                layers.append(lay)
    # Always-present pseudo-layers we draw separately:
    for extra in ("MultiLayer", "Outline", "Designators"):
        if extra not in seen:
            layers.append(extra)
            seen.add(extra)
    # Order by the canonical layer z-order so the legend reads sensibly.
    ordered: list[str] = []
    for lay in (_LAYER_ORDER + ["MultiLayer", "Outline", "Designators"]):
        if lay in seen:
            ordered.append(lay)
    for lay in layers:
        if lay not in ordered:
            ordered.append(lay)

    bbox = geometry.get("bbox") or {}
    x1 = float(bbox.get("x1", 0)) - opt.margin_mils
    y2 = float(bbox.get("y2", 0)) + opt.margin_mils
    width = 2600
    height = max(160, 60 + len(ordered) * 80)

    rows = "".join(
        f'<label style="display:block; padding:2px 4px; cursor:pointer; '
        f'color:#fff; font:600 22px monospace;">'
        f'<input type="checkbox" class="layer-cb" data-layer="{_xml(lay)}" '
        f'checked style="margin-right:6px;">'
        f'<span style="display:inline-block; width:14px; height:14px; '
        f'background:{_color_for(lay, opt)}; '
        f'vertical-align:-2px; margin-right:6px;"></span>'
        f'{_xml(lay)}</label>'
        for lay in ordered
    )

    # foreignObject in the flipped frame would render upside-down, so
    # we put it OUTSIDE the wrapper at "natural" SVG coords. With Y
    # flipped, y2 is the visual top of the board; we offset upward.
    if opt.flip_y:
        fo_x = x1
        fo_y = -y2
    else:
        fo_x = x1
        fo_y = y2 - height

    return (
        f'<foreignObject class="legend-host" '
        f'x="{fo_x}" y="{fo_y}" width="{width}" height="{height}">'
        f'<div xmlns="http://www.w3.org/1999/xhtml" '
        f'style="background:rgba(0,0,0,0.7); border-radius:6px; '
        f'padding:8px 10px; box-shadow:0 4px 12px rgba(0,0,0,0.4);">'
        f'<div style="color:#fff; font:700 22px monospace; '
        f'margin-bottom:4px; opacity:0.85;">LAYERS</div>'
        f'{rows}'
        f'</div></foreignObject>'
        f'<script><![CDATA['
        f'document.querySelectorAll(".layer-cb").forEach(function(cb){{'
        f'cb.addEventListener("change",function(){{'
        f'var name=cb.getAttribute("data-layer");'
        f'document.querySelectorAll(\'g[data-layer="\'+name+\'"]\').forEach('
        f'function(g){{g.style.display=cb.checked?"":"none";}});'
        f'}});'
        f'}});'
        f']]></script>'
    )


def _xml(s: Any) -> str:
    return html.escape(str(s or ""), quote=True)

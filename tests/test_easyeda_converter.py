# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""EasyEDA converter: parse, normalize, and emit for KiCad and Altium.

Fully offline against a committed fixture. What is pinned down:

* the shape-string grammar from the vendor spec (``~`` attributes,
  ``^^`` pin segments, ``#@$`` shape separator);
* the unit and axis normalization, 10 mil units on a Y-down canvas
  becoming mils on a Y-up origin-relative frame, which is the single
  most breakable part of the whole converter;
* KiCad emission shape (.kicad_sym / .kicad_mod), including the Y flip
  that footprints need and symbols do not;
* the Altium install plan mapping onto real MCP tool names;
* warnings for geometry no target CAD can reproduce.

The fetch client is exercised without network by stubbing its opener.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eda_agent.libimport.easyeda import (
    build_altium_plan,
    footprint_to_kicad_mod,
    parse_component,
    symbol_to_kicad_sym,
)
from eda_agent.libimport.easyeda.shapes import (
    EASYEDA_UNIT_MIL,
    parse_footprint_shapes,
    parse_symbol_shapes,
    parse_points,
    split_shapes,
)

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "easyeda_soic8.json"


@pytest.fixture(scope="module")
def comp():
    return parse_component(json.loads(_FIXTURE.read_text(encoding="utf-8")))


# --------------------------- grammar ---------------------------------


def test_split_shapes_uses_the_documented_separator():
    assert split_shapes("A~1#@$B~2") == ["A~1", "B~2"]
    assert split_shapes("") == []


def test_parse_points_accepts_space_and_comma_forms():
    assert parse_points("1 2 3 4") == [(1.0, 2.0), (3.0, 4.0)]
    assert parse_points("1,2 3,4") == [(1.0, 2.0), (3.0, 4.0)]
    # An odd trailing value is dropped rather than crashing.
    assert parse_points("1 2 3") == [(1.0, 2.0)]


def test_pad_fields_follow_the_spec_order():
    pad = parse_footprint_shapes(
        "PAD~OVAL~10~20~30~40~2~~7~5~~90~gge1~11~~N")[0]
    assert pad.shape == "OVAL"
    assert (pad.cx, pad.cy) == (10.0, 20.0)
    assert (pad.width, pad.height) == (30.0, 40.0)
    assert pad.layer == 2
    assert pad.number == "7"
    assert pad.hole_radius == 5.0
    assert pad.rotation == 90.0
    assert pad.hole_length == 11.0
    assert pad.plated is False
    assert pad.is_through_hole and pad.is_slot


def test_pin_segments_parse_name_number_and_length():
    raw = ("P~show~1~1~370~290~0~gge10~0^^370~290^^M370,290h-10~#880000"
           "^^1~360~290~0~IN1~end~~~#00F^^1~372~287~0~1~start~~~#00F"
           "^^0~365~290^^0~")
    pin = parse_symbol_shapes(raw)[0]
    assert pin.number == "1"
    assert pin.name == "IN1"
    assert pin.electric == 1
    assert pin.length == 10.0          # from the "h-10" body path
    assert (pin.x, pin.y) == (370.0, 290.0)


def test_tolerates_missing_and_blank_trailing_fields():
    # Real documents omit trailing attributes freely.
    pad = parse_footprint_shapes("PAD~RECT~1~2~3~4")[0]
    assert pad.layer == 1 and pad.plated is True and pad.hole_radius == 0.0


# ----------------------- normalization -------------------------------


def test_units_and_axis_are_normalized(comp):
    """10 mil units, Y-down canvas -> mils, Y-up, origin relative."""
    pins = {p.number: p for p in comp.symbol.shapes if p.kind == "pin"}
    # Pin 1 sits at canvas (370, 290) with origin (400, 300):
    #   x = (370-400)*10 = -300 mil,  y = (300-290)*10 = +100 mil
    assert pins["1"].x == pytest.approx(-300.0)
    assert pins["1"].y == pytest.approx(100.0)
    assert pins["1"].length == pytest.approx(10 * EASYEDA_UNIT_MIL)
    # Pin 2 mirrors to the right of the origin.
    assert pins["2"].x == pytest.approx(300.0)


def test_footprint_pads_normalized_and_hole_is_a_radius(comp):
    pads = {p.number: p for p in comp.footprint.shapes if p.kind == "pad"}
    assert set(pads) == {"1", "2", "3"}
    # Pad 1 at (3985, 2985), origin (4000, 3000).
    assert pads["1"].cx == pytest.approx(-150.0)
    assert pads["1"].cy == pytest.approx(150.0)
    assert pads["1"].width == pytest.approx(200.0)
    assert pads["1"].height == pytest.approx(60.0)
    assert not pads["1"].is_through_hole
    # Pad 3 is multi-layer with a hole radius of 4 units = 40 mil.
    assert pads["3"].is_through_hole
    assert pads["3"].hole_radius == pytest.approx(40.0)


def test_metadata_extracted(comp):
    assert comp.lcsc_id == "C7420"
    assert comp.mpn == "TESTPART-8"
    assert comp.manufacturer == "ExampleSemi"
    assert comp.package == "SOIC-8_TEST"
    assert comp.symbol.prefix == "U"
    assert comp.footprint.name == "SOIC-8_TEST"


def test_rect_y_becomes_the_bottom_edge_after_the_flip(comp):
    rect = next(s for s in comp.symbol.shapes if s.kind == "rect")
    # Canvas top edge y=280 h=60 -> bottom edge at canvas 340,
    # which is (300-340)*10 = -400 mil in the Y-up frame.
    assert rect.y == pytest.approx(-400.0)
    assert rect.height == pytest.approx(600.0)


# --------------------------- KiCad -----------------------------------


def test_kicad_symbol_is_wellformed(comp):
    text = symbol_to_kicad_sym(comp)
    assert text.startswith("(kicad_symbol_lib")
    assert text.count("(") == text.count(")"), "unbalanced s-expression"
    assert '(symbol "TESTPART-8"' in text
    assert '(pin input line' in text
    assert '(number "1"' in text and '(number "2"' in text
    assert '"Manufacturer"' in text and '"LCSC"' in text


def test_kicad_footprint_is_wellformed_and_y_flipped(comp):
    text = footprint_to_kicad_mod(comp)
    assert text.startswith('(footprint "SOIC-8_TEST"')
    assert text.count("(") == text.count(")"), "unbalanced s-expression"
    # Pad 1 is at +150 mil Y in the neutral frame; .kicad_mod is Y-down,
    # so it must be emitted negative: 150 mil = 3.81 mm.
    assert "-3.81" in text
    # Through-hole pad 3 carries a drill and multi-layer copper.
    assert "thru_hole" in text and "(drill" in text
    assert '"*.Cu" "*.Mask"' in text
    # The unplated HOLE becomes an np_thru_hole pad.
    assert "np_thru_hole" in text


def test_kicad_units_are_millimetres(comp):
    text = footprint_to_kicad_mod(comp)
    # 200 mil pad width = 5.08 mm exactly.
    assert "(size 5.08 " in text


# --------------------------- Altium ----------------------------------


def test_altium_plan_maps_onto_real_tools(comp):
    plan = build_altium_plan(comp, r"C:\lib\T.SchLib", r"C:\lib\T.PcbLib")
    assert plan["ok"]
    tools = [s["tool"] for s in plan["steps"]]
    # Creation must precede population, and linking comes last.
    assert tools.index("lib_create_symbol") < tools.index("lib_add_pins")
    assert (tools.index("lib_create_footprint")
            < tools.index("lib_add_footprint_pads"))
    assert tools[-1] == "lib_link_footprint"
    for t in tools:
        # app_set_active_document targets the library the lib_ tools
        # then act on; everything else is a lib_ authoring call.
        assert t.startswith("lib_") or t == "app_set_active_document", \
            f"unexpected tool {t}"
    assert plan["summary"]["pin_count"] == 2
    # 3 real pads. The fixture's unplated HOLE is NOT among them: the
    # library API has no NPTH primitive, and a pad with a blank
    # designator is silently dropped by lib_add_footprint_pads, so the
    # hole is reported in warnings instead of faked as a pad.
    assert plan["summary"]["pad_count"] == 3
    pad_step = next(s for s in plan["steps"]
                    if s["tool"] == "lib_add_footprint_pads")
    assert [p["designator"] for p in pad_step["args"]["pads"]] ==         ["1", "2", "3"]


def test_altium_pins_carry_altium_conventions(comp):
    plan = build_altium_plan(comp, "a.SchLib", "b.PcbLib")
    pins = next(s for s in plan["steps"]
                if s["tool"] == "lib_add_pins")["args"]["pins"]
    by_num = {p["designator"]: p for p in pins}
    # lib_add_pins names this electrical_type and wants lowercase.
    assert by_num["1"]["electrical_type"] == "input"
    assert by_num["2"]["electrical_type"] == "output"
    # lib_add_pins takes DEGREES (0/90/180/270), not a 0..3 code.
    # Pin 1 is on the left and extends +X; pin 2 mirrors it.
    assert by_num["1"]["rotation"] == 0
    assert by_num["2"]["rotation"] == 180
    assert all(isinstance(p["x"], int) for p in pins)


def test_altium_pads_use_multilayer_for_through_hole(comp):
    plan = build_altium_plan(comp, "a.SchLib", "b.PcbLib")
    pads = next(s for s in plan["steps"]
                if s["tool"] == "lib_add_footprint_pads")["args"]["pads"]
    by_num = {p["designator"]: p for p in pads}
    assert by_num["1"]["layer"] == "TopLayer"
    # A drilled pad is forced through-hole by lib_add_footprint_pads
    # itself, driven by hole_size; the layer field is only read for SMD.
    # Asserting layer=="MultiLayer" tested a field the tool ignores.
    assert by_num["1"]["hole_size"] == 0
    assert by_num["3"]["hole_size"] > 0
    # Altium wants a hole DIAMETER; EasyEDA stored a radius.
    assert by_num["3"]["hole_size"] == 80


def test_altium_body_art_can_be_suppressed(comp):
    full = build_altium_plan(comp, "a", "b", include_body_art=True)
    bare = build_altium_plan(comp, "a", "b", include_body_art=False)
    assert len(bare["steps"]) < len(full["steps"])
    assert not any(s["tool"].startswith("lib_add_symbol_")
                   for s in bare["steps"])


def test_altium_silkscreen_tracks_are_one_bulk_call(comp):
    plan = build_altium_plan(comp, "a", "b")
    track_steps = [s for s in plan["steps"]
                   if s["tool"] == "lib_add_footprint_tracks"]
    assert len(track_steps) == 1, "silkscreen must not be N round trips"


# --------------------------- warnings --------------------------------


def test_polygon_pad_warns_instead_of_silently_approximating():
    payload = {
        "result": {
            "dataStr": {"head": {"x": 0, "y": 0}, "shape": []},
            "packageDetail": {"title": "P", "dataStr": {
                "head": {"x": 0, "y": 0},
                "shape": ["PAD~POLYGON~0~0~10~10~1~~9~0~0 0 1 1~0~g~0~~Y"],
            }},
        }
    }
    comp = parse_component(payload)
    assert any("polygon pad" in w for w in comp.warnings)
    # The warning must name the pad so it can be found and checked.
    assert any("9" in w for w in comp.warnings)


def test_missing_geometry_is_reported_not_raised():
    comp = parse_component({"result": {}})
    assert comp.symbol is None and comp.footprint is None
    assert any("no symbol" in w for w in comp.warnings)
    assert any("no footprint" in w for w in comp.warnings)


# --------------------------- fetch -----------------------------------


def test_fetch_rejects_non_https_and_unknown_hosts(monkeypatch):
    from eda_agent.libimport.easyeda import fetch as F

    monkeypatch.delenv("EASYEDA_EXTRA_HOSTS", raising=False)
    with pytest.raises(F.EasyEdaFetchError, match="non-HTTPS"):
        F._check_url("http://easyeda.com/api/x")
    with pytest.raises(F.EasyEdaFetchError, match="allowlist"):
        F._check_url("https://evil.invalid/api/x")
    # The configured host and its subdomains are fine.
    F._check_url("https://easyeda.com/api/x")
    F._check_url("https://modules.easyeda.com/3dmodel/abc")


def test_fetch_normalizes_lcsc_ids():
    from eda_agent.libimport.easyeda import fetch as F

    assert F._normalize_lcsc("c7420") == "C7420"
    assert F._normalize_lcsc("7420") == "C7420"
    with pytest.raises(F.EasyEdaFetchError):
        F._normalize_lcsc("not-a-part")


def test_fetch_component_parses_a_stubbed_response(monkeypatch):
    from eda_agent.libimport.easyeda import fetch as F

    raw = _FIXTURE.read_bytes()
    monkeypatch.setattr(F, "_get", lambda url: raw)
    payload = F.fetch_component_json("C7420")
    comp = parse_component(payload)
    assert comp.mpn == "TESTPART-8"


def test_fetch_reports_upstream_failure(monkeypatch):
    from eda_agent.libimport.easyeda import fetch as F

    monkeypatch.setattr(
        F, "_get",
        lambda url: b'{"success": false, "message": "not found"}')
    with pytest.raises(F.EasyEdaFetchError, match="not found"):
        F.fetch_component_json("C1")


# --------------------------- arcs ------------------------------------
# Arcs were silently DROPPED by both emitters in the first cut: parsed
# fine, then no branch matched them, so pin-1 markers and package
# outlines vanished without a warning. These pin the conversion.


def test_svg_arc_endpoint_to_centre_matches_known_geometry():
    from eda_agent.libimport.easyeda.geometry import parse_svg_arc

    # Quarter circle radius 10 about the origin, (10,0) -> (0,10), CCW.
    arc = parse_svg_arc("M10,0 A10,10 0 0 1 0,10")
    assert arc is not None
    assert arc.cx == pytest.approx(0.0, abs=1e-9)
    assert arc.cy == pytest.approx(0.0, abs=1e-9)
    assert arc.rx == pytest.approx(10.0)
    assert arc.start_angle == pytest.approx(0.0)
    assert arc.end_angle == pytest.approx(90.0)
    # The midpoint must lie ON the arc, at 45 degrees.
    mx, my = arc.midpoint
    assert mx == pytest.approx(10 / 2 ** 0.5, abs=1e-6)
    assert my == pytest.approx(10 / 2 ** 0.5, abs=1e-6)
    assert arc.is_circular


def test_svg_arc_endpoints_round_trip():
    from eda_agent.libimport.easyeda.geometry import parse_svg_arc

    for path in ("M10,0 A10,10 0 0 1 0,10",
                 "M-5,0 A5,5 0 0 1 5,0",
                 "M0,0 A20,20 0 1 0 10,10"):
        arc = parse_svg_arc(path)
        assert arc is not None, path
        sx, sy = arc.point_at(0.0)
        ex, ey = arc.point_at(1.0)
        assert (sx, sy) == (pytest.approx(arc.x1, abs=1e-6),
                            pytest.approx(arc.y1, abs=1e-6))
        assert (ex, ey) == (pytest.approx(arc.x2, abs=1e-6),
                            pytest.approx(arc.y2, abs=1e-6))


def test_svg_arc_sweep_flag_controls_direction():
    from eda_agent.libimport.easyeda.geometry import parse_svg_arc

    ccw = parse_svg_arc("M10,0 A10,10 0 0 1 0,10")
    cw = parse_svg_arc("M10,0 A10,10 0 0 0 0,10")
    assert ccw.sweep_deg > 0
    assert cw.sweep_deg < 0


def test_degenerate_arcs_are_rejected_not_emitted():
    from eda_agent.libimport.easyeda.geometry import parse_svg_arc

    assert parse_svg_arc("M0,0 A0,0 0 0 1 5,5") is None   # zero radius
    assert parse_svg_arc("M5,5 A10,10 0 0 1 5,5") is None  # same endpoints
    assert parse_svg_arc("") is None
    assert parse_svg_arc("M0,0 L10,10") is None            # not an arc


def test_elliptical_arc_is_flagged_not_silently_circularized():
    from eda_agent.libimport.easyeda.geometry import parse_svg_arc

    arc = parse_svg_arc("M10,0 A10,5 0 0 1 0,5")
    assert arc is not None and not arc.is_circular


def _arc_component(path: str, layer: int = 3):
    return parse_component({"result": {
        "dataStr": {"head": {"x": 0, "y": 0},
                    "shape": [f"A~{path}~~1~~~gge1~0"]},
        "packageDetail": {"title": "FP", "dataStr": {
            "head": {"x": 0, "y": 0},
            "shape": [f"ARC~1~{layer}~~{path}~gge2~0"]}},
    }})


def test_kicad_emits_arcs_for_symbol_and_footprint():
    comp = _arc_component("M10,0 A10,10 0 0 1 0,10")
    mod = footprint_to_kicad_mod(comp)
    assert "(fp_arc" in mod and "(mid " in mod
    assert mod.count("(") == mod.count(")")
    sym = symbol_to_kicad_sym(comp)
    assert "(arc (start" in sym and "(mid " in sym
    assert sym.count("(") == sym.count(")")


def test_altium_plan_emits_arcs_in_centre_form():
    comp = _arc_component("M10,0 A10,10 0 0 1 0,10")
    plan = build_altium_plan(comp, "a.SchLib", "b.PcbLib")
    arcs = [s for s in plan["steps"]
            if s["tool"] == "lib_add_footprint_arc"]
    assert arcs, "footprint arc was dropped"
    a = arcs[0]["args"]
    # Altium wants centre + angles, not the SVG endpoint form.
    assert {"x_center", "y_center", "radius",
            "start_angle", "end_angle"} <= set(a)
    assert a["radius"] == 100        # 10 units * 10 mil
    # Normalizing to Y-up MIRRORS the curve, so a canvas arc that swept
    # +90 sweeps -90 here. That is the arc tracing the same physical
    # points, not a sign bug: verified against the transformed midpoint
    # in test_arc_survives_the_y_flip_unchanged_in_space below.
    assert a["start_angle"] == pytest.approx(0.0)
    assert a["end_angle"] == pytest.approx(-90.0)


def test_arc_survives_the_y_flip_unchanged_in_space():
    """The normalized arc must trace the same physical curve.

    Angles and sweep flags change under a Y flip; the geometry must not.
    Comparing the MIDPOINT catches a wrong sweep, a wrong centre, or a
    missing radius scale, none of which the endpoints alone would show.
    """
    from eda_agent.libimport.easyeda.geometry import svg_arc_to_center

    canvas = svg_arc_to_center(10, 0, 10, 10, 0, 0, 1, 0, 10)
    comp = _arc_component("M10,0 A10,10 0 0 1 0,10")
    a = next(s for s in comp.footprint.shapes if s.kind == "arc")
    neutral = svg_arc_to_center(a.x1, a.y1, a.rx, a.ry, a.rotation,
                                a.large_arc, a.sweep, a.x2, a.y2)
    cmx, cmy = canvas.midpoint
    nmx, nmy = neutral.midpoint
    # 10 mil per unit, Y flipped.
    assert nmx == pytest.approx(cmx * 10, abs=1e-6)
    assert nmy == pytest.approx(-cmy * 10, abs=1e-6)
    assert neutral.rx == pytest.approx(100.0)


def test_altium_warns_when_an_elliptical_arc_is_approximated():
    comp = _arc_component("M10,0 A10,5 0 0 1 0,5")
    plan = build_altium_plan(comp, "a", "b")
    assert any("elliptical" in w for w in plan["warnings"])


def test_no_negative_zero_in_emitted_coordinates(comp):
    mod = footprint_to_kicad_mod(comp)
    assert "-0.0" not in mod, "negative zero leaked into the output"


def test_pin_rotation_matches_kicad_altium_convention(comp):
    """EasyEDA's pin rotation is 180 degrees off the CAD convention.

    Ground truth, from a live EasyEDA API payload: a symbol whose body
    rect spans x=370..430 carries its LEFT pins at x=360 with the pin
    line drawn INWARD (``M360,310h10``) and ``rotation=180``, and its
    RIGHT pins at x=440 drawn inward with ``rotation=0``.

    KiCad (``angle_map = {0: 0, ...}``) and Altium (``dir_map =
    {0: (1, 0), ...}``) both call "extends +X" angle/orientation 0, so
    a left-side pin must come out as 0, not 180. Passing EasyEDA's
    value through unchanged reverses every pin: the connection point
    lands inside the body and the pin line overlaps the outline.
    """
    pins = {p.number: p for p in comp.symbol.shapes if p.kind == "pin"}

    left = pins["1"]
    right = pins["2"]
    assert left.x < 0 < right.x, "fixture should straddle the origin"
    # Each pin must EXTEND toward the body, i.e. toward x = 0.
    assert left.rotation == pytest.approx(0.0)
    assert right.rotation == pytest.approx(180.0)


def test_pin_rotation_survives_the_y_flip():
    """A vertical pin's direction must flip with the Y axis.

    rotation=90 and rotation=270 are the cases where a missing mirror
    is invisible in X but points the pin the wrong way in Y.
    """
    import math

    from eda_agent.libimport.easyeda.document import _to_mils_yup
    from eda_agent.libimport.easyeda.shapes import parse_symbol_shapes

    for canvas_rot in (0, 90, 180, 270):
        raw = (f"P~show~1~1~400~300~{canvas_rot}~gge1~0^^400~300"
               f"^^M400,300h-10~#880000^^1~390~300~0~A~end~~~#00F"
               f"^^1~402~297~0~1~start~~~#00F^^0~395~300^^0~")
        shapes = parse_symbol_shapes(raw)
        _to_mils_yup(shapes, 400, 300)
        pin = shapes[0]
        # Canvas direction is (-cos t, -sin t) on a Y-DOWN axis; the
        # neutral frame is Y-UP, so the Y component negates.
        t = math.radians(canvas_rot)
        want = (-math.cos(t), math.sin(t))
        got = (math.cos(math.radians(pin.rotation)),
               math.sin(math.radians(pin.rotation)))
        assert got[0] == pytest.approx(want[0], abs=1e-9), canvas_rot
        assert got[1] == pytest.approx(want[1], abs=1e-9), canvas_rot


def test_top_and_bottom_assembly_layers_stay_distinct():
    """EasyEDA 13/14 are top/bottom assembly and must not collide.

    Mapping both to one Altium mechanical layer puts bottom-side
    assembly art on the top layer, where it reads as real top marking.
    """
    from eda_agent.libimport.easyeda.altium import _ALTIUM_LAYER

    assert _ALTIUM_LAYER[13] != _ALTIUM_LAYER[14]
    # Every EasyEDA layer id must map somewhere unambiguous.
    for lid in (1, 2, 3, 4, 5, 6, 7, 8, 10, 11):
        assert lid in _ALTIUM_LAYER


# --------------------------------------------------------------------
# 3D model: EasyEDA serves OBJ geometry with the MTL library INLINED,
# which a stock OBJ reader either rejects or silently strips of colour.
# --------------------------------------------------------------------

_OBJ_HYBRID = """\
v -1.0 -1.0 0.0
newmtl 1
Ka 0.2 0.2 0.2
Kd 0.25 0.25 0.25
Ks 0.1 0.1 0.1
d 0.0
endmtl
v 1.0 -1.0 0.0
v 1.0 1.0 0.0
v -1.0 1.0 0.0
newmtl 2
Kd 0.85 0.85 0.85
d 0.5
endmtl
usemtl 1
f 1// 2// 3//
usemtl 2
f 1// 3// 4//
"""


def test_inlined_material_blocks_are_parsed():
    from eda_agent.libimport.easyeda.model3d import parse_easyeda_obj

    m = parse_easyeda_obj(_OBJ_HYBRID)
    assert len(m.vertices) == 4
    # Vertices appear both BEFORE and AFTER the material blocks; a parser
    # that stops at newmtl would only see the first one.
    assert m.vertices[0] == (-1.0, -1.0, 0.0)
    assert m.vertices[3] == (-1.0, 1.0, 0.0)
    assert set(m.materials) == {"1", "2"}
    assert m.materials["2"].diffuse == pytest.approx((0.85, 0.85, 0.85))
    # EasyEDA's "d" is transparency (0 = opaque), NOT MTL's dissolve.
    assert m.materials["1"].transparency == pytest.approx(0.0)
    assert m.materials["2"].transparency == pytest.approx(0.5)
    assert [n for n, _ in m.groups] == ["1", "2"]
    assert m.triangle_count == 2
    assert not m.warnings


def test_face_indices_are_one_based_and_tolerate_empty_slots():
    from eda_agent.libimport.easyeda.model3d import parse_easyeda_obj

    m = parse_easyeda_obj(_OBJ_HYBRID)
    # "f 1// 2// 3//" is OBJ's v//vn form with the normal omitted.
    assert m.groups[0][1] == [(0, 1, 2)]


def test_polygon_faces_are_fan_triangulated():
    from eda_agent.libimport.easyeda.model3d import parse_easyeda_obj

    quad = ("v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\n"
            "usemtl a\nf 1// 2// 3// 4//\n")
    m = parse_easyeda_obj(quad)
    assert m.groups[0][1] == [(0, 1, 2), (0, 2, 3)]


def test_wrl_uses_kicad_units_and_shares_one_vertex_array():
    from eda_agent.libimport.easyeda.model3d import (
        obj_to_wrl,
        parse_easyeda_obj,
    )

    m = parse_easyeda_obj(_OBJ_HYBRID)
    wrl = obj_to_wrl(m, name="T")
    assert wrl.startswith("#VRML V2.0 utf8")
    # KiCad VRML is in 0.1 inch units, so 1.0 mm becomes 1/2.54.
    assert "0.393701" in wrl
    # One DEF, then USE for every later material group: repeating the
    # point array per group would multiply a 1000-vertex model by N.
    assert wrl.count("DEF EEVERTS") == 1
    assert wrl.count("USE EEVERTS") == len(m.groups) - 1
    assert wrl.count("Shape {") == len(m.groups)


def test_undefined_material_is_reported_not_silently_defaulted():
    from eda_agent.libimport.easyeda.model3d import parse_easyeda_obj

    m = parse_easyeda_obj("v 0 0 0\nv 1 0 0\nv 0 1 0\nusemtl ghost\n"
                          "f 1// 2// 3//\n")
    assert any("ghost" in w for w in m.warnings)


def test_footprint_references_a_model_file_that_is_actually_produced(comp):
    """The emitter must not point at a .step we never write.

    EasyEDA serves an OBJ-family payload; model3d converts it to VRML.
    A ``.step`` reference is a guaranteed missing-file in KiCad.
    """
    from eda_agent.libimport.easyeda.kicad import footprint_to_kicad_mod

    text = footprint_to_kicad_mod(comp, model_path="${KIPRJMOD}/x.wrl")
    assert '(model "${KIPRJMOD}/x.wrl"' in text
    assert ".step" not in text


def test_search_failure_names_the_working_alternative(monkeypatch):
    """A dead upstream must not surface as a bare HTTP error.

    EasyEDA's search endpoint is no longer public. The caller can still
    import by LCSC id, so the failure has to say that.
    """
    from eda_agent.libimport.easyeda import fetch as fetch_mod

    def boom(url):
        raise fetch_mod.EasyEdaFetchError("HTTP 404 from " + url)

    monkeypatch.setattr(fetch_mod, "_get_json", boom)
    with pytest.raises(fetch_mod.EasyEdaFetchError) as ei:
        fetch_mod.search_components("anything")
    msg = str(ei.value)
    assert "lib_easyeda_import" in msg and "lcsc_id" in msg


# --------------------------------------------------------------------
# The Altium plan is a list of THIS SERVER'S OWN tool calls, so it can
# be checked against the real registered signatures instead of against
# the emitter's assumptions. Without this, the plan stayed internally
# consistent while every argument name was invented, and the failure
# would only appear when a user actually ran it against Altium.
# --------------------------------------------------------------------

def _registered_tools():
    import inspect

    from eda_agent.tools.application import register_application_tools
    from eda_agent.tools.library import register_library_tools

    class Cap:
        def __init__(self):
            self.fns = {}

        def tool(self, *a, **k):
            def deco(f):
                self.fns[f.__name__] = f
                return f
            return deco

    cap = Cap()
    register_library_tools(cap)
    register_application_tools(cap)
    return {n: inspect.signature(f) for n, f in cap.fns.items()}


def test_every_altium_step_matches_a_real_tool_signature(comp):
    from eda_agent.libimport.easyeda.altium import build_altium_plan

    sigs = _registered_tools()
    plan = build_altium_plan(comp, "S.SchLib", "P.PcbLib")
    assert plan["steps"], "plan should not be empty"

    problems = []
    for step in plan["steps"]:
        sig = sigs.get(step["tool"])
        if sig is None:
            problems.append(f"{step['tool']}: no such tool")
            continue
        params = set(sig.parameters)
        unknown = set(step["args"]) - params
        if unknown:
            problems.append(f"{step['tool']}: unknown args {sorted(unknown)}")
        import inspect as _i
        required = {n for n, p in sig.parameters.items()
                    if p.default is _i.Parameter.empty}
        missing = required - set(step["args"])
        if missing:
            problems.append(f"{step['tool']}: missing {sorted(missing)}")
    assert not problems, "plan does not match the tool API: " + "; ".join(
        problems)


def test_plan_activates_the_library_before_editing_it(comp):
    """The library tools act on the ACTIVE document, not a path argument.

    Without an activation step the plan would silently edit whichever
    library happened to be focused.
    """
    from eda_agent.libimport.easyeda.altium import build_altium_plan

    steps = build_altium_plan(comp, "S.SchLib", "P.PcbLib")["steps"]
    assert steps[0]["tool"] == "app_set_active_document"
    assert steps[0]["args"]["file_path"] == "S.SchLib"

    # Every edit must be preceded by an activation of the RIGHT library.
    sch_tools = {"lib_create_symbol", "lib_add_pins",
                 "lib_add_symbol_rectangle", "lib_add_symbol_lines",
                 "lib_add_symbol_polygon", "lib_add_symbol_arc",
                 "lib_link_footprint"}
    pcb_tools = {"lib_create_footprint", "lib_add_footprint_pads",
                 "lib_add_footprint_tracks", "lib_add_footprint_arc",
                 "lib_add_footprint_text"}
    active = None
    for step in steps:
        if step["tool"] == "app_set_active_document":
            active = step["args"]["file_path"]
        elif step["tool"] in sch_tools:
            assert active == "S.SchLib", f"{step['tool']} under {active}"
        elif step["tool"] in pcb_tools:
            assert active == "P.PcbLib", f"{step['tool']} under {active}"


def test_polygon_vertices_are_a_flat_string(comp):
    """lib_add_symbol_polygon takes "x,y,x,y", not a list of pairs."""
    from eda_agent.libimport.easyeda.altium import _symbol_art_steps

    class _P:
        kind = "polygon"
        points = [(0, 0), (100, 0), (100, 100)]

    class _C:
        symbol = type("S", (), {"shapes": [_P()]})()

    steps = _symbol_art_steps(_C(), "S.SchLib", "SYM")
    poly = [s for s in steps if s["tool"] == "lib_add_symbol_polygon"]
    assert poly, "polygon step missing"
    v = poly[0]["args"]["vertices"]
    assert isinstance(v, str)
    assert v.startswith("0,0,100,0,100,100")


# --------------------------------------------------------------------
# Robustness: a malformed or geometry-less payload must degrade, not
# crash, and must not report success when it built nothing.
# --------------------------------------------------------------------

@pytest.mark.parametrize("payload,label", [
    ({}, "empty"),
    ({"result": {"dataStr": {"head": {}, "shape": []}}}, "no geometry"),
    ({"result": {"dataStr": {"head": {"x": 0, "y": 0},
                             "shape": ["GARBAGE~~~", "R~~~~~"]}}}, "garbage"),
])
def test_bad_payloads_degrade_instead_of_raising(payload, label):
    from eda_agent.libimport.easyeda.altium import build_altium_plan

    comp = parse_component(payload)
    plan = build_altium_plan(comp, "S.SchLib", "P.PcbLib")
    assert isinstance(plan["steps"], list), label
    # Silence is the danger: a caller must be told the payload was thin.
    assert comp.warnings, f"{label} produced no warning"


def test_import_tool_refuses_to_report_success_with_an_empty_plan(tmp_path):
    """ok=True plus zero steps reads as a successful import.

    The caller then executes nothing and believes the part landed in the
    library. The kicad branch already refused this; the altium branch
    used to return ok=True with steps=[].
    """
    import asyncio

    from eda_agent.tools.library import register_library_tools

    captured: dict = {}

    class _Capture:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    register_library_tools(_Capture())
    # tmp_path, NOT the repo's fixtures dir: a test that writes into the
    # source tree leaves a stray file behind whenever it dies before its
    # cleanup runs, and the next git status is confusing for reasons
    # unrelated to whatever the reader is working on.
    payload = tmp_path / "empty_payload.json"
    payload.write_text(json.dumps({"result": {"dataStr": {"head": {},
                                                          "shape": []}}}),
                       encoding="utf-8")
    out = asyncio.run(captured["lib_easyeda_import"](
        payload_path=str(payload), target="altium",
        schlib_path="S.SchLib", pcblib_path="P.PcbLib"))

    assert out["ok"] is False
    assert "geometry" in out["reason"]


# --------------------------------------------------------------------
# SOLIDREGION is FILLED copper (thermal pads, shields, pours). Emitting
# it as an outline loses the copper area silently, which is an
# electrical difference, not a cosmetic one.
# --------------------------------------------------------------------

def _region_component():
    return parse_component({"result": {"packageDetail": {
        "title": "SRTEST", "dataStr": {"head": {"x": 0, "y": 0}, "shape": [
            "SOLIDREGION~1~~M 10 10 L 30 10 L 30 20 Z~solid~g~~~0",
            "PAD~RECT~10~30~4~4~1~~1~0~~0~g9~0"]}}}})


def test_solid_region_becomes_a_filled_polygon_not_hairlines():
    """It used to fall through the fp_line branch: two 0.12 mm traces,
    no fill, and the closing edge missing because the path's Z was
    never applied."""
    text = footprint_to_kicad_mod(_region_component())
    assert "fp_poly" in text, "filled region emitted as something else"
    poly = next(l for l in text.splitlines() if "fp_poly" in l)
    # Filled, not stroked.
    assert "(width 0)" in poly
    # The pad is still a separate primitive, not swallowed by the region.
    assert text.count("(pad ") == 1


def test_region_polygon_is_closed():
    """An unclosed pour is not a pour. The path carried Z; honour it."""
    import re

    text = footprint_to_kicad_mod(_region_component())
    poly = next(l for l in text.splitlines() if "fp_poly" in l)
    pts = re.findall(r"\(xy ([-\d.]+) ([-\d.]+)\)", poly)
    assert len(pts) >= 4, "expected the closing point to be appended"
    assert pts[0] == pts[-1], f"polygon not closed: {pts[0]} != {pts[-1]}"


def test_altium_warns_that_the_fill_cannot_be_reproduced():
    """Altium's library API has pads/tracks/arcs/text and no region.

    Drawing only the outline is the best it can do, so the caller has to
    be told the copper is missing rather than discovering it at fab.
    """
    plan = build_altium_plan(_region_component(), "S.SchLib", "P.PcbLib")
    joined = " ".join(plan["warnings"]).lower()
    assert "region" in joined and "outline" in joined


def _shapes_component(sym_raw=None, fp_raw=None):
    """Component from raw shape strings, normalized like the real path.

    _to_mils_yup is NOT optional here: parse_component always applies it,
    so a helper that skips it silently tests raw 10-mil canvas units and
    every geometric assertion written against it is off by 10x.
    """
    from eda_agent.libimport.easyeda.document import (
        EasyEdaComponent, EasyEdaFootprint, EasyEdaSymbol, _to_mils_yup,
    )
    comp = EasyEdaComponent()
    if sym_raw:
        shapes = parse_symbol_shapes(sym_raw)
        _to_mils_yup(shapes, 0.0, 0.0)
        comp.symbol = EasyEdaSymbol(name="S", shapes=shapes)
    if fp_raw:
        shapes = parse_footprint_shapes(fp_raw)
        _to_mils_yup(shapes, 0.0, 0.0)
        comp.footprint = EasyEdaFootprint(name="F", shapes=shapes)
    return comp


def test_unplated_hole_is_reported_not_faked():
    """An NPTH cannot be expressed, so say so rather than emit a no-op.

    The first attempt emitted the hole as a pad with an empty
    designator. That looked right in the plan and was DROPPED during
    serialization: lib_add_footprint_pads skips any pad without a
    designator (skipped_invalid). The step existed, the hole did not.

    Supplying a designator is not the fix either. In Altium a designator
    makes the pad connectable, so a mounting hole would become a real
    net-joinable pad, which is a worse failure than a missing hole.
    """
    comp = _shapes_component(fp_raw="HOLE~10~10~4~g~0")
    plan = build_altium_plan(comp, "S.SchLib", "P.PcbLib")

    # No pad step may claim to carry the hole.
    for step in plan["steps"]:
        if step["tool"] == "lib_add_footprint_pads":
            for pad in step["args"]["pads"]:
                assert pad["designator"], (
                    "emitted a pad with an empty designator; the tool "
                    "silently drops those")
    assert any("hole" in w.lower() and "NOT created" in w
               for w in plan["warnings"])


def test_no_plan_step_carries_a_pad_the_tool_would_drop():
    """Guard the general rule behind that bug.

    lib_add_footprint_pads discards any pad whose designator is blank.
    A plan that emits one reports success while losing geometry, which
    is invisible from the plan alone.
    """
    comp = _fixture_or_none()
    if comp is None:
        pytest.skip("fixture unavailable")
    plan = build_altium_plan(comp, "S.SchLib", "P.PcbLib")
    for step in plan["steps"]:
        if step["tool"] == "lib_add_footprint_pads":
            blank = [p for p in step["args"]["pads"] if not p["designator"]]
            assert not blank, f"{len(blank)} pad(s) would be dropped"


def _fixture_or_none():
    try:
        return parse_component(json.loads(_FIXTURE.read_text(encoding="utf-8")))
    except Exception:
        return None


def test_symbol_body_text_is_placed():
    """Altium's primitive for free text on a symbol is an ISch_Label.

    This used to be dropped with a warning claiming the library API had
    no symbol text primitive, which was true of our API and not of
    Altium's. Part markings carry meaning the geometry does not, so
    losing them changes what the symbol says.
    """
    comp = _shapes_component(sym_raw="T~0~10~10~0~~~60~~~~~Hello~1")
    plan = build_altium_plan(comp, "S.SchLib", "P.PcbLib")
    step = next((s for s in plan["steps"]
                 if s["tool"] == "lib_add_symbol_text"), None)
    assert step is not None, [s["tool"] for s in plan["steps"]]
    assert "Hello" in {t["text"] for t in step["args"]["texts"]}


def test_symbol_text_height_is_reported_as_unmapped():
    """The one thing that genuinely does not carry: the source states
    height in mils and Altium's font size is in its own units, with no
    documented relation. That is worth saying rather than guessing."""
    comp = _shapes_component(sym_raw="T~0~10~10~0~~~60~~~~~Hello~1")
    plan = build_altium_plan(comp, "S.SchLib", "P.PcbLib")
    assert [w for w in plan["warnings"] if "font size" in w], plan["warnings"]


def test_kicad_keeps_both_of_those():
    """KiCad can express them, so it must not warn its way out.

    The gap is Altium's library API, not the source geometry.
    """
    hole = _shapes_component(fp_raw="HOLE~10~10~4~g~0")
    assert "np_thru_hole" in footprint_to_kicad_mod(hole)
    text = _shapes_component(sym_raw="T~0~10~10~0~~~60~~~~~Hello~1")
    assert "Hello" in symbol_to_kicad_sym(text)


def test_symbol_arc_reaches_altium():
    """Curved symbol art used to vanish into the Altium plan.

    The FOOTPRINT path handled arcs; _symbol_art_steps never had a
    branch, so a symbol arc produced no step and no warning even though
    lib_add_symbol_arc exists. Silent, and only visible by diffing the
    drawn symbol against the source.
    """
    comp = _shapes_component(
        sym_raw="A~M10,0 A10,10 0 0 1 0,10~~~#000~1~0~none~g~0")
    plan = build_altium_plan(comp, "S.SchLib", "P.PcbLib")
    arcs = [s for s in plan["steps"] if s["tool"] == "lib_add_symbol_arc"]
    assert arcs, "symbol arc dropped from the Altium plan"
    args = arcs[0]["args"]
    # Centre form, matching what the tool takes (not the SVG endpoints).
    assert {"x_center", "y_center", "radius",
            "start_angle", "end_angle"} <= set(args)
    assert args["radius"] == 100          # 10 units * 10 mil
    assert args["start_angle"] == pytest.approx(0.0)


def test_every_parsed_shape_kind_reaches_altium_or_warns():
    """No shape may be dropped in silence.

    This is the class that produced the arc bug, the solid-region bug and
    the hole bug: geometry parses fine, then disappears between the model
    and the emitter with nothing to notice. Either a step is produced or
    the caller is told why not.
    """
    cases = {
        "rect": ("R~10~10~2~2~40~30~#000~1~0~none~g~0~", None),
        "ellipse": ("E~20~20~10~10~#000~1~0~none~g~0", None),
        "polyline": ("PL~0 0 50 0 50 50~#000~1~0~none~g~0", None),
        "polygon": ("PG~0 0 50 0 50 50~#000~1~0~none~g~0", None),
        "arc": ("A~M10,0 A10,10 0 0 1 0,10~~~#000~1~0~none~g~0", None),
        "text": ("T~0~10~10~0~~~60~~~~~Hello~1", "text"),
        "fp_pad": (None, "PAD~RECT~10~10~4~2~1~~1~0~~0~g~0"),
        "fp_track": (None, "TRACK~1~1~~10 10 30 10~g~0"),
        "fp_arc": (None, "ARC~1~3~~M10,0 A10,10 0 0 1 0,10~g~0"),
        "fp_circle": (None, "CIRCLE~20~20~10~1~3~g~0"),
        "fp_hole": (None, "HOLE~10~10~4~g~0"),
        "fp_region": (None,
                      "SOLIDREGION~1~~M 10 10 L 30 10 L 30 20 Z~solid~g~~~0"),
    }
    unreported = []
    for label, (sym_raw, fp_raw) in cases.items():
        expect_warning = isinstance(fp_raw, str) and sym_raw is None
        comp = _shapes_component(sym_raw=sym_raw,
                                 fp_raw=fp_raw if expect_warning else None)
        if label == "text":
            comp = _shapes_component(sym_raw=sym_raw)
        plan = build_altium_plan(comp, "S.SchLib", "P.PcbLib")
        art = [s for s in plan["steps"]
               if s["tool"] not in ("app_set_active_document",
                                    "lib_create_symbol",
                                    "lib_create_footprint",
                                    "lib_link_footprint")]
        # Either something was emitted for it, or a warning names it.
        if not art and not plan["warnings"]:
            unreported.append(label)
    assert not unreported, (
        f"these shapes produced neither a step nor a warning: "
        f"{unreported}")


def test_via_in_a_footprint_becomes_real_copper():
    """Thermal vias under an exposed pad are the heat path.

    EasyEDA models them as VIA, which the parser turns into a plated
    MultiLayer pad so both emitters place actual copper rather than a
    drawing.
    """
    comp = _shapes_component(fp_raw="VIA~20~20~5~~2~g~0")
    via = comp.footprint.shapes[0]
    assert via.kind == "pad" and via.is_through_hole and via.plated

    # KiCad has no designator requirement, so the copper is created.
    assert "(pad" in footprint_to_kicad_mod(comp)

    # Altium cannot: EasyEDA gives vias no pad number, and
    # lib_add_footprint_pads discards blank designators. Emitting one
    # anyway produced a step that looked right and did nothing, so the
    # gap is reported instead.
    plan = build_altium_plan(comp, "S.SchLib", "P.PcbLib")
    assert not any(s["tool"] == "lib_add_footprint_pads"
                   for s in plan["steps"])
    assert any("no pad number" in w for w in plan["warnings"])


def test_parser_command_set_is_pinned():
    """A new shape command must not slip in without emitter coverage.

    Every silent-drop bug in this converter had the same shape: the
    parser understood a primitive that no emitter did. Pinning the
    accepted commands makes adding one a deliberate act that fails this
    test until the emitters and the coverage test above are extended.
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "eda_agent"
           / "libimport" / "easyeda" / "shapes.py").read_text(encoding="utf-8")
    found = set(re.findall(r'cmd == "([A-Z]+)"', src))
    for group in re.findall(r'cmd in \(([^)]*)\)', src):
        found |= set(re.findall(r'"([A-Z]+)"', group))

    expected = {
        # symbol side
        "P",            # pin
        "R", "E",       # rect, ellipse
        "PL", "PG", "W",  # polyline, polygon, wire
        "A",            # arc
        "T",            # text
        # footprint side
        "PAD", "TRACK", "ARC", "CIRCLE", "RECT", "HOLE", "TEXT",
        "SOLIDREGION", "VIA",
    }
    assert found == expected, (
        f"parser command set changed: added={sorted(found - expected)}, "
        f"removed={sorted(expected - found)}. A new command needs an "
        f"emitter branch (or an explicit warning) before it is accepted, "
        f"or its geometry disappears silently.")


def test_cutout_regions_are_not_painted_as_fills():
    """REGRESSION GUARD, from a bug this converter actually shipped.

    Adding fill support turned every SOLIDREGION into a filled polygon.
    Real parts are full of regions that are NOT pours: an LQFP-48 carries
    97, all ``fill="cutout"`` on layers 99/100 which are not in the layer
    map. Painting those put a solid block over the body and a fill over
    every pad -- visibly worse than the hairlines the fix replaced, and
    invisible to a synthetic test that only used a genuine pour.

    Only a solid fill on a KNOWN layer may be filled.
    """
    cutout = _shapes_component(
        fp_raw="SOLIDREGION~99~~M 10 10 L 30 10 L 30 20 Z~cutout~g~~~0")
    assert "fp_poly" not in footprint_to_kicad_mod(cutout)

    unknown_layer = _shapes_component(
        fp_raw="SOLIDREGION~100~~M 10 10 L 30 10 L 30 20 Z~solid~g~~~0")
    assert "fp_poly" not in footprint_to_kicad_mod(unknown_layer)

    # The genuine pour must still fill, or the original bug is back.
    pour = _shapes_component(
        fp_raw="SOLIDREGION~1~~M 10 10 L 30 10 L 30 20 Z~solid~g~~~0")
    assert "fp_poly" in footprint_to_kicad_mod(pour)


def test_altium_region_warning_does_not_cry_wolf_on_cutouts():
    """A warning that fires on every import gets ignored.

    97 cutouts per part would have made the region warning noise.
    """
    cutout = _shapes_component(
        fp_raw="SOLIDREGION~99~~M 10 10 L 30 10 L 30 20 Z~cutout~g~~~0")
    plan = build_altium_plan(cutout, "S.SchLib", "P.PcbLib")
    assert not any("region" in w for w in plan["warnings"])

    pour = _shapes_component(
        fp_raw="SOLIDREGION~1~~M 10 10 L 30 10 L 30 20 Z~solid~g~~~0")
    plan = build_altium_plan(pour, "S.SchLib", "P.PcbLib")
    assert any("region" in w for w in plan["warnings"])


def test_dropped_faces_are_reported_not_swallowed():
    """A face pointing at a missing vertex loses surface silently.

    The model still writes a syntactically valid VRML, so the failure is
    invisible until someone looks at the 3D view and finds a hole.
    """
    from eda_agent.libimport.easyeda.model3d import parse_easyeda_obj

    m = parse_easyeda_obj(
        "v 0 0 0\nv 1 0 0\nv 0 1 0\nusemtl a\n"
        "f 1// 2// 3//\nf 1// 2// 99//\n")
    assert m.triangle_count == 1
    assert any("dropped" in w for w in m.warnings)


def test_a_model_with_no_usable_faces_says_so():
    """Vertices but no faces produce a valid file containing nothing.

    Without a warning that reads as a successful 3D import.
    """
    from eda_agent.libimport.easyeda.model3d import obj_to_wrl, parse_easyeda_obj

    m = parse_easyeda_obj("v 0 0 0\nv 1 0 0\nv 0 1 0\n")
    assert m.triangle_count == 0
    assert any("render as nothing" in w for w in m.warnings)
    # It still emits, because refusing would lose the vertices too.
    assert obj_to_wrl(m, name="T").startswith("#VRML")


def test_obj_parser_survives_malformed_input():
    """None of these may raise: a bad model must not kill the import."""
    from eda_agent.libimport.easyeda.model3d import parse_easyeda_obj

    for text in (
        "",
        "v 0 0\nv 1 0 0\nv 0 1 0\nusemtl a\nf 1// 2// 3//\n",   # short vertex
        "v a b c\nv 1 0 0\nv 0 1 0\nusemtl a\nf 1// 2// 3//\n",  # non-numeric
        "newmtl m\nKd 1 0 0\nv 0 0 0\n",                        # no endmtl
        "# only a comment\r\n",
        "usemtl a\nf 1// 2// 3//\nv 0 0 0\nv 1 0 0\nv 0 1 0\n",  # faces first
    ):
        m = parse_easyeda_obj(text)
        assert isinstance(m.warnings, list)


def test_negative_face_indices_resolve_relative():
    """OBJ allows negative indices counting back from the current end."""
    from eda_agent.libimport.easyeda.model3d import parse_easyeda_obj

    m = parse_easyeda_obj(
        "v 0 0 0\nv 1 0 0\nv 0 1 0\nusemtl a\nf -3// -2// -1//\n")
    assert m.groups[0][1] == [(0, 1, 2)]
    assert not any("dropped" in w for w in m.warnings)


def test_model_warnings_reach_the_tool_caller(tmp_path, monkeypatch):
    """A warning that stops short of the caller may as well not exist.

    model3d collects them, document merges them, and the tool has to
    surface them. That chain is three hand-offs long and every link is
    easy to drop.
    """
    import asyncio

    from eda_agent.libimport.easyeda import fetch as fetch_mod
    from eda_agent.tools.library import register_library_tools

    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    # Give the fixture a 3D model so the import fetches one.
    payload["result"].setdefault("packageDetail", {}).setdefault(
        "dataStr", {}).setdefault("shape", []).append(
        'SVGNODE~{"attrs":{"uuid":"deadbeef","title":"M"}}')

    monkeypatch.setattr(fetch_mod, "fetch_component_json",
                        lambda code: payload)
    # Faces pointing at a vertex that does not exist.
    monkeypatch.setattr(
        fetch_mod, "fetch_3d_model",
        lambda uuid: b"v 0 0 0\nv 1 0 0\nv 0 1 0\nusemtl a\n"
                     b"f 1// 2// 3//\nf 1// 2// 99//\n")

    captured: dict = {}

    class _Capture:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    register_library_tools(_Capture())
    out = asyncio.run(captured["lib_easyeda_import"](
        lcsc_id="C0000", target="kicad", output_dir=str(tmp_path)))

    assert out["ok"] is True, "a damaged 3D model must not fail the import"
    assert "model_3d" in out["files"]
    assert any("dropped" in w for w in out["warnings"]), (
        f"3D model warning never reached the caller: {out['warnings']}")




def test_blank_pad_number_is_reported_not_silently_dropped():
    """lib_add_footprint_pads discards blank designators without failing.

    A numberless pad therefore vanishes between plan and library with
    nothing raised and nothing logged. Real payloads do contain them.
    """
    comp = _shapes_component(
        fp_raw="PAD~RECT~10~10~4~2~1~~~0~~0~g~0#@$PAD~RECT~30~10~4~2~1~~2~0~~0~g~0")
    plan = build_altium_plan(comp, "S.SchLib", "P.PcbLib")
    step = next(s for s in plan["steps"]
                if s["tool"] == "lib_add_footprint_pads")
    # Only the numbered pad is emitted; the blank one would be dropped.
    assert [p["designator"] for p in step["args"]["pads"]] == ["2"]
    assert any("no pad number" in w for w in plan["warnings"])


def test_blank_pin_number_is_reported_not_silently_dropped():
    """Same filter on the pin side of lib_add_pins."""
    from eda_agent.libimport.easyeda.document import (
        EasyEdaComponent, EasyEdaSymbol, _to_mils_yup,
    )

    shapes = parse_symbol_shapes(
        "P~show~1~~10~10~180~g~0^^10~10^^M10,10h10~#000"
        "^^1~0~10~0~A~end~~~#00F^^1~12~7~0~~start~~~#00F^^0~5~10^^0~")
    _to_mils_yup(shapes, 0.0, 0.0)
    comp = EasyEdaComponent(symbol=EasyEdaSymbol(name="S", shapes=shapes))
    plan = build_altium_plan(comp, "S.SchLib", "P.PcbLib")
    joined = " ".join(plan["warnings"])
    assert "no pin number" in joined or "no pins" in joined


# --------------------------------------------------------------------
# File names come from vendor payload text, so they are untrusted input.
# --------------------------------------------------------------------

@pytest.mark.parametrize("raw,expect", [
    ("SOT-23/5 <BL>", "SOT-23_5 _BL_"),   # slash and angle brackets
    ('X"Y:Z*?', "X_Y_Z__"),               # quote, colon, star, question
    ("../../evil", "_.._evil"),           # path traversal
    ("CON", "_CON"),                      # Windows reserved device name
    ("  trailing. ", "trailing"),         # trailing dot/space are illegal
    ("ok_name", "ok_name"),               # untouched when already safe
])
def test_vendor_names_are_made_safe_for_the_filesystem(raw, expect):
    from eda_agent.tools.library import _safe_filename

    assert _safe_filename(raw) == expect


def test_import_survives_a_filesystem_hostile_part_name(tmp_path,
                                                        monkeypatch):
    """A vendor name with illegal characters used to crash the import.

    ``out / f"{name}.kicad_mod"`` raised a bare OSError (Errno 22) out of
    the MCP tool: not a handled failure, an unhandled one. Package titles
    genuinely contain slashes and angle brackets.
    """
    import asyncio

    from eda_agent.libimport.easyeda import fetch as fetch_mod
    from eda_agent.tools.library import register_library_tools

    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    payload["result"]["packageDetail"]["title"] = "SOT-23/5 <BL>"
    payload["result"]["dataStr"]["head"]["c_para"][
        "Manufacturer Part"] = 'X"Y:Z*?'
    monkeypatch.setattr(fetch_mod, "fetch_component_json",
                        lambda code: payload)

    captured: dict = {}

    class _Capture:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    register_library_tools(_Capture())
    out = asyncio.run(captured["lib_easyeda_import"](
        lcsc_id="C1", target="kicad", output_dir=str(tmp_path)))

    assert out["ok"] is True
    assert out["files"], "nothing written"
    for path in out["files"].values():
        p = Path(path)
        assert p.exists(), f"{p} was not created"
        # Sanitising is also containment: nothing may escape output_dir.
        assert p.resolve().parent == tmp_path.resolve(), (
            f"{p} escaped the output directory")


# --------------------------------------------------------------------
# Batch payload injection. The bridge grammar is "~~" between ops, ";"
# between fields, "=" between key and value, and the Pascal parser has
# no escape syntax. A free-text VALUE carrying those reshapes the
# payload while keeping it syntactically valid, so nothing fails.
# --------------------------------------------------------------------

def _recording_library_tools(monkeypatch):
    from eda_agent.tools import library as lib_mod

    calls: list = []

    class _Bridge:
        def send_command(self, command, params=None, **kwargs):
            calls.append((command, dict(params or {})))
            return {"success": True}

        async def send_command_async(self, command, params=None, **kwargs):
            return self.send_command(command, params, **kwargs)

    monkeypatch.setattr(lib_mod, "get_bridge", lambda: _Bridge())
    captured: dict = {}

    class _Capture:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    lib_mod.register_library_tools(_Capture())
    return captured, calls


def test_pin_name_cannot_inject_payload_fields(monkeypatch):
    """A pin named ``A;x=99`` used to move the pin.

    The injected ``x=99`` landed ahead of the real coordinate, and
    ``B~~designator=99`` split one op into two, inventing a pin. Both
    silent.
    """
    import asyncio

    fns, calls = _recording_library_tools(monkeypatch)
    asyncio.run(fns["lib_add_pins"](pins=[
        {"designator": "1", "name": "A;x=99", "x": 0, "y": 0,
         "length": 100, "rotation": 0, "electrical_type": "passive"},
        {"designator": "2", "name": "B~~designator=99", "x": 0, "y": 100,
         "length": 100, "rotation": 0, "electrical_type": "passive"},
    ]))
    payload = calls[-1][1]["pins"]
    ops = payload.split("~~")
    assert len(ops) == 2, f"payload split into {len(ops)} ops, expected 2"

    # Assert FIELD STRUCTURE, not raw text. The sanitiser deliberately
    # leaves "=" alone (Pascal splits on the FIRST "="), so the injected
    # text survives INSIDE the name value: that is correct and harmless.
    # What matters is that it is no longer a field of its own.
    def fields(op):
        out = {}
        for chunk in op.split(";"):
            if "=" in chunk:
                key, value = chunk.split("=", 1)
                out.setdefault(key, value)   # first wins, as Pascal does
        return out

    first, second = fields(ops[0]), fields(ops[1])
    # The coordinates the caller asked for, not the injected ones.
    assert first["x"] == "0" and first["y"] == "0"
    assert second["x"] == "0" and second["y"] == "100"
    # The injected text is carried harmlessly inside the name.
    assert first["name"] == "A,x=99"
    assert first["designator"] == "1" and second["designator"] == "2"


def test_pad_designator_cannot_inject_payload_fields(monkeypatch):
    import asyncio

    fns, calls = _recording_library_tools(monkeypatch)
    asyncio.run(fns["lib_add_footprint_pads"](pads=[
        {"designator": "1;x_size=999", "x": 10, "y": 20,
         "x_size": 30, "y_size": 40},
    ]))
    payload = calls[-1][1]["pads"]
    assert "~~" not in payload, "injection split the op"
    fields = {}
    for chunk in payload.split(";"):
        if "=" in chunk:
            key, value = chunk.split("=", 1)
            fields.setdefault(key, value)
    # The caller's size wins; the injected one is inside the designator.
    assert fields["x_size"] == "30"
    assert fields["designator"] == "1,x_size=999"


# ------------- EasyEDA -> KiCad export, read back again --------------

def test_easyeda_part_survives_export_to_kicad_and_reimport():
    """The lib_easyeda_import(target=kicad) path, checked end to end.

    The writer had four defects found only by round-tripping real data
    (pad and text rotation unmirrored, roundrect written as an oval, a
    falsy-zero corner ratio). Those were caught against the KiCad
    corpus, which never exercises an EasyEDA-sourced part -- this does.
    """
    import json
    from pathlib import Path

    from eda_agent.libimport.easyeda import parse_component
    from eda_agent.libimport.kicad.reader import (
        read_kicad_footprint,
        read_kicad_symbol,
    )

    fixture = Path(__file__).resolve().parent / "fixtures" / "easyeda_soic8.json"
    comp = parse_component(json.loads(fixture.read_text(encoding="utf-8")))

    def pin_sig(c):
        return sorted((p.number, p.unit, round(p.x), round(p.y),
                       round(p.rotation), p.display)
                      for p in c.symbol.shapes if p.kind == "pin")

    assert pin_sig(read_kicad_symbol(symbol_to_kicad_sym(comp))) == pin_sig(comp)

    def pad_sig(c):
        return sorted((p.number, round(p.cx), round(p.cy), round(p.width),
                       round(p.height), round(p.hole_radius, 1), p.shape,
                       round(p.rotation))
                      for p in c.footprint.shapes
                      if p.kind == "pad" and p.number)

    back = read_kicad_footprint(footprint_to_kicad_mod(comp))
    assert pad_sig(back) == pad_sig(comp)


def test_an_unplated_hole_stays_unplated_through_a_kicad_export():
    """An NPTH must not come back as copper.

    Compared SEMANTICALLY, not structurally: the neutral model has two
    ways to say "unplated hole" -- a ``hole`` shape and a pad with
    ``plated`` false -- while KiCad has one, ``np_thru_hole``. So the
    shape kind legitimately changes across the trip and the properties
    that matter must not. Demanding structural identity here would be
    asserting an artifact of the neutral model rather than anything
    about the conversion.

    What would be a real defect is the hole arriving plated, which puts
    copper in a mounting hole, or arriving as a different size.
    """
    import json
    from pathlib import Path

    from eda_agent.libimport.easyeda import parse_component
    from eda_agent.libimport.kicad.reader import read_kicad_footprint

    fixture = Path(__file__).resolve().parent / "fixtures" / "easyeda_soic8.json"
    comp = parse_component(json.loads(fixture.read_text(encoding="utf-8")))
    holes = [s for s in comp.footprint.shapes if s.kind == "hole"]
    assert holes, "fixture no longer carries an unplated hole"

    text = footprint_to_kicad_mod(comp)
    assert "np_thru_hole" in text, "the hole was not written as an NPTH"

    back = read_kicad_footprint(text)
    npth = [p for p in back.footprint.shapes
            if p.kind == "pad" and not p.plated]
    assert len(npth) == len(holes)
    for hole, pad in zip(holes, npth):
        assert pad.is_through_hole
        assert pad.hole_radius == pytest.approx(hole.diameter / 2, abs=1)
        assert round(pad.cx) == round(hole.cx)
        assert round(pad.cy) == round(hole.cy)

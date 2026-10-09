# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""proj_export_step and pcb_repour_polygons report what happened.

Both answered success whatever happened. proj_export_step returned
success:true without looking for the file, and from a schematic tab the
export went to a view that was not the board. pcb_repour_polygons ran a
process name nothing documents and returned repoured:true, while a pour
past a corrected edge clearance stayed as it was. pcb_get_polygons gave
the outline's area as the copper's, so a repour could not be seen in it.

These handlers need Altium to run, so the shape of what they decide on
is checked here, with comments stripped so an explanation cannot pass
for the code. Release verification step 26 runs them live.
"""

from __future__ import annotations

import re
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "altium"


def _routine(unit: str, name: str) -> str:
    code = (SCRIPTS / unit).read_text(encoding="utf-8", errors="replace")
    start = re.search(rf"(?mi)^(?:Function|Procedure) {name}\b", code)
    assert start, f"missing routine {name}"
    end = re.search(r"(?m)^End;", code[start.start():])
    body = code[start.start():start.start() + end.end()]
    return re.sub(r"'(?:(?:'')|[^'])*'|\{[^}]*\}|//[^\n]*",
                  lambda m: m[0] if m[0].startswith("'") else " ", body)


def test_step_export_claims_a_file_only_when_a_new_one_is_there():
    body = _routine("Project.pas", "Proj_ExportSTEP")
    compare = body.index("RunProcess('PCB:ExportSTEP3D')")
    assert body.index("ResolvePCBBoard(") < compare, "the board is focused first"
    assert body.index("AgeBefore := FileAge(OutputPath)") < compare
    check = body.index("(AgeAfter <> -1) And (AgeAfter <> AgeBefore)")
    assert check > compare
    assert body.count('"generated":true') == 1
    assert body.index('"generated":true') > check
    assert '"generated":false' in body
    assert '{"success":true}' not in body


def test_repour_goes_through_the_api_and_counts_what_it_rebuilt():
    body = _routine("PCB.pas", "PCB_RepourPolygons")
    assert "RunProcess(" not in body
    for step in ("Poly.SetState_CopperPourInvalid", "Poly.Rebuild",
                 "PolygonCopper(Poly, AreaBefore, ExactBefore)", "PolygonCopper(Poly, AreaAfter, ExactAfter)",
                 "If Not Poly.Poured Then", ".PourIndex >"):
        assert step in body, step
    # The option is set for the repour and put back whatever happens.
    finally_at = body.index("Finally")
    assert body.index("PolygonRepour := eAlwaysRepour") < finally_at
    assert "PolygonRepour := RepourMode" in body[finally_at:]
    assert '"repoured":true' not in body
    assert "BoolToJsonStr((Rebuilt > 0) And (Failed = 0))" in body


def test_polygon_rows_carry_the_poured_copper_beside_the_outline():
    body = _routine("PCB.pas", "PCB_GetPolygons")
    assert "PolygonCopper(Polygon, CopperSqMils, CopperExact)" in body
    assert '"copper_area_mm2":' in body and '"copper_pieces":' in body
    assert '"copper_area_exact":' in body, "an area whose holes could not be read says so"
    assert '"area_mm2":' in body, "the outline's area stays, under its own name"

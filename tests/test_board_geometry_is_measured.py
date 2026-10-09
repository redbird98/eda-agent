# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Board geometry answers are measured from the objects themselves.

Three replies were measured from the wrong thing:

- pcb_delete_object took the distance to a track's midpoint, so a point
  exactly on a long track was "not found within 100 mils".
- The board outline's bounding_rect came from a cached size a reshape did
  not refresh, and renders built on it cut off a third of the board.
- audit_find_acute_angles squared Integer coordinate differences, which
  overflowed on any track over about 4.6 mil; Sqrt of the negative sum
  gave NaN, FloatToStr wrote it, and the reply was not JSON.

The helpers are extracted from the production scripts and run under FPC.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "altium"


def _code(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8", errors="replace")


def _routine(code: str, name: str) -> str:
    start = re.search(rf"(?mi)^(?:Function|Procedure) {name}\b", code)
    assert start, f"missing routine {name}"
    end = re.search(r"(?m)^End;", code[start.start():])
    body = code[start.start():start.start() + end.end()]
    return re.sub(r"'(?:(?:'')|[^'])*'|\{[^}]*\}|//[^\n]*",
                  lambda m: m[0] if m[0].startswith("'") else " ", body, flags=re.S)


def _fpc():
    fpc = shutil.which("fpc")
    if not fpc:
        pytest.skip("Free Pascal Compiler (fpc) is not installed or not on PATH")
    return fpc


def _run(tmp_path, name: str, program: str) -> int:
    path = tmp_path / f"{name}.pas"
    path.write_text(program, encoding="utf-8")
    built = subprocess.run([_fpc(), str(path)], cwd=tmp_path, capture_output=True, text=True)
    assert built.returncode == 0, built.stdout + built.stderr
    exe = tmp_path / (f"{name}.exe" if os.name == "nt" else name)
    return subprocess.run([str(exe)], capture_output=True, text=True).returncode


def test_the_delete_distance_is_to_the_object(tmp_path):
    pcb = _code("PCB.pas")
    checks = [
        # A point ON a long track, far from its midpoint: zero.
        "SegDistMils(2000, 1300, 1300, 1300, 8250, 1300)",
        # Beside it, and past its end.
        "SegDistMils(2000, 1340, 1300, 1300, 8250, 1300) - 40",
        "SegDistMils(8280, 1340, 1300, 1300, 8250, 1300) - 50",
        # A degenerate track is a point.
        "SegDistMils(3, 4, 0, 0, 0, 0) - 5",
        # A rectangle: zero inside, to the edge outside (internal units in).
        "RectDistMils(50, 50, R)",
        "RectDistMils(130, 50, R) - 30",
        # An arc: on the curve inside its sweep, to the nearer end outside.
        "ArcDistMils(0, 120, 0, 0, 100, 0, 180) - 20",
        "ArcDistMils(0, -100, 0, 0, 100, 0, 180) - Sqrt(20000)",
    ]
    lines = [f"  if Abs({c}) > 0.001 then Halt({i});" for i, c in enumerate(checks, 1)]
    program = "\n".join([
        "program dist;", "{$mode delphi}", "uses SysUtils, Math;",
        "type TCoordRect = record Left, Bottom, Right, Top: Integer; end;",
        _routine(pcb, "SegDistMils"), _routine(pcb, "RectDistMils"), _routine(pcb, "ArcDistMils"),
        "var R: TCoordRect;",
        "begin", "  R.Left := 0; R.Bottom := 0; R.Right := 1000000; R.Top := 1000000;",
        *lines, "end.",
    ])
    assert _run(tmp_path, "dist", program) == 0


def test_the_delete_handler_uses_those_distances_not_a_midpoint():
    body = _routine(_code("PCB.pas"), "PCB_DeleteObject")
    assert "SegDistMils(" in body and "ArcDistMils(" in body and "RectDistMils(" in body
    assert "Div 2" not in body, "a reference point is back"
    assert '"others_as_close":' in body and '"bbox_mils":[' in body


def test_the_outline_extents_come_from_its_segments(tmp_path):
    gen = _code("PCBGeneric.pas")
    # A rectangle, and a half disc whose top is an arc's 90 degree crossing
    # that neither vertex reaches.
    program = "\n".join([
        "program extents;", "{$mode delphi}", "uses SysUtils;",
        "const ePolySegmentLine = 0; ePolySegmentArc = 1;",
        "type TPolySegment = record Kind, vx, vy, cx, cy: Integer; Angle1, Angle2: Double; end;",
        "IPCB_BoardOutline = class Segs: array of TPolySegment;",
        "function GetSeg(I: Integer): TPolySegment; function PointCount: Integer;",
        "property Segments[I: Integer]: TPolySegment read GetSeg; end;",
        "function IPCB_BoardOutline.GetSeg(I: Integer): TPolySegment; begin Result := Segs[I]; end;",
        "function IPCB_BoardOutline.PointCount: Integer; begin Result := Length(Segs); end;",
        _routine(gen, "AngleOnSweep"), _routine(gen, "OutlineExtents"),
        "procedure Put(O: IPCB_BoardOutline; I, K, X, Y, CX, CY: Integer; A1, A2: Double);",
        "begin O.Segs[I].Kind := K; O.Segs[I].vx := X; O.Segs[I].vy := Y; O.Segs[I].cx := CX;",
        "O.Segs[I].cy := CY; O.Segs[I].Angle1 := A1; O.Segs[I].Angle2 := A2; end;",
        "var O: IPCB_BoardOutline; L, B, R, T: Integer;",
        "begin",
        "  O := IPCB_BoardOutline.Create; SetLength(O.Segs, 4);",
        "  Put(O, 0, 0, 10000000, 10000000, 0, 0, 0, 0); Put(O, 1, 0, 80000000, 10000000, 0, 0, 0, 0);",
        "  Put(O, 2, 0, 80000000, 54000000, 0, 0, 0, 0); Put(O, 3, 0, 10000000, 54000000, 0, 0, 0, 0);",
        "  L := 0; B := 0; R := 70000000; T := 50000000;",
        "  OutlineExtents(O, L, B, R, T);",
        "  if (L <> 10000000) or (B <> 10000000) or (R <> 80000000) or (T <> 54000000) then Halt(1);",
        "  SetLength(O.Segs, 2);",
        "  Put(O, 0, 1, 10000000, 0, 0, 0, 0, 180); Put(O, 1, 0, -10000000, 0, 0, 0, 0, 0);",
        "  OutlineExtents(O, L, B, R, T);",
        "  if (L <> -10000000) or (B <> 0) or (R <> 10000000) or (T <> 10000000) then Halt(2);",
        "  SetLength(O.Segs, 0); L := 7; OutlineExtents(O, L, B, R, T);",
        "  if L <> 7 then Halt(3);",
        "end.",
    ])
    assert _run(tmp_path, "extents", program) == 0


def test_every_outline_reply_uses_the_measured_extents_and_a_reshape_refreshes():
    pcb, gen = _code("PCB.pas"), _code("Generic.pas")
    outline = _routine(pcb, "PCB_GetBoardOutline")
    assert "OutlineExtents(Outline, EL, EB, ER, ET)" in outline
    assert "CoordToMils(BR." not in outline
    geometry = _routine(gen, "Gen_GetPcbGeometry")
    assert "OutlineExtents(Board.BoardOutline, EL, EB, ER, ET)" in geometry
    assert "CoordToMils(BR." not in geometry
    shape = _routine(pcb, "PCB_SetBoardShape")
    assert shape.index("RefreshBoardOutline(Board)") > shape.index("BoardOutline.Validate")


def test_nan_and_infinity_go_out_as_null(tmp_path):
    utils = _code("Utils.pas")
    program = "\n".join([
        "program floats;", "{$mode delphi}", "uses SysUtils, Math;",
        _routine(utils, "FloatToJsonStr"),
        "begin",
        "  if FloatToJsonStr(NaN) <> 'null' then Halt(1);",
        "  if FloatToJsonStr(Infinity) <> 'null' then Halt(2);",
        "  if FloatToJsonStr(NegInfinity) <> 'null' then Halt(3);",
        "  if FloatToJsonStr(2.5) <> '2.5' then Halt(4);",
        "end.",
    ])
    assert _run(tmp_path, "floats", program) == 0


def test_the_acute_angle_audit_works_in_real_mils_and_reports_a_join_once():
    body = _routine(_code("Audit.pas"), "Audit_FindAcuteAngles")
    vectors = re.findall(r"V[12][XY]\s*:=\s*([^;]+);", body)
    assert vectors, "the audit's vectors were not found; update this test"
    assert all("/ 10000.0" in v for v in vectors), vectors
    assert "Other.I_ObjectAddress > Track.I_ObjectAddress" in body

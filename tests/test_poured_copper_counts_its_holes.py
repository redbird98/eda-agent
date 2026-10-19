# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A pour's copper area is its regions less their holes.

MEASURED on Altium 26.10.1: IPCB_Region.Area is the region's OUTER
contour. A GND pour that visibly cleared a via after a repour read
929.0304 mm2 before and after, exactly its outline. The holes are now
taken off, but only once the outer contour's own shoelace area agrees
with Area, which also decides whether a contour's points count from 0
or from 1; a region that fails that check makes the answer inexact
rather than wrong.

The helpers are extracted from PCB.pas and run under FPC against stub
contours of both numberings.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "altium"


def _routine(name: str) -> str:
    code = (SCRIPTS / "PCB.pas").read_text(encoding="utf-8", errors="replace")
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


MIL = 10000  # internal units per mil


def _program(checks):
    lines = [f"  if Abs({expr} - ({want!r})) > 0.01 then Halt({i});"
             for i, (expr, want) in enumerate(checks, 1)]
    return "\n".join([
        "program copper;", "{$mode delphi}", "uses SysUtils;",
        "type",
        # A contour whose points are numbered from First; anything outside
        # raises, as a real out-of-range read would.
        "IPCB_Contour = class Xs, Ys: array of Integer; First: Integer;",
        "  function GetX(I: Integer): Integer; function GetY(I: Integer): Integer;",
        "  function Count: Integer;",
        "  property X[I: Integer]: Integer read GetX; property Y[I: Integer]: Integer read GetY; end;",
        "IPCB_Region = class Area: Int64; Main: IPCB_Contour; HoleList: array of IPCB_Contour;",
        "  function MainContour: IPCB_Contour; function HoleCount: Integer;",
        "  function GetHole(I: Integer): IPCB_Contour;",
        "  property Holes[I: Integer]: IPCB_Contour read GetHole; end;",
        "function IPCB_Contour.GetX(I: Integer): Integer;",
        "begin if (I < First) or (I >= First + Length(Xs)) then raise Exception.Create('range');",
        "  Result := Xs[I - First]; end;",
        "function IPCB_Contour.GetY(I: Integer): Integer;",
        "begin if (I < First) or (I >= First + Length(Ys)) then raise Exception.Create('range');",
        "  Result := Ys[I - First]; end;",
        "function IPCB_Contour.Count: Integer; begin Result := Length(Xs); end;",
        "function IPCB_Region.MainContour: IPCB_Contour; begin Result := Main; end;",
        "function IPCB_Region.HoleCount: Integer; begin Result := Length(HoleList); end;",
        "function IPCB_Region.GetHole(I: Integer): IPCB_Contour; begin Result := HoleList[I]; end;",
        "function Sq(X0, Y0, S, First: Integer): IPCB_Contour;",
        "begin Result := IPCB_Contour.Create; Result.First := First;",
        "  SetLength(Result.Xs, 4); SetLength(Result.Ys, 4);",
        "  Result.Xs[0] := X0; Result.Ys[0] := Y0; Result.Xs[1] := X0 + S; Result.Ys[1] := Y0;",
        "  Result.Xs[2] := X0 + S; Result.Ys[2] := Y0 + S; Result.Xs[3] := X0; Result.Ys[3] := Y0 + S; end;",
        "function Region(First, Holes: Integer): IPCB_Region; var I: Integer;",
        f"begin Result := IPCB_Region.Create; Result.Main := Sq(0, 0, {1000 * MIL}, First);",
        f"  Result.Area := Int64({1000 * MIL}) * {1000 * MIL}; SetLength(Result.HoleList, Holes);",
        f"  for I := 0 to Holes - 1 do Result.HoleList[I] := Sq({100 * MIL} + I * {200 * MIL}, {100 * MIL}, {100 * MIL}, First); end;",
        _routine("ContourAreaSqMils"), _routine("RegionCopperSqMils"),
        "var Ok: Boolean; R: IPCB_Region;",
        "function Copper(R: IPCB_Region): Double; begin Ok := True; Result := RegionCopperSqMils(R, Ok); end;",
        "function Exact(R: IPCB_Region): Double; begin Copper(R); if Ok then Result := 1 else Result := 0; end;",
        "begin",
        *lines,
        "end.",
    ])


def _run(tmp_path, program):
    path = tmp_path / "copper.pas"
    path.write_text(program, encoding="utf-8")
    built = subprocess.run([_fpc(), str(path)], cwd=tmp_path, capture_output=True, text=True)
    assert built.returncode == 0, built.stdout + built.stderr
    exe = tmp_path / ("copper.exe" if os.name == "nt" else "copper")
    return subprocess.run([str(exe)], capture_output=True, text=True).returncode


def test_holes_come_off_whichever_way_the_points_are_numbered(tmp_path):
    checks = [
        # A 1000 mil square, no holes: the outer area as it is.
        ("Copper(Region(0, 0))", 1_000_000.0), ("Exact(Region(0, 0))", 1.0),
        # Two 100 mil holes, points from 0 and from 1: both come off.
        ("Copper(Region(0, 2))", 980_000.0), ("Exact(Region(0, 2))", 1.0),
        ("Copper(Region(1, 2))", 980_000.0), ("Exact(Region(1, 2))", 1.0),
    ]
    assert _run(tmp_path, _program(checks)) == 0


def test_a_contour_that_disagrees_with_area_is_not_trusted(tmp_path):
    # The outer contour no longer matches Area, so its holes cannot be
    # read with any confidence: the outer area stands, marked inexact.
    checks = [
        ("Copper(R)", 1_000_000.0), ("Exact(R)", 0.0),
    ]
    program = _program(checks).replace(
        "begin\n  if Abs(Copper(R)",
        f"begin\n  R := Region(0, 1); R.Main := Sq(0, 0, {500 * MIL}, 0);\n  if Abs(Copper(R)", 1)
    assert "R := Region(0, 1)" in program
    assert _run(tmp_path, program) == 0

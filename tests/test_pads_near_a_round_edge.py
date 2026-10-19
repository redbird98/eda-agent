# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""audit_find_pads_near_board_edge measures to arcs as well as lines.

On a round board it found 0 pads at a 118 mil threshold with a USB-C pad
0.5 mm from the edge. The whole measurement was Board.PrimPrimDistance
against the outline inside an empty Try, and a pad was counted only when
that call answered. The distance now comes from the outline's own
segments, and the helpers are extracted from Audit.pas and PCB.pas and
run under FPC on a round outline and a rectangular one.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "altium"
COORD = 10000  # internal units per mil


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


def _corners(x, y, w, h, rot):
    a = math.radians(rot)
    c, s = math.cos(a), math.sin(a)
    pts = []
    for lx, ly in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
        pts.append((x + lx * c - ly * s, y + lx * s + ly * c))
    return pts


R_BOARD = 1000.0
PAD_W, PAD_H, ROT = 11.811, 45.276, 329.5


def _usb_pad():
    """A connector pad pushed out until its farthest corner is 0.5 mm in."""
    gap = 19.685
    # Along the radius at 60 degrees, pad long axis roughly radial.
    ux, uy = math.cos(math.radians(60)), math.sin(math.radians(60))
    lo, hi = 0.0, R_BOARD
    for _ in range(200):
        mid = (lo + hi) / 2
        far = max(math.hypot(px, py) for px, py in _corners(mid * ux, mid * uy, PAD_W, PAD_H, ROT))
        if R_BOARD - far > gap:
            lo = mid
        else:
            hi = mid
    return lo * ux, lo * uy, gap


def _program(checks):
    audit, pcb = _code("Audit.pas"), _code("PCB.pas")
    lines = [f"  if Abs({expr} - ({want!r})) > 0.01 then Halt({i});"
             for i, (expr, want) in enumerate(checks, 1)]
    return "\n".join([
        "program edge;", "{$mode delphi}", "uses SysUtils, Math;",
        "const ePolySegmentLine = 0; ePolySegmentArc = 1;",
        "  ePadObject = 1; eViaObject = 2; eRounded = 1; eRectangular = 2;",
        "type TPolySegment = record Kind, vx, vy, cx, cy: Integer; Angle1, Angle2: Double; end;",
        "IPCB_BoardOutline = class Segs: array of TPolySegment;",
        "function GetSeg(I: Integer): TPolySegment; function PointCount: Integer;",
        "property Segments[I: Integer]: TPolySegment read GetSeg; end;",
        "IPCB_Primitive = class x, y, ObjectId, TopXSize, TopYSize, TopShape, Size: Integer;",
        "  Rotation: Double; end;",
        "IPCB_Pad = IPCB_Primitive; IPCB_Via = IPCB_Primitive;",
        "function IPCB_BoardOutline.GetSeg(I: Integer): TPolySegment; begin Result := Segs[I]; end;",
        "function IPCB_BoardOutline.PointCount: Integer; begin Result := Length(Segs); end;",
        _routine(pcb, "SegDistMils"), _routine(pcb, "ArcDistMils"),
        _routine(audit, "OutlineDistMils"), _routine(audit, "ShapePointGapMils"),
        _routine(audit, "PadEdgeGapMils"),
        "procedure Put(O: IPCB_BoardOutline; I, K, X, Y, CX, CY: Integer; A1, A2: Double);",
        "begin O.Segs[I].Kind := K; O.Segs[I].vx := X; O.Segs[I].vy := Y; O.Segs[I].cx := CX;",
        "O.Segs[I].cy := CY; O.Segs[I].Angle1 := A1; O.Segs[I].Angle2 := A2; end;",
        "function Prim(Kind, X, Y, W, H, Shape: Integer; Rot: Double): IPCB_Primitive;",
        "begin Result := IPCB_Primitive.Create; Result.ObjectId := Kind; Result.x := X;",
        "Result.y := Y; Result.TopXSize := W; Result.TopYSize := H; Result.Size := W;",
        "Result.TopShape := Shape; Result.Rotation := Rot; end;",
        "var Round_, Rect: IPCB_BoardOutline; R: Integer;",
        "begin",
        f"  R := {int(R_BOARD * COORD)};",
        "  Round_ := IPCB_BoardOutline.Create; SetLength(Round_.Segs, 4);",
        "  Put(Round_, 0, 1, R, 0, 0, 0, 0, 90); Put(Round_, 1, 1, 0, R, 0, 0, 90, 180);",
        "  Put(Round_, 2, 1, -R, 0, 0, 0, 180, 270); Put(Round_, 3, 1, 0, -R, 0, 0, 270, 360);",
        "  Rect := IPCB_BoardOutline.Create; SetLength(Rect.Segs, 4);",
        f"  Put(Rect, 0, 0, 0, 0, 0, 0, 0, 0); Put(Rect, 1, 0, {2000 * COORD}, 0, 0, 0, 0, 0);",
        f"  Put(Rect, 2, 0, {2000 * COORD}, {1000 * COORD}, 0, 0, 0, 0);",
        f"  Put(Rect, 3, 0, 0, {1000 * COORD}, 0, 0, 0, 0);",
        *lines, "end.",
    ])


def _run(tmp_path, program):
    path = tmp_path / "edge.pas"
    path.write_text(program, encoding="utf-8")
    built = subprocess.run([_fpc(), str(path)], cwd=tmp_path, capture_output=True, text=True)
    assert built.returncode == 0, built.stdout + built.stderr
    exe = tmp_path / ("edge.exe" if os.name == "nt" else "edge")
    return subprocess.run([str(exe)], capture_output=True, text=True).returncode


def test_a_turned_pad_near_a_round_edge_is_measured_to_the_arc(tmp_path):
    x, y, gap = _usb_pad()
    pad = (f"Prim(ePadObject, {round(x * COORD)}, {round(y * COORD)}, "
           f"{round(PAD_W * COORD)}, {round(PAD_H * COORD)}, eRectangular, {ROT})")
    centre = math.hypot(x, y)
    checks = [
        # A point at the centre is a radius from every quarter arc.
        ("OutlineDistMils(Round_, 0, 0)", R_BOARD),
        # A point on the 45 degree diagonal, which no vertex is near.
        ("OutlineDistMils(Round_, 500 * Cos(Pi / 4), 500 * Sin(Pi / 4))", R_BOARD - 500),
        ("OutlineDistMils(Round_, %r, %r)" % (x, y), R_BOARD - centre),
        # The pad itself: its farthest corner, 0.5 mm in.
        ("PadEdgeGapMils(Round_, %s)" % pad, gap),
    ]
    assert _run(tmp_path, _program(checks)) == 0


def test_vias_round_and_obround_pads_on_a_straight_edge(tmp_path):
    checks = [
        # A 20 mil via 30 mil from the left edge: 20 mil of board between.
        (f"PadEdgeGapMils(Rect, Prim(eViaObject, {30 * COORD}, {500 * COORD}, "
         f"{20 * COORD}, 0, 0, 0))", 20.0),
        # A 20 x 60 obround 50 mil below the top edge: upright, its end cap
        # reaches 30 mil up and leaves 20; turned 90 degrees it lies flat,
        # reaches 10 and leaves 40.
        (f"PadEdgeGapMils(Rect, Prim(ePadObject, {1000 * COORD}, {950 * COORD}, "
         f"{20 * COORD}, {60 * COORD}, eRounded, 0))", 20.0),
        (f"PadEdgeGapMils(Rect, Prim(ePadObject, {1000 * COORD}, {950 * COORD}, "
         f"{20 * COORD}, {60 * COORD}, eRounded, 90))", 40.0),
        # A rectangle 40 x 10 at 45 degrees near the bottom edge: its lowest
        # corner is (40 + 10) / 2 * sin 45 below the centre.
        (f"PadEdgeGapMils(Rect, Prim(ePadObject, {1000 * COORD}, {100 * COORD}, "
         f"{40 * COORD}, {10 * COORD}, eRectangular, 45))",
         100 - 25 / math.sqrt(2)),
        # Copper past the edge is a gap of 0, not a negative one.
        (f"PadEdgeGapMils(Rect, Prim(eViaObject, {5 * COORD}, {500 * COORD}, "
         f"{20 * COORD}, 0, 0, 0))", 0.0),
    ]
    assert _run(tmp_path, _program(checks)) == 0


def test_the_audit_no_longer_trusts_prim_prim_distance():
    body = _routine(_code("Audit.pas"), "Audit_FindPadsNearBoardEdge")
    assert "PrimPrimDistance" not in body
    assert "PadEdgeGapMils(Outline, Obj)" in body
    assert "Inc(Unmeasured)" in body and "'unmeasured'" in body

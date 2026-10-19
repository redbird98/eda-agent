# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A schematic object is registered in its container once.

AddSchObject and RegisterSchObjectInContainer both put an object into a
document. sch_replicate_component called both on its copy, Altium held
it twice under one key, and the next move of either part raised "An
item with the same key has already been added".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts" / "altium"
UNITS = sorted(p for p in SCRIPTS.glob("*.pas") if p.name != "Altium_MCP.pas")

_COMMENTS = re.compile(r"\{[^}]*\}|//[^\n]*|\(\*.*?\*\)", re.S)
_ROUTINE = re.compile(r"(?mi)^(?:Function|Procedure)\s+\w+")
_CALL = re.compile(
    r"(\w+(?:\.\w+)*)\.(AddSchObject|RegisterSchObjectInContainer)\(\s*(\w+)\s*\)")


def _doubles(code: str) -> list[str]:
    code = _COMMENTS.sub(" ", code)
    starts = [m.start() for m in _ROUTINE.finditer(code)] + [len(code)]
    found = []
    for a, b in zip(starts, starts[1:]):
        seen: dict[tuple[str, str], set[str]] = {}
        for owner, call, obj in _CALL.findall(code[a:b]):
            seen.setdefault((owner.lower(), obj.lower()), set()).add(call)
        name = _ROUTINE.match(code, a).group(0).split()[-1]
        found += [f"{name}: {o}.*({v})" for (o, v), calls in seen.items()
                  if len(calls) == 2]
    return found


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_no_routine_adds_and_registers_the_same_object(unit):
    assert _doubles(unit.read_text(encoding="utf-8", errors="replace")) == []


def test_the_check_finds_the_double_it_exists_for():
    code = ("Function Gen_ReplicateSchComponent(P : String) : String;\n"
            "Begin\n"
            "    Try SchDoc.AddSchObject(NewComp); Except End;\n"
            "    Try SchDoc.RegisterSchObjectInContainer(NewComp); Except End;\n"
            "End;\n")
    assert _doubles(code) == ["Gen_ReplicateSchComponent: schdoc.*(newcomp)"]
    # Different objects, or one in a comment, are not a double.
    assert _doubles(code.replace("RegisterSchObjectInContainer(NewComp)",
                                 "RegisterSchObjectInContainer(Other)")) == []
    assert _doubles(code.replace("Try SchDoc.AddSchObject(NewComp); Except End;",
                                 "{ SchDoc.AddSchObject(NewComp) }")) == []

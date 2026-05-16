# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba
"""Tests for design.pipeline: plan -> SchematicCanvas, pure Python.

The pipeline is the orchestrator; these tests check it produces a
sensible canvas without ever touching Altium. A MockExtractor returns
hand-built SymbolModels keyed by (lib_path, lib_ref).

Coverage:
- Happy path: every plan part lands as an instance; nets become wires
  or labels or ports.
- Missing symbol: pipeline fails cleanly with a per-(lib, ref) error.
- Missing pin: pipeline fails with the offending pin id.
- needs_creation parts: skipped with a warning note, not a failure.
- parameter_stamps: built for parts with metadata; empty for bare parts.
"""

from __future__ import annotations

from typing import Optional

import pytest

from eda_agent.design.pipeline import build_canvas_from_plan
from eda_agent.design.plan import DesignPlan
from eda_agent.design.symbols import (
    SymbolBBox,
    SymbolExtractor,
    SymbolModel,
    SymbolPin,
)


_LIB = "/fake/lib.SchLib"


class MockExtractor(SymbolExtractor):
    """Return canned SymbolModels without instantiating the bridge."""

    def __init__(self, symbols: dict[tuple[str, str], SymbolModel]) -> None:
        # Skip parent __init__ (no bridge/cache needed for tests).
        self._symbols = symbols

    def extract_one(self, lib_path: str, lib_ref: str) -> Optional[SymbolModel]:
        return self._symbols.get((lib_path, lib_ref))

    def extract_many(self, refs):
        return {
            (lib_path, lib_ref): self._symbols[(lib_path, lib_ref)]
            for (lib_path, lib_ref) in refs
            if (lib_path, lib_ref) in self._symbols
        }


def _passive(lib_ref: str) -> SymbolModel:
    return SymbolModel(
        lib_path=_LIB, lib_ref=lib_ref,
        pins=(
            SymbolPin(designator="1", name="1", x=-100, y=0,
                      orientation=2, length=100, electrical_type="passive"),
            SymbolPin(designator="2", name="2", x=100, y=0,
                      orientation=0, length=100, electrical_type="passive"),
        ),
        body_bbox=SymbolBBox(x_min=-50, y_min=-30, x_max=50, y_max=30),
    )


_BASE_SYMBOLS = {
    (_LIB, "RES"): _passive("RES"),
    (_LIB, "CAP"): _passive("CAP"),
}


def _basic_rc_plan(extra_part_fields: Optional[dict] = None) -> DesignPlan:
    """R1 in series with C1; one signal net and one ground net.

    Net.pins requires min 2 items, so the schema-shortest plan uses two
    2-pin nets that together connect every pin.
    """
    extra = extra_part_fields or {}
    return DesignPlan.model_validate({
        "spec": "rc lowpass",
        "summary": "trivial RC",
        "sheets": [{"name": "main", "title": "RC", "size": "A4"}],
        "zones": [{"name": "filter", "sheet": "main"}],
        "parts": [
            {"refdes": "R1", "lib_ref": "RES", "lib_path": _LIB,
             "value": "10k", "status": "existing",
             "sheet": "main", "zone": "filter", **extra.get("R1", {})},
            {"refdes": "C1", "lib_ref": "CAP", "lib_path": _LIB,
             "value": "100nF", "status": "existing",
             "sheet": "main", "zone": "filter", **extra.get("C1", {})},
        ],
        "nets": [
            {"name": "VOUT", "pins": [
                {"refdes": "R1", "pin": "2"},
                {"refdes": "C1", "pin": "1"}]},
            {"name": "GND", "is_ground": True, "pins": [
                {"refdes": "R1", "pin": "1"},
                {"refdes": "C1", "pin": "2"}]},
        ],
    })


def test_pipeline_happy_path():
    """A clean plan produces a canvas with every plan part placed."""
    # VIN net only has 1 pin; that violates Net.pins min items=2. Adjust:
    plan = DesignPlan.model_validate({
        "spec": "rc", "summary": "rc",
        "sheets": [{"name": "main", "size": "A4"}],
        "zones": [{"name": "z", "sheet": "main"}],
        "parts": [
            {"refdes": "R1", "lib_ref": "RES", "lib_path": _LIB,
             "value": "10k", "status": "existing",
             "sheet": "main", "zone": "z"},
            {"refdes": "C1", "lib_ref": "CAP", "lib_path": _LIB,
             "value": "100nF", "status": "existing",
             "sheet": "main", "zone": "z"},
        ],
        "nets": [
            {"name": "VOUT", "pins": [
                {"refdes": "R1", "pin": "2"},
                {"refdes": "C1", "pin": "1"}]},
            {"name": "GND", "is_ground": True, "pins": [
                {"refdes": "R1", "pin": "1"},
                {"refdes": "C1", "pin": "2"}]},
        ],
    })
    result = build_canvas_from_plan(plan, MockExtractor(_BASE_SYMBOLS))
    assert result.ok, [f.text for f in result.failures]
    assert result.placement_count == 2
    refdes_placed = {i.refdes for i in result.canvas.instances}
    assert refdes_placed == {"R1", "C1"}


def test_pipeline_missing_symbol_fails_cleanly():
    """Plan references a lib_ref the extractor can't produce -> hard failure."""
    plan = _basic_rc_plan()
    # Drop CAP from the available symbols so C1 can't resolve.
    extractor = MockExtractor({(_LIB, "RES"): _passive("RES")})
    result = build_canvas_from_plan(plan, extractor)
    assert not result.ok
    assert any("CAP" in f.text for f in result.failures)


def test_pipeline_skips_needs_creation_with_warning():
    """A needs_creation part should not appear on the canvas, and should
    surface a warning note (not a hard failure)."""
    plan = DesignPlan.model_validate({
        "spec": "x", "summary": "x",
        "sheets": [{"name": "main", "size": "A4"}],
        "zones": [{"name": "z", "sheet": "main"}],
        "parts": [
            {"refdes": "R1", "lib_ref": "RES", "lib_path": _LIB,
             "status": "existing", "sheet": "main", "zone": "z"},
            {"refdes": "U1", "lib_ref": "STM32G031F",
             "value": "STM32G031F",
             "status": "needs_creation", "sheet": "main", "zone": "z",
             "rationale": "no stm32 in library yet"},
        ],
        "nets": [
            {"name": "GND", "is_ground": True, "pins": [
                {"refdes": "R1", "pin": "1"},
                {"refdes": "R1", "pin": "2"}]},
        ],
    })
    result = build_canvas_from_plan(plan, MockExtractor(_BASE_SYMBOLS))
    # Hard failure only if R1 fails -- U1 is just a warning.
    refdes_placed = {i.refdes for i in result.canvas.instances}
    assert "U1" not in refdes_placed
    assert "R1" in refdes_placed
    assert any("U1" in n.text for n in result.notes)


def test_pipeline_unknown_pin_id_fails():
    """A plan net referencing a pin id not on the symbol -> failure."""
    plan = DesignPlan.model_validate({
        "spec": "x", "summary": "x",
        "sheets": [{"name": "main", "size": "A4"}],
        "zones": [{"name": "z", "sheet": "main"}],
        "parts": [
            {"refdes": "R1", "lib_ref": "RES", "lib_path": _LIB,
             "status": "existing", "sheet": "main", "zone": "z"},
            {"refdes": "R2", "lib_ref": "RES", "lib_path": _LIB,
             "status": "existing", "sheet": "main", "zone": "z"},
        ],
        "nets": [
            # Pin 99 doesn't exist on RES.
            {"name": "GND", "is_ground": True, "pins": [
                {"refdes": "R1", "pin": "99"},
                {"refdes": "R2", "pin": "1"}]},
        ],
    })
    result = build_canvas_from_plan(plan, MockExtractor(_BASE_SYMBOLS))
    assert not result.ok
    assert any("99" in f.text for f in result.failures)


def test_pipeline_parameter_stamps_carry_value_and_metadata():
    plan = _basic_rc_plan(extra_part_fields={
        "R1": {"manufacturer": "Yageo", "mpn": "RC0603FR-0710KL",
               "datasheet_url": "https://datasheet/yageo.pdf"},
    })
    result = build_canvas_from_plan(plan, MockExtractor(_BASE_SYMBOLS))
    assert result.ok, [f.text for f in result.failures]
    # R1 should get Value + Manufacturer + MPN + Datasheet stamps.
    r1_stamps = result.parameter_stamps.get("R1", {})
    assert r1_stamps.get("Value") == "10k"
    assert r1_stamps.get("Manufacturer") == "Yageo"
    assert r1_stamps.get("Manufacturer Part Number") == "RC0603FR-0710KL"
    assert r1_stamps.get("Datasheet") == "https://datasheet/yageo.pdf"
    # C1 has Value only.
    c1_stamps = result.parameter_stamps.get("C1", {})
    assert c1_stamps == {"Value": "100nF"}


def test_validation_flags_unrepresented_net():
    """A net whose pins are placed but never wired/labelled/ported should
    surface as a warning. We can't easily make the pipeline produce that
    state via its normal path -- it always tries to wire block-local nets
    and label cross-block ones -- so we exercise the validation helper
    directly against a hand-built canvas."""
    from eda_agent.design.canvas import SchematicCanvas, SymbolInstance
    from eda_agent.design.pipeline import (
        PipelineResult, _validate_canvas_against_plan,
    )

    sym = _passive("RES")
    canvas = SchematicCanvas()
    canvas.add_instance(SymbolInstance(refdes="R1", symbol=sym, x=0, y=0, rotation=0))
    canvas.add_instance(SymbolInstance(refdes="R2", symbol=sym, x=200, y=0, rotation=0))
    # No wires/labels/ports added at all.

    plan = DesignPlan.model_validate({
        "spec": "x", "summary": "x",
        "sheets": [{"name": "main", "size": "A4"}],
        "parts": [
            {"refdes": "R1", "lib_ref": "RES", "lib_path": _LIB,
             "status": "existing", "sheet": "main"},
            {"refdes": "R2", "lib_ref": "RES", "lib_path": _LIB,
             "status": "existing", "sheet": "main"},
        ],
        "nets": [
            {"name": "SIG", "pins": [
                {"refdes": "R1", "pin": "2"},
                {"refdes": "R2", "pin": "1"}]},
        ],
    })
    result = PipelineResult(canvas=canvas)
    _validate_canvas_against_plan(plan, canvas, result)
    warnings = [n for n in result.notes if n.severity == "warning"]
    assert any("SIG" in w.text for w in warnings)


def test_validation_quiet_when_net_pins_off_canvas():
    """A net whose pins reference refdes NOT placed on the canvas should
    NOT warn -- that's a multi-sheet case where the net lives elsewhere."""
    from eda_agent.design.canvas import SchematicCanvas
    from eda_agent.design.pipeline import (
        PipelineResult, _validate_canvas_against_plan,
    )

    canvas = SchematicCanvas()  # nothing placed
    plan = DesignPlan.model_validate({
        "spec": "x", "summary": "x",
        "sheets": [{"name": "main", "size": "A4"}],
        "parts": [
            {"refdes": "R1", "lib_ref": "RES", "lib_path": _LIB,
             "status": "existing", "sheet": "main"},
            {"refdes": "R2", "lib_ref": "RES", "lib_path": _LIB,
             "status": "existing", "sheet": "main"},
        ],
        "nets": [
            {"name": "SIG", "pins": [
                {"refdes": "R1", "pin": "2"},
                {"refdes": "R2", "pin": "1"}]},
        ],
    })
    result = PipelineResult(canvas=canvas)
    _validate_canvas_against_plan(plan, canvas, result)
    # No instances on canvas => net is "off this canvas" => no warning.
    warnings = [n for n in result.notes if n.severity == "warning"]
    assert warnings == []


def test_pipeline_power_net_produces_port_glyph():
    """is_ground=True net should emit at least one PowerPort on the canvas."""
    plan = DesignPlan.model_validate({
        "spec": "x", "summary": "x",
        "sheets": [{"name": "main", "size": "A4"}],
        "zones": [{"name": "z", "sheet": "main"}],
        "parts": [
            {"refdes": "R1", "lib_ref": "RES", "lib_path": _LIB,
             "status": "existing", "sheet": "main", "zone": "z"},
            {"refdes": "C1", "lib_ref": "CAP", "lib_path": _LIB,
             "status": "existing", "sheet": "main", "zone": "z"},
        ],
        "nets": [
            {"name": "GND", "is_ground": True, "pins": [
                {"refdes": "R1", "pin": "1"},
                {"refdes": "C1", "pin": "2"}]},
            {"name": "VOUT", "pins": [
                {"refdes": "R1", "pin": "2"},
                {"refdes": "C1", "pin": "1"}]},
        ],
    })
    result = build_canvas_from_plan(plan, MockExtractor(_BASE_SYMBOLS))
    assert result.ok, [f.text for f in result.failures]
    assert result.power_port_count >= 1
    gnd_ports = [p for p in result.canvas.power_ports if p.text == "GND"]
    assert gnd_ports, "GND net should produce a GND-named PowerPort"
    # GND glyph should be one of the gnd_* styles.
    assert any("gnd" in p.style.lower() for p in gnd_ports)

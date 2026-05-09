# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba
"""Schema tests for DesignPlan, round-trip, validation, cross-check."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from eda_agent.design.plan import (
    BomLine,
    DesignPlan,
    DesignRuleDelta,
    Net,
    Part,
    PartStatus,
    PinRef,
    Sheet,
    Zone,
)


def _minimal_plan() -> DesignPlan:
    """Smallest plan that should pass validation: two parts, one net."""
    return DesignPlan(
        spec="LED with 1k current-limiting resistor on 5V rail",
        summary="A single LED tied to GND through a 1k resistor on the 5V0 rail.",
        sheets=[Sheet(name="main", title="LED test", size="A4")],
        zones=[
            Zone(name="led_zone", sheet="main", origin_mm=(0.0, 0.0), size_mm=(40.0, 40.0))
        ],
        parts=[
            Part(refdes="R1", lib_ref="RES_0805", value="1k", sheet="main", zone="led_zone"),
            Part(refdes="D1", lib_ref="LED_RED", sheet="main", zone="led_zone"),
        ],
        nets=[
            Net(
                name="V5",
                pins=[PinRef(refdes="R1", pin="1")],
                is_power=True,
            ),
            Net(
                name="LED_A",
                pins=[PinRef(refdes="R1", pin="2"), PinRef(refdes="D1", pin="A")],
            ),
            Net(
                name="GND",
                pins=[PinRef(refdes="D1", pin="K")],
                is_ground=True,
            ),
        ],
    )


# A power/ground net of size 1 violates Net.pins min_length=2; we relax that
# only in `_minimal_plan` if needed. Build one that's actually valid:
def _valid_minimal_plan() -> DesignPlan:
    return DesignPlan(
        spec="LED with 1k current-limiting resistor on 5V rail",
        summary="LED tied to GND through 1k on the 5V0 rail.",
        sheets=[Sheet(name="main")],
        parts=[
            Part(refdes="R1", lib_ref="RES_0805", value="1k", sheet="main"),
            Part(refdes="D1", lib_ref="LED_RED", sheet="main"),
        ],
        nets=[
            Net(
                name="V5",
                is_power=True,
                pins=[
                    PinRef(refdes="R1", pin="1"),
                    PinRef(refdes="D1", pin="A"),  # placeholder so pins>=2
                ],
            ),
            Net(
                name="LED_A",
                pins=[
                    PinRef(refdes="R1", pin="2"),
                    PinRef(refdes="D1", pin="A"),
                ],
            ),
        ],
    )


def test_minimal_plan_validates() -> None:
    plan = _valid_minimal_plan()
    assert plan.spec.startswith("LED")
    assert len(plan.parts) == 2
    assert plan.cross_check() == []


def test_round_trip_through_json() -> None:
    plan = _valid_minimal_plan()
    blob = plan.model_dump_json()
    parsed = json.loads(blob)
    assert parsed["spec"] == plan.spec
    rehydrated = DesignPlan.model_validate(parsed)
    assert rehydrated == plan


def test_extra_fields_rejected() -> None:
    payload = json.loads(_valid_minimal_plan().model_dump_json())
    payload["mystery"] = "should_fail"
    with pytest.raises(ValidationError):
        DesignPlan.model_validate(payload)


def test_invalid_refdes_rejected() -> None:
    with pytest.raises(ValidationError):
        Part(refdes="r1", lib_ref="RES_0805", sheet="main")  # lowercase


def test_invalid_net_name_rejected() -> None:
    with pytest.raises(ValidationError):
        Net(
            name="3V3 (rail)",  # spaces & parens not allowed
            pins=[PinRef(refdes="R1", pin="1"), PinRef(refdes="R2", pin="1")],
        )


def test_duplicate_refdes_rejected() -> None:
    with pytest.raises(ValidationError):
        DesignPlan(
            spec="x",
            summary="x",
            sheets=[Sheet(name="main")],
            parts=[
                Part(refdes="R1", lib_ref="RES", sheet="main"),
                Part(refdes="R1", lib_ref="RES", sheet="main"),
            ],
            nets=[
                Net(
                    name="N1",
                    pins=[PinRef(refdes="R1", pin="1"), PinRef(refdes="R1", pin="2")],
                )
            ],
        )


def test_duplicate_net_rejected() -> None:
    with pytest.raises(ValidationError):
        DesignPlan(
            spec="x",
            summary="x",
            sheets=[Sheet(name="main")],
            parts=[Part(refdes="R1", lib_ref="RES", sheet="main")],
            nets=[
                Net(
                    name="N1",
                    pins=[PinRef(refdes="R1", pin="1"), PinRef(refdes="R1", pin="2")],
                ),
                Net(
                    name="N1",
                    pins=[PinRef(refdes="R1", pin="1"), PinRef(refdes="R1", pin="2")],
                ),
            ],
        )


def test_duplicate_pin_in_net_rejected() -> None:
    with pytest.raises(ValidationError):
        Net(
            name="N1",
            pins=[
                PinRef(refdes="R1", pin="1"),
                PinRef(refdes="R1", pin="1"),
            ],
        )


def test_cross_check_catches_unknown_sheet() -> None:
    plan = _valid_minimal_plan()
    plan.parts[0].sheet = "nonexistent"
    problems = plan.cross_check()
    assert any("nonexistent" in p for p in problems)


def test_cross_check_catches_unknown_refdes_in_net() -> None:
    plan = _valid_minimal_plan()
    plan.nets[0].pins[0] = PinRef(refdes="R99", pin="1")
    problems = plan.cross_check()
    assert any("R99" in p for p in problems)


def test_cross_check_catches_unknown_zone() -> None:
    plan = _valid_minimal_plan()
    plan.parts[0].zone = "ghost"
    problems = plan.cross_check()
    assert any("ghost" in p for p in problems)


def test_part_status_default_is_existing() -> None:
    p = Part(refdes="U1", lib_ref="STM32F103", sheet="main")
    assert p.status == PartStatus.EXISTING


def test_part_status_needs_creation_round_trips() -> None:
    p = Part(
        refdes="U1",
        lib_ref="MAX9611_NEW",
        sheet="main",
        status=PartStatus.NEEDS_CREATION,
    )
    blob = p.model_dump_json()
    rehydrated = Part.model_validate_json(blob)
    assert rehydrated.status == PartStatus.NEEDS_CREATION


def test_design_rule_delta_round_trip() -> None:
    rule = DesignRuleDelta(
        rule_kind="Width",
        scope="POWER",
        parameters={"min_mil": "10", "preferred_mil": "20"},
    )
    blob = rule.model_dump_json()
    assert DesignRuleDelta.model_validate_json(blob) == rule


def test_bom_line_round_trip() -> None:
    bom = BomLine(refdes_list=["R1", "R2"], mpn="ERJ-6ENF1001V", qty=2)
    blob = bom.model_dump_json()
    assert BomLine.model_validate_json(blob) == bom


def test_open_questions_default_empty() -> None:
    plan = _valid_minimal_plan()
    assert plan.open_questions == []

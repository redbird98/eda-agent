# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba
"""Layout helper tests — grid placement is deterministic and non-overlapping."""

from __future__ import annotations

from eda_agent.design.layout import (
    DEFAULT_COLS_PER_ROW,
    GRID_PITCH_X_MILS,
    GRID_PITCH_Y_MILS,
    compute_layout,
)
from eda_agent.design.plan import DesignPlan, Net, Part, PinRef, Sheet, Zone


def _plan_with_n_parts(n: int) -> DesignPlan:
    parts = [Part(refdes=f"R{i + 1}", lib_ref="RES", sheet="main") for i in range(n)]
    nets = [
        Net(
            name=f"N{i}",
            pins=[
                PinRef(refdes=parts[i].refdes, pin="1"),
                PinRef(refdes=parts[(i + 1) % n].refdes, pin="2"),
            ],
        )
        for i in range(max(1, n))
    ]
    return DesignPlan(
        spec="x",
        summary="x",
        sheets=[Sheet(name="main")],
        parts=parts,
        nets=nets,
    )


def test_layout_produces_one_placement_per_part() -> None:
    plan = _plan_with_n_parts(5)
    placements = compute_layout(plan)
    assert {p.refdes for p in placements} == {p.refdes for p in plan.parts}


def test_layout_no_overlapping_coords() -> None:
    plan = _plan_with_n_parts(20)
    placements = compute_layout(plan)
    coords = {(p.x_mils, p.y_mils) for p in placements}
    assert len(coords) == len(placements)


def test_layout_wraps_to_next_row() -> None:
    plan = _plan_with_n_parts(DEFAULT_COLS_PER_ROW + 1)
    placements = compute_layout(plan)
    by_refdes = {p.refdes: p for p in placements}
    first = by_refdes["R1"]
    last = by_refdes[f"R{DEFAULT_COLS_PER_ROW + 1}"]
    assert last.y_mils == first.y_mils - GRID_PITCH_Y_MILS


def test_layout_grid_pitch() -> None:
    plan = _plan_with_n_parts(2)
    placements = compute_layout(plan)
    a, b = sorted(placements, key=lambda p: p.refdes)
    assert b.x_mils - a.x_mils == GRID_PITCH_X_MILS


def test_layout_separates_zones() -> None:
    plan = DesignPlan(
        spec="x",
        summary="x",
        sheets=[Sheet(name="main")],
        zones=[
            Zone(name="left", origin_mm=(0.0, 0.0)),
            Zone(name="right", origin_mm=(100.0, 0.0)),
        ],
        parts=[
            Part(refdes="R1", lib_ref="RES", sheet="main", zone="left"),
            Part(refdes="R2", lib_ref="RES", sheet="main", zone="right"),
        ],
        nets=[
            Net(
                name="N1",
                pins=[PinRef(refdes="R1", pin="1"), PinRef(refdes="R2", pin="1")],
            )
        ],
    )
    placements = compute_layout(plan)
    by_ref = {p.refdes: p for p in placements}
    # 100mm gap should land R2 well past R1
    assert by_ref["R2"].x_mils > by_ref["R1"].x_mils + 1000


def test_layout_handles_unzoned_parts() -> None:
    plan = _plan_with_n_parts(3)
    placements = compute_layout(plan)
    assert len(placements) == 3

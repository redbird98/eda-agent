# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba
"""Layout helper tests.

The layout is force-directed (springs along nets, repulsion between
all pairs). Tests assert behavioural properties:
- One placement per part, all within the sheet.
- No two parts overlap (centre-to-centre >= sum of half-bboxes).
- Parts that share a net cluster within a reasonable radius.
- Connectors tagged with role='power_in' land near the left edge.
- Multi-IC plans place each IC inside the sheet, separated.
"""

from __future__ import annotations

from eda_agent.design.layout import (
    SHEET_MAX_X_MILS,
    SHEET_MAX_Y_MILS,
    SHEET_ORIGIN_X_MILS,
    SHEET_ORIGIN_Y_MILS,
    _bbox_half,
    _force_directed_layout,
    _hard_shove_pass,
    _pin_count_per_part,
    audit_overlaps,
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


def _distance(a, b) -> float:
    return ((a.x_mils - b.x_mils) ** 2 + (a.y_mils - b.y_mils) ** 2) ** 0.5


def _ic_part(refdes: str = "U1") -> Part:
    return Part(refdes=refdes, lib_ref="LM358", sheet="main")


def test_layout_produces_one_placement_per_part() -> None:
    plan = _plan_with_n_parts(5)
    placements = compute_layout(plan)
    assert {p.refdes for p in placements} == {p.refdes for p in plan.parts}


def test_layout_all_inside_sheet() -> None:
    plan = _plan_with_n_parts(15)
    placements = compute_layout(plan)
    for p in placements:
        assert SHEET_ORIGIN_X_MILS <= p.x_mils <= SHEET_MAX_X_MILS
        assert SHEET_ORIGIN_Y_MILS <= p.y_mils <= SHEET_MAX_Y_MILS


def test_layout_no_overlap() -> None:
    """No two parts share the same (x, y); centre-to-centre separation
    is at least 700 mils (the conservative bbox for a 2-pin part)."""
    plan = _plan_with_n_parts(12)
    placements = compute_layout(plan)
    for i, a in enumerate(placements):
        for b in placements[i + 1 :]:
            assert (a.x_mils, a.y_mils) != (b.x_mils, b.y_mils)
            assert _distance(a, b) >= 700


def test_layout_handles_single_part() -> None:
    plan = DesignPlan(
        spec="x",
        summary="x",
        sheets=[Sheet(name="main")],
        parts=[Part(refdes="R1", lib_ref="RES", sheet="main")],
        nets=[
            Net(
                name="X",
                pins=[
                    PinRef(refdes="R1", pin="1"),
                    PinRef(refdes="R1", pin="2"),
                ],
            )
        ],
    )
    placements = compute_layout(plan)
    assert len(placements) == 1


def test_layout_decoupling_cap_clusters_near_ic() -> None:
    """C1 across VCC and GND, U1 also tied to VCC. The spring pulls C1
    within a couple-of-bbox radius of U1."""
    plan = DesignPlan(
        spec="x",
        summary="x",
        sheets=[Sheet(name="main")],
        parts=[
            _ic_part("U1"),
            Part(refdes="C1", lib_ref="CAP", value="100nF", sheet="main"),
        ],
        nets=[
            Net(
                name="VCC",
                is_power=True,
                pins=[
                    PinRef(refdes="U1", pin="1"),
                    PinRef(refdes="C1", pin="1"),
                ],
            ),
            Net(
                name="GND",
                is_ground=True,
                pins=[
                    PinRef(refdes="U1", pin="2"),
                    PinRef(refdes="U1", pin="3"),
                    PinRef(refdes="C1", pin="2"),
                ],
            ),
            Net(
                name="SIG",
                pins=[
                    PinRef(refdes="U1", pin="4"),
                    PinRef(refdes="U1", pin="1"),
                ],
            ),
        ],
    )
    placements = {p.refdes: p for p in compute_layout(plan)}
    assert _distance(placements["C1"], placements["U1"]) < 2500


def test_layout_pullup_resistor_clusters_near_ic() -> None:
    """R1 sits between VCC and a signal net touching U1.VIN. Spring
    pulls R1 close to U1."""
    plan = DesignPlan(
        spec="x",
        summary="x",
        sheets=[Sheet(name="main")],
        parts=[
            _ic_part("U1"),
            Part(refdes="R1", lib_ref="RES", value="10k", sheet="main"),
        ],
        nets=[
            Net(
                name="VCC",
                is_power=True,
                pins=[
                    PinRef(refdes="U1", pin="1"),
                    PinRef(refdes="R1", pin="1"),
                ],
            ),
            Net(
                name="GND",
                is_ground=True,
                pins=[
                    PinRef(refdes="U1", pin="2"),
                    PinRef(refdes="U1", pin="3"),
                ],
            ),
            Net(
                name="MID",
                pins=[
                    PinRef(refdes="U1", pin="4"),
                    PinRef(refdes="R1", pin="2"),
                ],
            ),
        ],
    )
    placements = {p.refdes: p for p in compute_layout(plan)}
    assert _distance(placements["R1"], placements["U1"]) < 2500


def test_layout_unconnected_parts_separate() -> None:
    """Two parts with no shared net should NOT cluster — repulsion
    pushes them apart well beyond their bboxes."""
    plan = DesignPlan(
        spec="x",
        summary="x",
        sheets=[Sheet(name="main")],
        parts=[
            Part(refdes="R1", lib_ref="RES", sheet="main"),
            Part(refdes="R2", lib_ref="RES", sheet="main"),
        ],
        nets=[
            Net(
                name="N1",
                pins=[PinRef(refdes="R1", pin="1"), PinRef(refdes="R1", pin="2")],
            ),
            Net(
                name="N2",
                pins=[PinRef(refdes="R2", pin="1"), PinRef(refdes="R2", pin="2")],
            ),
        ],
    )
    placements = {p.refdes: p for p in compute_layout(plan)}
    assert _distance(placements["R1"], placements["R2"]) > 800


def test_layout_power_in_connector_near_left_edge() -> None:
    """A connector tagged role='power_in' is biased toward the left edge,
    so it lands left of the IC it connects to."""
    plan = DesignPlan(
        spec="x",
        summary="x",
        sheets=[Sheet(name="main")],
        zones=[Zone(name="pwr_in", role="power_in", origin_mm=(0.0, 0.0))],
        parts=[
            _ic_part("U1"),
            Part(refdes="J1", lib_ref="HDR2", sheet="main", zone="pwr_in"),
        ],
        nets=[
            Net(
                name="VCC",
                is_power=True,
                pins=[
                    PinRef(refdes="U1", pin="1"),
                    PinRef(refdes="J1", pin="1"),
                ],
            ),
            Net(
                name="GND",
                is_ground=True,
                pins=[
                    PinRef(refdes="U1", pin="2"),
                    PinRef(refdes="U1", pin="3"),
                    PinRef(refdes="U1", pin="4"),
                    PinRef(refdes="J1", pin="2"),
                ],
            ),
        ],
    )
    placements = {p.refdes: p for p in compute_layout(plan)}
    assert placements["J1"].x_mils < placements["U1"].x_mils


def test_layout_two_ics_both_inside_sheet() -> None:
    """Two ICs sharing two nets: both inside the sheet, well separated."""
    plan = DesignPlan(
        spec="x",
        summary="x",
        sheets=[Sheet(name="main")],
        parts=[_ic_part("U1"), _ic_part("U2")],
        nets=[
            Net(
                name="A",
                pins=[PinRef(refdes="U1", pin=str(i)) for i in range(1, 5)]
                + [PinRef(refdes="U2", pin="1")],
            ),
            Net(
                name="B",
                pins=[PinRef(refdes="U2", pin=str(i)) for i in range(2, 5)]
                + [PinRef(refdes="U1", pin="1")],
            ),
        ],
    )
    placements = {p.refdes: p for p in compute_layout(plan)}
    for u in ("U1", "U2"):
        assert SHEET_ORIGIN_X_MILS <= placements[u].x_mils <= SHEET_MAX_X_MILS
        assert SHEET_ORIGIN_Y_MILS <= placements[u].y_mils <= SHEET_MAX_Y_MILS
    assert _distance(placements["U1"], placements["U2"]) >= 1200


def test_layout_deterministic_across_runs() -> None:
    """Same plan -> same placement. Uses a seeded RNG for jitter."""
    plan = _plan_with_n_parts(8)
    a = compute_layout(plan)
    b = compute_layout(plan)
    a_by = {p.refdes: (p.x_mils, p.y_mils) for p in a}
    b_by = {p.refdes: (p.x_mils, p.y_mils) for p in b}
    assert a_by == b_by


# ---------------------------------------------------------------------
# Hard-shove (audit-aware second pass) tests.
#
# The force-directed solver alone converges to a local minimum and on
# dense plans (the 14-part buck) leaves bbox overlaps. The shove pass
# audits the converged result and pushes overlapping pairs apart.
# These tests pin down the shove's invariants.
# ---------------------------------------------------------------------


def test_shove_separates_two_overlapping_parts() -> None:
    """Two parts placed on top of each other are pushed apart so their
    bboxes no longer intersect."""
    plan = DesignPlan(
        spec="x",
        summary="x",
        sheets=[Sheet(name="main")],
        parts=[
            Part(refdes="R1", lib_ref="RES", sheet="main"),
            Part(refdes="R2", lib_ref="RES", sheet="main"),
        ],
        nets=[
            Net(
                name="N",
                pins=[PinRef(refdes="R1", pin="1"), PinRef(refdes="R2", pin="1")],
            )
        ],
    )
    # Fabricate a pre-shove placement where both parts share (x, y).
    from eda_agent.design.layout import PlacedPart

    overlap = [
        PlacedPart(refdes="R1", sheet="main", x_mils=5000, y_mils=4000),
        PlacedPart(refdes="R2", sheet="main", x_mils=5000, y_mils=4000),
    ]
    cleaned, residual = _hard_shove_pass(plan, overlap)
    assert residual == 0
    assert audit_overlaps(plan, cleaned) == []


def test_shove_buck_plan_has_zero_overlaps() -> None:
    """The 14-part dense buck plan must come out of compute_layout with
    no pairwise bbox overlaps."""
    parts = [
        Part(refdes="U1", lib_ref="TPS54331D", sheet="main", role="ic"),
        Part(refdes="D1", lib_ref="SS14", sheet="main", role="diode"),
        Part(refdes="L1", lib_ref="IND", sheet="main", role="inductor"),
        Part(refdes="C1", lib_ref="CAP", sheet="main", role="cin_bulk"),
        Part(refdes="C2", lib_ref="CAP", sheet="main", role="cin_hf"),
        Part(refdes="C3", lib_ref="CAP", sheet="main", role="cout"),
        Part(refdes="C4", lib_ref="CAP", sheet="main", role="cboot"),
        Part(refdes="C5", lib_ref="CAP", sheet="main", role="cz_comp"),
        Part(refdes="C6", lib_ref="CAP", sheet="main", role="cp_comp"),
        Part(refdes="R1", lib_ref="RES", sheet="main", role="rfb_top"),
        Part(refdes="R2", lib_ref="RES", sheet="main", role="rfb_bot"),
        Part(refdes="R3", lib_ref="RES", sheet="main", role="rcomp"),
        Part(refdes="J1", lib_ref="HDR2", sheet="main", role="vin_conn"),
        Part(refdes="J2", lib_ref="HDR2", sheet="main", role="vout_conn"),
    ]
    # Net topology mirrors a buck: VIN, VOUT, GND, SW, FB, COMP, BOOT.
    nets = [
        Net(
            name="VIN",
            is_power=True,
            pins=[
                PinRef(refdes="J1", pin="1"),
                PinRef(refdes="U1", pin="2"),
                PinRef(refdes="C1", pin="1"),
                PinRef(refdes="C2", pin="1"),
            ],
        ),
        Net(
            name="VOUT",
            is_power=True,
            pins=[
                PinRef(refdes="L1", pin="2"),
                PinRef(refdes="C3", pin="1"),
                PinRef(refdes="R1", pin="1"),
                PinRef(refdes="J2", pin="1"),
            ],
        ),
        Net(
            name="GND",
            is_ground=True,
            pins=[
                PinRef(refdes="J1", pin="2"),
                PinRef(refdes="U1", pin="7"),
                PinRef(refdes="C1", pin="2"),
                PinRef(refdes="C2", pin="2"),
                PinRef(refdes="C3", pin="2"),
                PinRef(refdes="D1", pin="1"),
                PinRef(refdes="R2", pin="2"),
                PinRef(refdes="C5", pin="2"),
                PinRef(refdes="J2", pin="2"),
            ],
        ),
        Net(
            name="SW",
            pins=[
                PinRef(refdes="U1", pin="8"),
                PinRef(refdes="D1", pin="2"),
                PinRef(refdes="L1", pin="1"),
            ],
        ),
        Net(
            name="FB",
            pins=[
                PinRef(refdes="U1", pin="5"),
                PinRef(refdes="R1", pin="2"),
                PinRef(refdes="R2", pin="1"),
            ],
        ),
        Net(
            name="COMP",
            pins=[
                PinRef(refdes="U1", pin="6"),
                PinRef(refdes="R3", pin="1"),
                PinRef(refdes="C6", pin="1"),
            ],
        ),
        Net(
            name="COMP_Z",
            pins=[
                PinRef(refdes="R3", pin="2"),
                PinRef(refdes="C5", pin="1"),
                PinRef(refdes="C6", pin="2"),
            ],
        ),
        Net(
            name="BOOT",
            pins=[
                PinRef(refdes="U1", pin="1"),
                PinRef(refdes="C4", pin="1"),
            ],
        ),
    ]
    plan = DesignPlan(
        spec="buck", summary="14-part buck", topology="buck",
        sheets=[Sheet(name="main")], parts=parts, nets=nets,
    )
    placed = compute_layout(plan)
    assert audit_overlaps(plan, placed) == []


def test_shove_power_in_connector_stays_near_left_edge() -> None:
    """``power_in`` connectors are edge-biased — the shove must NOT
    yank one back into the interior to resolve an overlap. The other
    part absorbs the push instead."""
    plan = DesignPlan(
        spec="x",
        summary="x",
        sheets=[Sheet(name="main")],
        zones=[Zone(name="pwr_in", role="power_in", origin_mm=(0.0, 0.0))],
        parts=[
            _ic_part("U1"),
            Part(refdes="J1", lib_ref="HDR2", sheet="main", zone="pwr_in"),
            Part(refdes="C1", lib_ref="CAP", sheet="main"),
            Part(refdes="C2", lib_ref="CAP", sheet="main"),
            Part(refdes="R1", lib_ref="RES", sheet="main"),
        ],
        nets=[
            Net(
                name="VCC",
                is_power=True,
                pins=[
                    PinRef(refdes="J1", pin="1"),
                    PinRef(refdes="U1", pin="1"),
                    PinRef(refdes="C1", pin="1"),
                    PinRef(refdes="C2", pin="1"),
                    PinRef(refdes="R1", pin="1"),
                ],
            ),
            Net(
                name="GND",
                is_ground=True,
                pins=[
                    PinRef(refdes="J1", pin="2"),
                    PinRef(refdes="U1", pin="2"),
                    PinRef(refdes="U1", pin="3"),
                    PinRef(refdes="C1", pin="2"),
                    PinRef(refdes="C2", pin="2"),
                    PinRef(refdes="R1", pin="2"),
                ],
            ),
        ],
    )
    placements = {p.refdes: p for p in compute_layout(plan)}
    # Sheet midpoint is ~5750 mils; J1 should sit well left of it.
    sheet_mid_x = (SHEET_ORIGIN_X_MILS + SHEET_MAX_X_MILS) // 2
    assert placements["J1"].x_mils < sheet_mid_x
    # And left of U1 specifically.
    assert placements["J1"].x_mils < placements["U1"].x_mils


def test_shove_ic_moves_less_than_passive() -> None:
    """When an IC and a passive overlap, the IC absorbs ~20% of the
    push, the passive ~80%. Measured as delta from the pre-shove
    position to the post-shove position."""
    plan = DesignPlan(
        spec="x",
        summary="x",
        sheets=[Sheet(name="main")],
        parts=[
            # IC with 4+ pins.
            Part(refdes="U1", lib_ref="LM358", sheet="main"),
            # 2-pin passive.
            Part(refdes="R1", lib_ref="RES", sheet="main"),
        ],
        nets=[
            Net(
                name="A",
                pins=[
                    PinRef(refdes="U1", pin="1"),
                    PinRef(refdes="U1", pin="2"),
                    PinRef(refdes="U1", pin="3"),
                    PinRef(refdes="U1", pin="4"),
                    PinRef(refdes="R1", pin="1"),
                ],
            ),
            Net(
                name="B",
                pins=[PinRef(refdes="U1", pin="1"), PinRef(refdes="R1", pin="2")],
            ),
        ],
    )
    from eda_agent.design.layout import PlacedPart

    # Hand-built overlap: same (x, y).
    overlap = [
        PlacedPart(refdes="U1", sheet="main", x_mils=5000, y_mils=4000),
        PlacedPart(refdes="R1", sheet="main", x_mils=5000, y_mils=4000),
    ]
    cleaned, residual = _hard_shove_pass(plan, overlap)
    assert residual == 0
    by = {p.refdes: p for p in cleaned}
    d_u1 = abs(by["U1"].x_mils - 5000) + abs(by["U1"].y_mils - 4000)
    d_r1 = abs(by["R1"].x_mils - 5000) + abs(by["R1"].y_mils - 4000)
    # R1 should have moved strictly more than U1.
    assert d_r1 > d_u1


def test_shove_wall_redirects_push_to_other_part() -> None:
    """If one half of the pair is jammed against the right wall, the
    push goes entirely into the other part rather than into the wall.
    The wall-bound part must still satisfy its in-sheet invariant."""
    plan = DesignPlan(
        spec="x",
        summary="x",
        sheets=[Sheet(name="main")],
        parts=[
            Part(refdes="R1", lib_ref="RES", sheet="main"),
            Part(refdes="R2", lib_ref="RES", sheet="main"),
        ],
        nets=[
            Net(
                name="N",
                pins=[PinRef(refdes="R1", pin="1"), PinRef(refdes="R2", pin="1")],
            )
        ],
    )
    from eda_agent.design.layout import PlacedPart

    pin_count = _pin_count_per_part(plan)
    half_r1 = _bbox_half(pin_count["R1"])
    half_r2 = _bbox_half(pin_count["R2"])
    # R2 pinned at the right wall (centre = MAX_X - half).
    r2_x_at_wall = SHEET_MAX_X_MILS - half_r2
    overlap = [
        PlacedPart(refdes="R1", sheet="main", x_mils=r2_x_at_wall - 100, y_mils=4000),
        PlacedPart(refdes="R2", sheet="main", x_mils=r2_x_at_wall, y_mils=4000),
    ]
    cleaned, residual = _hard_shove_pass(plan, overlap)
    assert residual == 0
    by = {p.refdes: p for p in cleaned}
    # Both still inside the sheet.
    assert SHEET_ORIGIN_X_MILS + half_r1 <= by["R1"].x_mils <= SHEET_MAX_X_MILS - half_r1
    assert SHEET_ORIGIN_X_MILS + half_r2 <= by["R2"].x_mils <= SHEET_MAX_X_MILS - half_r2
    # R1 should have moved LEFT (away from the wall) — the wall
    # redirected the push back into it.
    assert by["R1"].x_mils < r2_x_at_wall - 100


def test_shove_single_and_empty_plan_trivially_returns() -> None:
    """0-part and 1-part plans must round-trip through the shove with
    no error."""
    # Single part.
    one_part = DesignPlan(
        spec="x",
        summary="x",
        sheets=[Sheet(name="main")],
        parts=[Part(refdes="R1", lib_ref="RES", sheet="main")],
        nets=[
            Net(
                name="N",
                pins=[
                    PinRef(refdes="R1", pin="1"),
                    PinRef(refdes="R1", pin="2"),
                ],
            )
        ],
    )
    placed = compute_layout(one_part)
    assert len(placed) == 1
    assert audit_overlaps(one_part, placed) == []
    # Empty placement list — exercise the early-return branch directly.
    cleaned, residual = _hard_shove_pass(one_part, [])
    assert cleaned == []
    assert residual == 0

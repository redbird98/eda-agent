# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba
"""Executor tests with a fake bridge — no Altium round-trips."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eda_agent.design.executor import (
    ExecutorResult,
    execute_plan,
    execute_plan_from_json,
)
from eda_agent.design.plan import (
    DesignPlan,
    Net,
    Part,
    PartStatus,
    PinRef,
    Sheet,
)


class _FakeBridge:
    """Records every send_command call; configurable failure simulation.

    pin_layouts maps refdes -> [{pin_number, pin_name, x_mils, y_mils}];
    when generic.get_sch_component_pins is called we return the matching
    layout. Defaults to a 2-pin layout for every queried refdes so tests
    that don't care about exact pin positions still get a sensible response.
    """

    def __init__(
        self,
        fail_commands: set[str] | None = None,
        pin_layouts: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_commands = fail_commands or set()
        self.pin_layouts = pin_layouts or {}

    def send_command(
        self,
        command: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        self.calls.append((command, params or {}))
        if command in self.fail_commands:
            raise RuntimeError(f"forced failure on {command}")

        if command == "generic.get_sch_component_pins":
            designator = (params or {}).get("designator", "")
            layout = self.pin_layouts.get(
                designator,
                [
                    {"pin_number": "1", "pin_name": "A", "x_mils": 1000, "y_mils": 5000},
                    {"pin_number": "2", "pin_name": "K", "x_mils": 1100, "y_mils": 5000},
                ],
            )
            return {"designator": designator, "pins": layout}

        return {"ok": True}


def _two_part_plan() -> DesignPlan:
    return DesignPlan(
        spec="LED + 1k on 5V",
        summary="LED tied to GND through 1k on the V5 rail.",
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
                    PinRef(refdes="D1", pin="A"),
                ],
            ),
            Net(
                name="LED_K",
                pins=[
                    PinRef(refdes="R1", pin="2"),
                    PinRef(refdes="D1", pin="K"),
                ],
            ),
        ],
    )


def test_executor_creates_project_when_missing(tmp_path: Path) -> None:
    bridge = _FakeBridge()
    result = execute_plan(
        _two_part_plan(),
        str(tmp_path / "test.PrjPcb"),
        bridge=bridge,
    )

    assert result.ok is True
    cmds = [c for c, _ in bridge.calls]
    assert "project.create" in cmds
    assert "application.create_document" in cmds
    assert "application.set_active_document" in cmds
    assert cmds.count("generic.place_sch_component_from_library") == 2
    assert cmds[-1] == "application.save_all"


def test_executor_opens_existing_project(tmp_path: Path) -> None:
    bridge = _FakeBridge()
    project = tmp_path / "test.PrjPcb"
    project.write_text("[Design]\nVersion=1.0\n", encoding="utf-8")

    result = execute_plan(_two_part_plan(), str(project), bridge=bridge)

    assert result.ok is True
    cmds = [c for c, _ in bridge.calls]
    assert "project.open" in cmds
    assert "project.create" not in cmds


def test_executor_halts_on_needs_creation(tmp_path: Path) -> None:
    bridge = _FakeBridge()
    plan = DesignPlan(
        spec="custom IC",
        summary="needs an IC we don't have yet.",
        sheets=[Sheet(name="main")],
        parts=[
            Part(
                refdes="U1",
                lib_ref="MAX9999",
                status=PartStatus.NEEDS_CREATION,
                sheet="main",
                value="MAX9999AUT",
            ),
            Part(refdes="C1", lib_ref="CAP_0805", sheet="main"),
        ],
        nets=[
            Net(
                name="VCC",
                is_power=True,
                pins=[PinRef(refdes="U1", pin="1"), PinRef(refdes="C1", pin="1")],
            )
        ],
    )

    result = execute_plan(plan, str(tmp_path / "x.PrjPcb"), bridge=bridge)

    assert result.ok is False
    assert result.needs_creation == ["U1"]
    assert bridge.calls == []  # no Altium mutation when halting early


def test_executor_reports_place_failures(tmp_path: Path) -> None:
    bridge = _FakeBridge(fail_commands={"generic.place_sch_component_from_library"})
    result = execute_plan(
        _two_part_plan(),
        str(tmp_path / "x.PrjPcb"),
        bridge=bridge,
    )
    assert result.ok is False
    assert len(result.failures) == 2
    assert all(f.code == "PLACE_FAILED" for f in result.failures)


def test_executor_places_parts_at_distinct_coords(tmp_path: Path) -> None:
    bridge = _FakeBridge()
    execute_plan(_two_part_plan(), str(tmp_path / "x.PrjPcb"), bridge=bridge)

    place_calls = [
        params for cmd, params in bridge.calls
        if cmd == "generic.place_sch_component_from_library"
    ]
    assert len(place_calls) == 2
    coords = {(p["x"], p["y"]) for p in place_calls}
    assert len(coords) == 2  # no two parts at the same coords


def test_executor_passes_designator_and_lib_ref(tmp_path: Path) -> None:
    bridge = _FakeBridge()
    execute_plan(_two_part_plan(), str(tmp_path / "x.PrjPcb"), bridge=bridge)

    place_calls = [
        params for cmd, params in bridge.calls
        if cmd == "generic.place_sch_component_from_library"
    ]
    designators = {p["designator"] for p in place_calls}
    assert designators == {"R1", "D1"}
    lib_refs = {p["lib_reference"] for p in place_calls}
    assert lib_refs == {"RES_0805", "LED_RED"}


def test_execute_plan_from_json_invalid_json_returns_error(tmp_path: Path) -> None:
    bridge = _FakeBridge()
    result = execute_plan_from_json("not json", str(tmp_path / "x.PrjPcb"), bridge=bridge)
    assert result.ok is False
    assert any("invalid JSON" in n for n in result.notes)
    assert bridge.calls == []


def test_execute_plan_from_json_schema_error_returns_error(tmp_path: Path) -> None:
    bridge = _FakeBridge()
    result = execute_plan_from_json(
        '{"spec": "x"}',
        str(tmp_path / "x.PrjPcb"),
        bridge=bridge,
    )
    assert result.ok is False
    assert bridge.calls == []


def test_execute_plan_from_json_round_trip(tmp_path: Path) -> None:
    bridge = _FakeBridge()
    blob = _two_part_plan().model_dump_json()
    result = execute_plan_from_json(blob, str(tmp_path / "x.PrjPcb"), bridge=bridge)
    assert result.ok is True
    assert len(result.placed) == 2


def test_executor_result_serializes_to_dict(tmp_path: Path) -> None:
    bridge = _FakeBridge()
    result = execute_plan(_two_part_plan(), str(tmp_path / "x.PrjPcb"), bridge=bridge)
    blob = result.to_dict()
    assert blob["ok"] is True
    assert isinstance(blob["placed"], list)
    assert isinstance(blob["failures"], list)


def test_executor_create_project_failure_short_circuits(tmp_path: Path) -> None:
    bridge = _FakeBridge(fail_commands={"project.create"})
    result = execute_plan(
        _two_part_plan(),
        str(tmp_path / "x.PrjPcb"),
        bridge=bridge,
    )
    assert result.ok is False
    # Should NOT proceed to create_document or place after project failure
    cmds = [c for c, _ in bridge.calls]
    assert "application.create_document" not in cmds
    assert "generic.place_sch_component_from_library" not in cmds


# ---------------------------------------------------------------------------
# Wiring stage (Slice B.2)
# ---------------------------------------------------------------------------


def test_executor_drops_net_labels_at_pin_coords(tmp_path: Path) -> None:
    bridge = _FakeBridge(
        pin_layouts={
            "R1": [
                {"pin_number": "1", "pin_name": "1", "x_mils": 1500, "y_mils": 6500},
                {"pin_number": "2", "pin_name": "2", "x_mils": 1500, "y_mils": 6300},
            ],
            "D1": [
                {"pin_number": "A", "pin_name": "A", "x_mils": 2500, "y_mils": 6500},
                {"pin_number": "K", "pin_name": "K", "x_mils": 2500, "y_mils": 6300},
            ],
        },
    )
    result = execute_plan(_two_part_plan(), str(tmp_path / "x.PrjPcb"), bridge=bridge)

    assert result.ok is True
    # V5 is is_power -> two power ports; LED_K is plain -> two net labels
    power_calls = [
        params for cmd, params in bridge.calls if cmd == "generic.place_power_port"
    ]
    label_calls = [
        params for cmd, params in bridge.calls if cmd == "generic.place_net_label"
    ]
    assert len(power_calls) == 2
    assert len(label_calls) == 2
    assert all(p["text"] == "V5" for p in power_calls)
    assert all(p["text"] == "LED_K" for p in label_calls)
    assert result.nets_labelled == 2
    assert result.power_ports_placed == 2


def test_executor_uses_pin_name_when_number_missing(tmp_path: Path) -> None:
    """Plan refers to pin 'A' on D1; layout has pin_number=A so it resolves."""
    bridge = _FakeBridge(
        pin_layouts={
            "R1": [
                {"pin_number": "1", "pin_name": "1", "x_mils": 1000, "y_mils": 6000},
                {"pin_number": "2", "pin_name": "2", "x_mils": 1000, "y_mils": 5800},
            ],
            "D1": [
                {"pin_number": "A", "pin_name": "Anode", "x_mils": 2000, "y_mils": 6000},
                {"pin_number": "K", "pin_name": "Cathode", "x_mils": 2000, "y_mils": 5800},
            ],
        },
    )
    result = execute_plan(_two_part_plan(), str(tmp_path / "x.PrjPcb"), bridge=bridge)
    assert result.ok is True
    assert all(f.code != "PIN_NOT_FOUND" for f in result.failures)


def test_executor_caches_pin_lookup_per_refdes(tmp_path: Path) -> None:
    """A part referenced by multiple nets should only trigger one lookup."""
    bridge = _FakeBridge()
    result = execute_plan(_two_part_plan(), str(tmp_path / "x.PrjPcb"), bridge=bridge)
    assert result.ok is True

    pin_lookups = [
        params.get("designator")
        for cmd, params in bridge.calls
        if cmd == "generic.get_sch_component_pins"
    ]
    # R1 is in V5 + LED_K, D1 is in V5 + LED_K. Cache should keep each
    # designator to a single lookup.
    from collections import Counter
    counts = Counter(pin_lookups)
    assert counts == {"R1": 1, "D1": 1}


def test_executor_reports_pin_not_found(tmp_path: Path) -> None:
    """If the plan references a pin that's not on the placed component."""
    bridge = _FakeBridge(
        pin_layouts={
            "R1": [
                {"pin_number": "1", "pin_name": "1", "x_mils": 0, "y_mils": 0},
                {"pin_number": "2", "pin_name": "2", "x_mils": 0, "y_mils": 0},
            ],
            "D1": [
                {"pin_number": "1", "pin_name": "1", "x_mils": 0, "y_mils": 0},
                {"pin_number": "2", "pin_name": "2", "x_mils": 0, "y_mils": 0},
            ],
        },
    )
    # plan references D1 pin "A" / "K" but we only have "1"/"2"
    result = execute_plan(_two_part_plan(), str(tmp_path / "x.PrjPcb"), bridge=bridge)
    assert result.ok is False
    pin_failures = [f for f in result.failures if f.code == "PIN_NOT_FOUND"]
    assert len(pin_failures) >= 2
    assert any("D1" in f.refdes for f in pin_failures)


def test_executor_handles_pin_lookup_failure(tmp_path: Path) -> None:
    bridge = _FakeBridge(fail_commands={"generic.get_sch_component_pins"})
    result = execute_plan(_two_part_plan(), str(tmp_path / "x.PrjPcb"), bridge=bridge)
    assert result.ok is False
    assert any(f.code == "PIN_LOOKUP_FAILED" for f in result.failures)


def test_executor_ground_uses_gnd_glyph(tmp_path: Path) -> None:
    plan = DesignPlan(
        spec="ground only",
        summary="ground sanity",
        sheets=[Sheet(name="main")],
        parts=[
            Part(refdes="R1", lib_ref="RES", sheet="main"),
            Part(refdes="R2", lib_ref="RES", sheet="main"),
        ],
        nets=[
            Net(
                name="GND",
                is_ground=True,
                pins=[PinRef(refdes="R1", pin="1"), PinRef(refdes="R2", pin="1")],
            ),
            Net(
                name="OUT",
                pins=[PinRef(refdes="R1", pin="2"), PinRef(refdes="R2", pin="2")],
            ),
        ],
    )
    bridge = _FakeBridge()
    result = execute_plan(plan, str(tmp_path / "x.PrjPcb"), bridge=bridge)
    assert result.ok is True
    power_calls = [
        params for cmd, params in bridge.calls if cmd == "generic.place_power_port"
    ]
    assert len(power_calls) == 2
    assert all("gnd" in p["style"] for p in power_calls)


def test_executor_agnd_picks_signal_ground(tmp_path: Path) -> None:
    plan = DesignPlan(
        spec="analog ground",
        summary="analog ground",
        sheets=[Sheet(name="main")],
        parts=[
            Part(refdes="R1", lib_ref="RES", sheet="main"),
            Part(refdes="R2", lib_ref="RES", sheet="main"),
        ],
        nets=[
            Net(
                name="AGND",
                is_ground=True,
                pins=[PinRef(refdes="R1", pin="1"), PinRef(refdes="R2", pin="1")],
            ),
            Net(
                name="N1",
                pins=[PinRef(refdes="R1", pin="2"), PinRef(refdes="R2", pin="2")],
            ),
        ],
    )
    bridge = _FakeBridge()
    execute_plan(plan, str(tmp_path / "x.PrjPcb"), bridge=bridge)
    power_calls = [
        params for cmd, params in bridge.calls if cmd == "generic.place_power_port"
    ]
    assert all(p["style"] == "gnd_signal" for p in power_calls)

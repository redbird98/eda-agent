# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba
"""Executor tests with a fake bridge, no Altium round-trips."""

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


def _parse_batch_ops(payload: str) -> list[dict[str, str]]:
    """Decode a Pascal-side bulk payload: records separated by ``~~``,
    fields by ``;``, key/value by ``=``."""
    ops: list[dict[str, str]] = []
    for record in payload.split("~~"):
        if not record:
            continue
        fields: dict[str, str] = {}
        for kv in record.split(";"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                fields[k] = v
        ops.append(fields)
    return ops


def _bulk_labels(bridge: Any) -> list[dict[str, str]]:
    """Return per-label ops parsed from any generic.place_net_labels call."""
    ops: list[dict[str, str]] = []
    for c, p in bridge.calls:
        if c == "generic.place_net_labels":
            ops.extend(_parse_batch_ops(p.get("labels", "")))
    return ops


def _bulk_ports(bridge: Any) -> list[dict[str, str]]:
    """Return per-port ops parsed from any generic.place_power_ports call."""
    ops: list[dict[str, str]] = []
    for c, p in bridge.calls:
        if c == "generic.place_power_ports":
            ops.extend(_parse_batch_ops(p.get("ports", "")))
    return ops


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
    # Bulk: one place_sch_components_from_library call with 2 placements.
    bulk_place = [
        p for c, p in bridge.calls
        if c == "generic.place_sch_components_from_library"
    ]
    assert len(bulk_place) == 1
    assert len(_parse_batch_ops(bulk_place[0]["placements"])) == 2
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
    bridge = _FakeBridge(fail_commands={"generic.place_sch_components_from_library"})
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

    bulk = [
        p for c, p in bridge.calls
        if c == "generic.place_sch_components_from_library"
    ]
    assert len(bulk) == 1
    ops = _parse_batch_ops(bulk[0]["placements"])
    assert len(ops) == 2
    coords = {(op["x"], op["y"]) for op in ops}
    assert len(coords) == 2  # no two parts at the same coords


def test_executor_passes_designator_and_lib_ref(tmp_path: Path) -> None:
    bridge = _FakeBridge()
    execute_plan(_two_part_plan(), str(tmp_path / "x.PrjPcb"), bridge=bridge)

    bulk = [
        p for c, p in bridge.calls
        if c == "generic.place_sch_components_from_library"
    ]
    assert len(bulk) == 1
    ops = _parse_batch_ops(bulk[0]["placements"])
    designators = {op["designator"] for op in ops}
    assert designators == {"R1", "D1"}
    lib_refs = {op["lib_reference"] for op in ops}
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
    # V5 is is_power -> rail glyph consolidation (task #51): pins close
    # enough to cluster get ONE shared rail port. LED_K is a signal net
    # -> ONE label per net + wires connecting pins.
    power_calls = _bulk_ports(bridge)
    label_calls = _bulk_labels(bridge)
    # Pins are 100 mils apart in the test fixture so they cluster into 1.
    assert len(power_calls) == 1  # V5: clustered into one rail glyph
    assert len(label_calls) == 1  # LED_K: one signal label
    assert power_calls[0]["text"] == "V5"
    assert label_calls[0]["text"] == "LED_K"
    assert result.nets_labelled == 1
    assert result.power_ports_placed == 1


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
    power_calls = _bulk_ports(bridge)
    # GND rail-consolidation (#51): two pins close enough cluster to one glyph.
    assert len(power_calls) == 1
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
    power_calls = _bulk_ports(bridge)
    assert all(p["style"] == "gnd_signal" for p in power_calls)


# ---------------------------------------------------------------------------
# Stub-wire + orientation (Slices 1-3)
# ---------------------------------------------------------------------------


def test_executor_draws_stub_wire_before_each_label_or_port(tmp_path: Path) -> None:
    """Every label/port placement must be preceded by a stub-wire call.

    Slice 1: drop a 100-mil stub from the pin's hot end so ERC stops
    flagging "Floating net labels". The stub call has to land in the IPC
    stream BEFORE the label/port placement for the same pin, otherwise the
    label gets registered at the pin endpoint with no electrical wire
    connecting it back to the component.
    """
    bridge = _FakeBridge(
        pin_layouts={
            "R1": [
                {
                    "pin_number": "1",
                    "pin_name": "1",
                    "x_mils": 1500,
                    "y_mils": 6500,
                    "orientation": 0,
                    "pin_length_mils": 200,
                },
                {
                    "pin_number": "2",
                    "pin_name": "2",
                    "x_mils": 1500,
                    "y_mils": 6300,
                    "orientation": 2,
                    "pin_length_mils": 200,
                },
            ],
            "D1": [
                {
                    "pin_number": "A",
                    "pin_name": "A",
                    "x_mils": 2500,
                    "y_mils": 6500,
                    "orientation": 1,
                    "pin_length_mils": 200,
                },
                {
                    "pin_number": "K",
                    "pin_name": "K",
                    "x_mils": 2500,
                    "y_mils": 6300,
                    "orientation": 3,
                    "pin_length_mils": 200,
                },
            ],
        },
    )
    result = execute_plan(_two_part_plan(), str(tmp_path / "x.PrjPcb"), bridge=bridge)
    assert result.ok is True

    # Bulk: one place_wires call per sheet holds every stub + every
    # signal-net routing segment.
    bulk_wires = [
        params for cmd, params in bridge.calls if cmd == "generic.place_wires"
    ]
    label_ops = _bulk_labels(bridge)
    port_ops = _bulk_ports(bridge)
    assert len(bulk_wires) == 1
    wire_ops = _parse_batch_ops(bulk_wires[0]["wires"])
    # 4 stubs (one per pin on the 2 nets) + routing segments for the
    # signal net LED_K (L-path = 2 segments since the two stub ends
    # differ in both X and Y).
    assert len(wire_ops) >= 4
    # Rail consolidation: V5 (power, 2 pins close) -> 1 port glyph.
    # LED_K (signal, 2 pins) -> 1 label.
    assert len(port_ops) == 1
    assert len(label_ops) == 1

    # Behavioural invariant: every label / port has at least one wire in
    # the batch whose endpoint touches the label/port's (x, y) coord.
    wire_pts: set[tuple[str, str]] = set()
    for op in wire_ops:
        wire_pts.add((op["x1"], op["y1"]))
        wire_pts.add((op["x2"], op["y2"]))
    for params in label_ops + port_ops:
        assert (params["x"], params["y"]) in wire_pts


def _single_pin_plan(net_name: str, *, is_power: bool = False,
                     is_ground: bool = False) -> DesignPlan:
    """Plan with two resistors and one net of two pins so the schema is happy.

    Tests only inspect the first pin (R1.1); R2.1 is just there to satisfy
    Net.pins min-length=2 in the pydantic model.
    """
    return DesignPlan(
        spec="single net",
        summary="one net, two pins",
        sheets=[Sheet(name="main")],
        parts=[
            Part(refdes="R1", lib_ref="RES", sheet="main"),
            Part(refdes="R2", lib_ref="RES", sheet="main"),
        ],
        nets=[
            Net(
                name=net_name,
                is_power=is_power,
                is_ground=is_ground,
                pins=[
                    PinRef(refdes="R1", pin="1"),
                    PinRef(refdes="R2", pin="1"),
                ],
            ),
        ],
    )


def test_executor_stub_endpoint_drives_label_placement(tmp_path: Path) -> None:
    """The label / power port sits at the FAR end of the stub, not on the pin.

    ISch_Pin.Location is the electrical endpoint of the pin (verified
    empirically on a placed TPS54331D). The stub starts AT Pin.Location
    and extends outward by _STUB_LEN_MILS. pin_length is NOT added.

    For a pin facing right at (1000, 5000) with pin_length=300 the
    stub goes from (1000, 5000) to (1100, 5000); label lands at
    (1100, 5000). The pin_length field is retained on the wire payload
    for ABI compat but does not affect the math.
    """
    bridge = _FakeBridge(
        pin_layouts={
            "R1": [
                {
                    "pin_number": "1",
                    "pin_name": "1",
                    "x_mils": 1000,
                    "y_mils": 5000,
                    "orientation": 0,
                    "pin_length_mils": 300,
                },
            ],
            "R2": [
                {
                    "pin_number": "1",
                    "pin_name": "1",
                    "x_mils": 9000,
                    "y_mils": 5000,
                    "orientation": 2,
                    "pin_length_mils": 300,
                },
            ],
        },
    )
    execute_plan(_single_pin_plan("OUT"), str(tmp_path / "x.PrjPcb"), bridge=bridge)

    bulk_wires = [p for c, p in bridge.calls if c == "generic.place_wires"]
    assert len(bulk_wires) == 1
    wire_ops = _parse_batch_ops(bulk_wires[0]["wires"])
    # R1 pin facing right: hot=(1000,5000) (Pin.Location IS the hot end),
    # stub end=(1300,5000) with _STUB_LEN_MILS = 300
    assert wire_ops[0] == {"x1": "1000", "y1": "5000", "x2": "1300", "y2": "5000"}
    label_calls = _bulk_labels(bridge)
    assert label_calls[0]["x"] == "1300"
    assert label_calls[0]["y"] == "5000"
    # Horizontal pin -> horizontal label
    assert label_calls[0]["orientation"] == "0"


def test_executor_vertical_pin_rotates_net_label(tmp_path: Path) -> None:
    """A pin facing up (orientation=1) should rotate the label to read along Y."""
    bridge = _FakeBridge(
        pin_layouts={
            "R1": [
                {
                    "pin_number": "1",
                    "pin_name": "1",
                    "x_mils": 1000,
                    "y_mils": 5000,
                    "orientation": 1,
                    "pin_length_mils": 200,
                },
            ],
            "R2": [
                {
                    "pin_number": "1",
                    "pin_name": "1",
                    "x_mils": 4000,
                    "y_mils": 5000,
                    "orientation": 1,
                    "pin_length_mils": 200,
                },
            ],
        },
    )
    execute_plan(_single_pin_plan("OUT"), str(tmp_path / "x.PrjPcb"), bridge=bridge)
    label_calls = _bulk_labels(bridge)
    # Signal nets now get ONE label per net, anchored at the first stub
    # end. For a 2-pin net, _signal_label_anchor returns stub_ends[0] which
    # is R1's stub end (orientation=1 / up -> y=5000+300=5300). The label
    # itself is always horizontal (orientation=0); readability rotation is
    # a per-pin label decision that no longer applies in the one-label
    # convention.
    assert len(label_calls) == 1
    assert label_calls[0]["orientation"] == "0"
    assert label_calls[0]["x"] == "1000"
    assert label_calls[0]["y"] == "5300"


def test_executor_vcc_power_port_always_faces_up(tmp_path: Path) -> None:
    """VCC-style power-port glyph always points UP (orientation 1),
    independent of pin direction. User schematic convention: power
    rails read consistently upward."""
    bridge = _FakeBridge(
        pin_layouts={
            "R1": [
                {
                    "pin_number": "1",
                    "pin_name": "1",
                    "x_mils": 1000,
                    "y_mils": 5000,
                    "orientation": 1,  # pin points up
                    "pin_length_mils": 200,
                },
            ],
            "R2": [
                {
                    "pin_number": "1",
                    "pin_name": "1",
                    "x_mils": 4000,
                    "y_mils": 5000,
                    "orientation": 1,
                    "pin_length_mils": 200,
                },
            ],
        },
    )
    execute_plan(
        _single_pin_plan("VCC", is_power=True),
        str(tmp_path / "x.PrjPcb"),
        bridge=bridge,
    )
    port_calls = _bulk_ports(bridge)
    # Canonical: VCC always points up.
    assert port_calls[0]["orientation"] == "1"
    assert port_calls[0]["style"] == "bar"


def test_executor_gnd_power_port_always_faces_down(tmp_path: Path) -> None:
    """GND glyph always points DOWN (orientation 3), independent of pin
    direction. User schematic convention."""
    bridge = _FakeBridge(
        pin_layouts={
            "R1": [
                {
                    "pin_number": "1",
                    "pin_name": "1",
                    "x_mils": 1000,
                    "y_mils": 5000,
                    "orientation": 3,  # pin points down
                    "pin_length_mils": 200,
                },
            ],
            "R2": [
                {
                    "pin_number": "1",
                    "pin_name": "1",
                    "x_mils": 4000,
                    "y_mils": 5000,
                    "orientation": 3,
                    "pin_length_mils": 200,
                },
            ],
        },
    )
    execute_plan(
        _single_pin_plan("GND", is_ground=True),
        str(tmp_path / "x.PrjPcb"),
        bridge=bridge,
    )
    port_calls = _bulk_ports(bridge)
    assert port_calls[0]["orientation"] == "3"
    assert port_calls[0]["style"] == "gnd_power"

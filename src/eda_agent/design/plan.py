# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba
"""Design plan schema — the contract between planner and executor.

A DesignPlan is the structured output of the planner. The executor reads it
and instantiates each Part on a SchDoc, drops a net label at every PinRef,
and adds power ports for the Nets flagged as power. The schema is strict
(extra='forbid') so the planner can't smuggle ambiguity past validation.

Coordinates are millimetres relative to a Sheet's origin. The executor maps
them to Altium internal units via the existing MilsToCoord / mm_to_coord
helpers.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


_REFDES_PATTERN = r"^[A-Z]+[0-9]+[A-Z]?$"
_NET_PATTERN = r"^[A-Za-z_][A-Za-z0-9_+\-/]*$"


class PartStatus(str, Enum):
    """Whether the planner found this part in the user's libraries."""

    EXISTING = "existing"           # lib_ref resolves in the user's libs
    NEEDS_CREATION = "needs_creation"  # planner picked a part not in libs


class PinRef(BaseModel):
    """One end of a net — a refdes + pin identifier."""

    model_config = ConfigDict(extra="forbid")

    refdes: str = Field(pattern=_REFDES_PATTERN)
    pin: str = Field(min_length=1, description="Pin number or pin name")


class Part(BaseModel):
    """A component the executor will place on a sheet."""

    model_config = ConfigDict(extra="forbid")

    refdes: str = Field(pattern=_REFDES_PATTERN)
    lib_ref: str = Field(min_length=1, description="Library reference (symbol name)")
    lib_path: Optional[str] = Field(
        default=None,
        description="Absolute path to the SchLib. Optional — executor falls "
        "back to project-installed libs when omitted.",
    )
    value: Optional[str] = Field(
        default=None,
        description="Component value (e.g. '10k', '100nF', 'LM7805'). For "
        "actives this often equals lib_ref; for passives it carries the value.",
    )
    footprint: Optional[str] = Field(
        default=None,
        description="Footprint name. The executor only stamps it when set; "
        "leave None to defer to the symbol's default.",
    )
    status: PartStatus = PartStatus.EXISTING
    sheet: str = Field(default="main", description="Sheet name (matches Sheet.name)")
    zone: Optional[str] = Field(
        default=None,
        description="Optional Zone.name to place the part inside.",
    )
    rationale: Optional[str] = Field(
        default=None,
        description="One-line why-this-part — for audit logs, not sent to "
        "Altium. Useful when the planner re-iterates after a validation fail.",
    )


class Net(BaseModel):
    """A logical net — connects 2+ pins via shared net labels."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_NET_PATTERN)
    pins: list[PinRef] = Field(min_length=2)
    is_power: bool = Field(
        default=False,
        description="If True, executor adds a power port (VCC-style) instead "
        "of a plain net label.",
    )
    is_ground: bool = Field(
        default=False,
        description="If True, executor adds a GND-style power port.",
    )

    @field_validator("pins")
    @classmethod
    def _unique_pin_endpoints(cls, pins: list[PinRef]) -> list[PinRef]:
        seen = set()
        for p in pins:
            key = (p.refdes, p.pin)
            if key in seen:
                raise ValueError(f"duplicate pin endpoint {p.refdes}.{p.pin}")
            seen.add(key)
        return pins


class Zone(BaseModel):
    """A rough placement region on a sheet — signal flow guidance."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    sheet: str = Field(default="main")
    origin_mm: tuple[float, float] = Field(default=(0.0, 0.0))
    size_mm: tuple[float, float] = Field(default=(40.0, 40.0))
    role: Optional[str] = Field(
        default=None,
        description="Free-text role tag — e.g. 'power_in', 'mcu', 'usb_front_end'.",
    )


class Sheet(BaseModel):
    """A schematic sheet that the executor will create or reuse."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    title: Optional[str] = Field(default=None)
    size: str = Field(default="A4", description="Altium sheet size code (A4, A3, ...)")


class DesignRuleDelta(BaseModel):
    """A design-rule override the planner wants applied to the project.

    The executor only acts on this in the PCB stage (later slice). Captured
    here so the planner can express constraints up front.
    """

    model_config = ConfigDict(extra="forbid")

    rule_kind: str = Field(min_length=1, description="e.g. 'Width', 'Clearance'")
    scope: str = Field(min_length=1, description="Net class or 'all'")
    parameters: dict[str, str] = Field(default_factory=dict)


class BomLine(BaseModel):
    """One BOM line — derived from the parts list, but planner-asserted."""

    model_config = ConfigDict(extra="forbid")

    refdes_list: list[str] = Field(min_length=1)
    manufacturer: Optional[str] = None
    mpn: Optional[str] = Field(default=None, description="Manufacturer part number")
    description: Optional[str] = None
    qty: int = Field(ge=1, default=1)


class DesignPlan(BaseModel):
    """Complete, validated plan that the executor consumes.

    Everything the planner produced. Round-trips through JSON cleanly so the
    orchestrator can persist plans for audit and replay.
    """

    model_config = ConfigDict(extra="forbid")

    spec: str = Field(min_length=1, description="The original natural-language spec")
    summary: str = Field(
        min_length=1,
        description="One-paragraph explanation of the topology choice — for "
        "the human reviewer, not the executor.",
    )
    sheets: list[Sheet] = Field(min_length=1)
    zones: list[Zone] = Field(default_factory=list)
    parts: list[Part] = Field(min_length=1)
    nets: list[Net] = Field(min_length=1)
    bom: list[BomLine] = Field(default_factory=list)
    design_rules: list[DesignRuleDelta] = Field(default_factory=list)
    open_questions: list[str] = Field(
        default_factory=list,
        description="Things the planner could not decide and wants the user "
        "to clarify. Empty list = the planner is confident.",
    )

    @field_validator("parts")
    @classmethod
    def _unique_refdes(cls, parts: list[Part]) -> list[Part]:
        seen: set[str] = set()
        for p in parts:
            if p.refdes in seen:
                raise ValueError(f"duplicate refdes {p.refdes}")
            seen.add(p.refdes)
        return parts

    @field_validator("nets")
    @classmethod
    def _unique_net_names(cls, nets: list[Net]) -> list[Net]:
        seen: set[str] = set()
        for n in nets:
            if n.name in seen:
                raise ValueError(f"duplicate net name {n.name}")
            seen.add(n.name)
        return nets

    def cross_check(self) -> list[str]:
        """Cross-validation that doesn't fit a single field validator.

        Returns a list of human-readable problems — empty list means clean.
        Called by the planner before returning a plan, and by the executor as
        a safety net.
        """
        problems: list[str] = []

        sheet_names = {s.name for s in self.sheets}
        zone_names = {z.name for z in self.zones}
        part_refdes = {p.refdes for p in self.parts}

        for p in self.parts:
            if p.sheet not in sheet_names:
                problems.append(f"part {p.refdes}.sheet={p.sheet!r} not in sheets")
            if p.zone is not None and p.zone not in zone_names:
                problems.append(f"part {p.refdes}.zone={p.zone!r} not in zones")

        for z in self.zones:
            if z.sheet not in sheet_names:
                problems.append(f"zone {z.name}.sheet={z.sheet!r} not in sheets")

        for n in self.nets:
            for pr in n.pins:
                if pr.refdes not in part_refdes:
                    problems.append(
                        f"net {n.name} references unknown refdes {pr.refdes}"
                    )

        return problems

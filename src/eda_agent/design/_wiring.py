# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Pure-Python helpers shared by executor.py and pipeline.py.

The legacy ``executor.py`` and the new canvas-based ``pipeline.py``
both need: BOM/parameter resolution, net-representation rules (port vs
wire vs label), ground-style heuristics, junction detection, and the
project-stem-namespaced sheet path. Keeping two copies risks drift.

This module is the single source of truth; both callers import from
here. The helpers are name-mangled with a leading underscore for the
legacy executor's expectations; the pipeline imports them under the
same names so a future un-mangling pass can flip both at once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from eda_agent.design.plan import DesignPlan, Net, Part


def _sheet_path(project_path: Path, sheet_name: str) -> Path:
    """Default SchDoc path for a plan-declared sheet.

    Namespaced by the project stem so two projects in the same workspace can
    both declare a sheet called 'main' without colliding on disk. Without the
    namespace, calling design.execute_plan twice with two different .PrjPcb
    paths but the same plan.sheets[].name silently writes the second design
    onto the first one's SchDoc, mixing parts from two unrelated projects.
    """
    return project_path.parent / f"{project_path.stem}__{sheet_name}.SchDoc"


def _bom_lookup(plan: DesignPlan) -> dict[str, tuple[Optional[str], Optional[str]]]:
    """Map refdes -> (manufacturer, mpn) drawn from plan.bom for fallback.

    Built once per execution so the per-part lookup is O(1). Multiple BomLines
    can reference the same refdes only if there's a planner bug; first match
    wins.
    """
    lookup: dict[str, tuple[Optional[str], Optional[str]]] = {}
    for line in plan.bom:
        for refdes in line.refdes_list:
            lookup.setdefault(refdes, (line.manufacturer, line.mpn))
    return lookup


def _part_parameters(
    part: Part,
    bom_lookup: dict[str, tuple[Optional[str], Optional[str]]],
) -> dict[str, str]:
    """Build the parameter sub-object the Pascal handler will stamp.

    Resolution order for Manufacturer / MPN: Part fields win over BomLine
    fallback. Empty values are stripped so the Pascal side has nothing to
    skip and the IPC payload stays compact.
    """
    bom_mfr, bom_mpn = bom_lookup.get(part.refdes, (None, None))
    mfr = part.manufacturer or bom_mfr
    mpn = part.mpn or bom_mpn

    candidate: dict[str, Optional[str]] = {
        "Value": part.value,
        "Manufacturer": mfr,
        "Manufacturer Part Number": mpn,
        "Footprint": part.footprint,
    }
    return {k: v for k, v in candidate.items() if v}


def _detect_junctions(
    wires: list[tuple[int, int, int, int]],
) -> list[tuple[int, int]]:
    """Return the set of points that need a junction dot.

    Two cases need a dot:

    1. **Three or more wire endpoints coincide.** Counted from the
       endpoint multiset. A T-junction adds 3 endpoints to one point
       (two wires end, one starts/passes); a 4-way junction adds 4.
    2. **A wire endpoint sits strictly between the endpoints of another
       wire's axis-aligned segment.** That's the "wire stops on
       another wire" case Altium's auto-compiler also treats as a
       junction but our scripted placement doesn't always trigger.

    Wires are ``(x1, y1, x2, y2)`` tuples; only axis-aligned segments
    (the only kind we emit) are checked.
    """
    from collections import Counter

    endpoint_counts: Counter = Counter()
    for (x1, y1, x2, y2) in wires:
        endpoint_counts[(x1, y1)] += 1
        endpoint_counts[(x2, y2)] += 1

    junctions: set[tuple[int, int]] = {
        pt for pt, c in endpoint_counts.items() if c >= 3
    }

    endpoints: set[tuple[int, int]] = set()
    for (x1, y1, x2, y2) in wires:
        endpoints.add((x1, y1))
        endpoints.add((x2, y2))

    for (x, y) in endpoints:
        for (sx1, sy1, sx2, sy2) in wires:
            if (x, y) == (sx1, sy1) or (x, y) == (sx2, sy2):
                continue
            if sx1 == sx2 and x == sx1:
                if min(sy1, sy2) < y < max(sy1, sy2):
                    junctions.add((x, y))
            elif sy1 == sy2 and y == sy1:
                if min(sx1, sx2) < x < max(sx1, sx2):
                    junctions.add((x, y))

    return sorted(junctions)


def _ground_style(net_name: str) -> str:
    """Pick a power-port style for an is_ground net based on its name.

    Altium has separate gnd_power / gnd_signal / gnd_earth glyphs. When the
    net name carries a hint we honour it; otherwise default to gnd_power.
    """
    upper = net_name.upper()
    if "EARTH" in upper or upper == "PE":
        return "gnd_earth"
    if "AGND" in upper or "ANALOG" in upper or upper == "AGND":
        return "gnd_signal"
    return "gnd_power"


def _power_port_orientation(pin_orientation: int, is_ground: bool) -> int:
    """Canonical schematic convention:

    - VCC / power rails ALWAYS point up   (orientation 1) -- bar / circle
      glyph sits above the connection point.
    - GND ALWAYS points down              (orientation 3) -- triangle /
      bar glyph hangs below the connection point.

    Independent of the pin's outward direction. The stub wire absorbs
    the horizontal-vs-vertical mismatch when the pin faces sideways:
    the port's electrical connection is always at the stub end, and
    the glyph extends UP for power or DOWN for ground from there.

    ``pin_orientation`` is unused (retained for ABI compat).
    """
    del pin_orientation  # noqa: F841 - retained for ABI compat
    return 3 if is_ground else 1


def _net_representation(
    net: Net,
    refdes_to_zone: dict[str, Optional[str]],
) -> str:
    """Pick the visual representation for a net per discipline rule 3.

    Three-tier priority:
      1. ``is_power`` or ``is_ground`` -> ``'port'``: power-port glyph at
         every pin (cluster-consolidated by the caller).
      2. ``force_label=True`` -> ``'label_per_pin'``: planner-driven
         override for an intra-block net that would tangle as a wire.
      3. All pins share one zone (functional block) -> ``'wire'``: route
         pins together with wires; no label.
      4. Otherwise (pins span multiple zones, or some pins are unzoned
         while others are in a zone) -> ``'label_per_pin'``: one net
         label at each pin, no inter-pin wires.

    A net whose pins are ALL unzoned (every component has ``zone=None``)
    falls through to ``'wire'`` because they share the implicit "no zone"
    group. This keeps current behaviour for plans that don't define
    zones yet — the executor still wires them together. Once the planner
    assigns zones, the rule kicks in.
    """
    if net.is_power or net.is_ground:
        return "port"
    if net.force_label:
        return "label_per_pin"
    zones = {refdes_to_zone.get(p.refdes) for p in net.pins}
    if len(zones) == 1:
        return "wire"
    return "label_per_pin"

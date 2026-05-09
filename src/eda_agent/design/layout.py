# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba
"""Grid-layout helper for the executor.

Converts a DesignPlan's parts + zones into Altium mil coordinates. The
goal is "components are visible and don't overlap"; aesthetic placement
is not a goal of the autonomous flow, manual cleanup happens after.
"""

from __future__ import annotations

from dataclasses import dataclass

from eda_agent.design.plan import DesignPlan, Part, Zone


# Default grid spacing in mils. 2000 mils gives clear visual separation on
# an A4 sheet. Origin (5000, 4000) lands the first part roughly at the
# centre of an A4 landscape sheet so parts are immediately visible.
GRID_PITCH_X_MILS = 2000
GRID_PITCH_Y_MILS = 2000

DEFAULT_ORIGIN_X_MILS = 5000
DEFAULT_ORIGIN_Y_MILS = 4000

# How many parts per row before wrapping to the next row.
DEFAULT_COLS_PER_ROW = 6


@dataclass(frozen=True)
class PlacedPart:
    """Computed placement for one part."""

    refdes: str
    sheet: str
    x_mils: int
    y_mils: int
    rotation: int = 0


def _zone_origin_mils(zone: Zone) -> tuple[int, int]:
    """Convert a Zone's mm origin into mils."""
    return (
        int(round(zone.origin_mm[0] / 0.0254)),
        int(round(zone.origin_mm[1] / 0.0254)),
    )


def compute_layout(plan: DesignPlan) -> list[PlacedPart]:
    """Compute (x, y) for every part in the plan.

    Parts grouped by (sheet, zone). Each group lays out left-to-right,
    top-to-bottom in a fixed grid; wrap after DEFAULT_COLS_PER_ROW parts.
    No-zone parts share an "_unzoned" group per sheet.
    """
    placed: list[PlacedPart] = []

    zones_by_name: dict[tuple[str, str], Zone] = {
        (z.sheet, z.name): z for z in plan.zones
    }

    grouped: dict[tuple[str, str], list[Part]] = {}
    for p in plan.parts:
        key = (p.sheet, p.zone or "_unzoned")
        grouped.setdefault(key, []).append(p)

    # Stable order: by sheet, then zone, then refdes.
    for (sheet, zone_name) in sorted(grouped.keys()):
        parts_in_zone = sorted(grouped[(sheet, zone_name)], key=lambda p: p.refdes)

        zone = zones_by_name.get((sheet, zone_name))
        if zone is not None:
            origin_x, origin_y = _zone_origin_mils(zone)
            origin_x += DEFAULT_ORIGIN_X_MILS
            origin_y += DEFAULT_ORIGIN_Y_MILS
        else:
            origin_x = DEFAULT_ORIGIN_X_MILS
            origin_y = DEFAULT_ORIGIN_Y_MILS

        for idx, part in enumerate(parts_in_zone):
            col = idx % DEFAULT_COLS_PER_ROW
            row = idx // DEFAULT_COLS_PER_ROW
            x = origin_x + col * GRID_PITCH_X_MILS
            # Y grows downward in Altium-on-screen but coordinates grow
            # upward, subtract row * pitch so successive rows step down.
            y = origin_y - row * GRID_PITCH_Y_MILS
            placed.append(
                PlacedPart(
                    refdes=part.refdes,
                    sheet=part.sheet,
                    x_mils=x,
                    y_mils=y,
                )
            )

    return placed

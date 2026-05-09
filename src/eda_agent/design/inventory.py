# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba
"""Library inventory snapshot, what the planner sees as 'available parts'.

The planner reads this to bias its part choices toward libs the user
already has. NDA scope: the snapshot only includes paths the caller
explicitly hands in (the user's own neutral standard libraries). It does
not crawl the current project's local libs, those may be client-specific
and re-using their content across engagements would breach NDA.

Two modes:

* ``snapshot_live(...)``, opens each SchLib in Altium and queries the
  bridge. Use during a real session.
* ``LibraryInventory.from_json_file(...)``, loads a previously saved
  snapshot. Lets the planner iterate offline without an Altium round-trip.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("eda_agent.design.inventory")


class ComponentSummary(BaseModel):
    """One component row the planner sees."""

    model_config = ConfigDict(extra="ignore")

    lib_ref: str = Field(min_length=1)
    designator_prefix: Optional[str] = None
    description: Optional[str] = None
    pin_count: Optional[int] = None
    footprint: Optional[str] = None
    parameters: dict[str, str] = Field(default_factory=dict)


class LibrarySummary(BaseModel):
    """One library row, path + components."""

    model_config = ConfigDict(extra="ignore")

    path: str = Field(min_length=1)
    components: list[ComponentSummary] = Field(default_factory=list)


class LibraryInventory(BaseModel):
    """The planner-visible inventory: a flat list of libraries."""

    model_config = ConfigDict(extra="forbid")

    libraries: list[LibrarySummary] = Field(default_factory=list)

    def total_components(self) -> int:
        return sum(len(lib.components) for lib in self.libraries)

    def find(self, lib_ref: str) -> Optional[tuple[str, ComponentSummary]]:
        """Look up a component by lib_ref. Returns (lib_path, summary) or None."""
        for lib in self.libraries:
            for comp in lib.components:
                if comp.lib_ref == lib_ref:
                    return lib.path, comp
        return None

    @classmethod
    def from_json_file(cls, path: Path) -> "LibraryInventory":
        with open(path, "r", encoding="utf-8") as f:
            return cls.model_validate(json.load(f))

    def to_json_file(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.model_dump_json(indent=2))


def _component_summary_from_raw(row: dict) -> ComponentSummary:
    """Map a raw library.get_components row into a ComponentSummary.

    The bridge response shape is permissive, different Altium builds /
    component types use different field names. We try the common ones.
    """

    def _get(*keys: str) -> Optional[str]:
        for k in keys:
            v = row.get(k)
            if v not in (None, ""):
                return v if isinstance(v, str) else str(v)
        return None

    pin_count: Optional[int] = None
    raw_pin_count = row.get("pin_count") or row.get("PinCount") or row.get("pins")
    if isinstance(raw_pin_count, int):
        pin_count = raw_pin_count
    elif isinstance(raw_pin_count, str) and raw_pin_count.isdigit():
        pin_count = int(raw_pin_count)
    elif isinstance(raw_pin_count, list):
        pin_count = len(raw_pin_count)

    return ComponentSummary(
        lib_ref=_get("name", "lib_ref", "Name", "ComponentName") or "",
        designator_prefix=_get("designator_prefix", "Designator", "DesignatorPrefix"),
        description=_get("description", "Description"),
        pin_count=pin_count,
        footprint=_get("footprint", "Footprint", "DefaultFootprint"),
    )


def snapshot_live(library_paths: list[Path]) -> LibraryInventory:
    """Pull a component summary for each SchLib path.

    The Pascal handler reads the SchLib from disk via
    ``SchServer.CreateLibCompInfoReader(library_path)``, so the lib does
    not need to be open in Altium first.

    The caller controls scope by handing in an explicit path list. We never
    crawl. Project-local libs stay out of the snapshot unless the caller
    asks for them by hand, which is the right boundary for NDA isolation.
    """
    from eda_agent.bridge import get_bridge  # late import, bridge needs Altium running

    bridge = get_bridge()
    libs: list[LibrarySummary] = []

    for raw_path in library_paths:
        lib_path = Path(raw_path).expanduser().resolve()
        if not lib_path.exists():
            logger.warning("library path missing: %s", lib_path)
            libs.append(LibrarySummary(path=str(lib_path)))
            continue

        try:
            raw = bridge.send_command(
                "library.get_components", {"library_path": str(lib_path)}
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("library.get_components failed for %s: %s", lib_path, exc)
            libs.append(LibrarySummary(path=str(lib_path)))
            continue

        rows = []
        if isinstance(raw, dict):
            rows = raw.get("components") or raw.get("results") or []
        elif isinstance(raw, list):
            rows = raw

        components = [_component_summary_from_raw(r) for r in rows if isinstance(r, dict)]
        components = [c for c in components if c.lib_ref]
        libs.append(LibrarySummary(path=str(lib_path), components=components))

    return LibraryInventory(libraries=libs)

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""EDA-agnostic engineering calculators.

Trace-width, impedance, termination, length-match and thermal-via sizing are
pure physics (IPC-2221/2141, transmission-line theory) with no dependency on any
EDA tool. The Altium backend already exposes these as ``pcb_calc_*``; this module
gives the KiCad backend the same tools, with the same names and the same
underlying functions, so the two backends are calculator-equivalent.

Offline family: ``{"ok": True, ...}`` / ``{"ok": False, "reason": ...}``.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Optional


def _dictify(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    return {"result": obj}


def _ok(obj: Any) -> dict[str, Any]:
    return {"ok": True, **_dictify(obj)}


def _fail(e: Exception) -> dict[str, Any]:
    return {"ok": False, "reason": str(e)}


def register_calc_tools(mcp) -> None:

    @mcp.tool()
    async def pcb_calc_trace_width_for_impedance(
            target_ohms: float, geometry: str, dielectric_height_mils: float,
            dielectric_constant: float = 4.2, copper_oz: float = 1.0,
            spacing_mils: float = 0.0) -> dict[str, Any]:
        """Trace width (mils) to hit a target impedance for a geometry
        (microstrip / stripline / their *_diff variants). Differential targets
        need spacing_mils. Inverse of the IPC-2141 impedance formula."""
        from ..design.impedance_sizing import trace_width_for_impedance
        try:
            return _ok(trace_width_for_impedance(
                target_ohms, geometry, dielectric_height_mils,
                dielectric_constant=dielectric_constant, copper_oz=copper_oz,
                spacing_mils=spacing_mils))
        except Exception as e:
            return _fail(e)

    @mcp.tool()
    async def pcb_calc_termination(
            length_mils: float, rise_time_ns: float, z0: float, er: float,
            geometry: str = "microstrip",
            driver_impedance: Optional[float] = None,
            vcc: Optional[float] = None, width_mils: Optional[float] = None,
            height_mils: Optional[float] = None,
            multi_load: bool = False) -> dict[str, Any]:
        """Assess whether a net is electrically long for its edge rate and, if
        so, recommend a termination (series for point-to-point, Thevenin /
        parallel for a multi-load bus) with E24 values."""
        from ..design.signal_integrity import recommend_termination
        kw: dict[str, Any] = {"geometry": geometry, "multi_load": multi_load}
        for k, v in (("driver_impedance", driver_impedance), ("vcc", vcc),
                     ("width_mils", width_mils), ("height_mils", height_mils)):
            if v is not None:
                kw[k] = v
        try:
            return _ok(recommend_termination(length_mils, rise_time_ns, z0,
                                             er, **kw))
        except Exception as e:
            return _fail(e)

    @mcp.tool()
    async def pcb_calc_length_match(
            dielectric_constant: float = 4.2, geometry: str = "stripline",
            width_mils: Optional[float] = None,
            dielectric_height_mils: Optional[float] = None,
            skew_budget_ps: Optional[float] = None,
            rise_time_ns: Optional[float] = None,
            match_fraction: float = 0.1,
            lengths: Optional[dict[str, float]] = None) -> dict[str, Any]:
        """Resolve the effective Er and a match tolerance from a skew budget (or
        match_fraction * rise_time_ns), then optionally report a group of net
        lengths against it."""
        from ..design.length_matching import assess_length_match
        try:
            return _ok(assess_length_match(
                dielectric_constant=dielectric_constant, geometry=geometry,
                width_mils=width_mils,
                dielectric_height_mils=dielectric_height_mils,
                skew_budget_ps=skew_budget_ps, rise_time_ns=rise_time_ns,
                match_fraction=match_fraction, lengths=lengths))
        except Exception as e:
            return _fail(e)

    @mcp.tool()
    async def pcb_calc_thermal_vias(
            drill_mm: float, plating_um: float, length_mm: float,
            filled_copper: bool = False, power_w: Optional[float] = None,
            delta_t_c: Optional[float] = None,
            target_k_per_w: Optional[float] = None,
            via_count: Optional[int] = None) -> dict[str, Any]:
        """Size a thermal-via field for a target thermal resistance (given
        directly, or as power_w with a delta_t_c budget), or score an existing
        via_count. Fourier conduction, R = L/(k*A)."""
        from ..design.thermal_vias import assess_thermal_vias
        kw: dict[str, Any] = {"filled_copper": filled_copper}
        for k, v in (("power_w", power_w), ("delta_t_c", delta_t_c),
                     ("target_k_per_w", target_k_per_w),
                     ("via_count", via_count)):
            if v is not None:
                kw[k] = v
        try:
            return _ok(assess_thermal_vias(drill_mm, plating_um, length_mm, **kw))
        except Exception as e:
            return _fail(e)

    @mcp.tool()
    async def pcb_calc_trace_width_for_current(
            current_a: float, copper_oz: float = 1.0, delta_t_c: float = 10.0,
            layer: str = "external", margin: float = 0.2,
            length_mils: float = 0.0) -> dict[str, Any]:
        """Minimum track width (mils) to carry a current at a temperature rise
        (IPC-2221), widened by ``margin`` and snapped to a 0.1 mil grid."""
        from ..design.trace_sizing import trace_width_for_current
        try:
            return _ok(trace_width_for_current(
                current_a, copper_oz=copper_oz, delta_t_c=delta_t_c,
                layer=layer, margin=margin, length_mils=length_mils))
        except Exception as e:
            return _fail(e)

    @mcp.tool()
    async def pcb_calc_track_current_capacity(
            width_mils: float, copper_oz: float = 1.0,
            delta_t_c: float = 10.0, layer: str = "external") -> dict[str, Any]:
        """Forward IPC-2221: the current a track of this width sustains at a
        given temperature rise."""
        from ..design.trace_sizing import current_capacity_amps
        try:
            amps = current_capacity_amps(width_mils, copper_oz=copper_oz,
                                         delta_t_c=delta_t_c, layer=layer)
            return {"ok": True, "current_a": round(amps, 4),
                    "width_mils": width_mils, "copper_oz": copper_oz,
                    "delta_t_c": delta_t_c, "layer": layer}
        except Exception as e:
            return _fail(e)

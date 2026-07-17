# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Backend adapters: fill a normalized snapshot from a live EDA tool.

Each adapter turns one EDA's live API into the same :class:`DesignSnapshot`.
The neutral tools call :func:`resolve_backend` to pick the adapter matching the
active backend (or an explicit override) and then ``await backend.snapshot()``.
Adding another EDA is a matter of writing one more adapter here.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from .snapshot import DesignSnapshot


class BackendUnavailableError(RuntimeError):
    """The requested backend's tool is not reachable (not running, API off)."""


def active_backend_name() -> str:
    """The backend the server started under (same resolution as the server)."""
    return (os.environ.get("EDA_AGENT_BACKEND", "altium") or "altium").strip().lower()


class KiCadBackend:
    name = "kicad"

    async def health(self) -> dict[str, Any]:
        from ..bridge.kicad_bridge import get_kicad_bridge, KiCadNotReachableError
        try:
            return get_kicad_bridge().ping()
        except KiCadNotReachableError as e:
            raise BackendUnavailableError(str(e)) from None

    async def snapshot(self) -> DesignSnapshot:
        from ..bridge.kicad_bridge import get_kicad_bridge, KiCadNotReachableError
        br = get_kicad_bridge()
        try:
            parts, pins, unconnected = br.component_pins()
            stats = br.board_stats()
        except KiCadNotReachableError as e:
            raise BackendUnavailableError(str(e)) from None
        return DesignSnapshot.build(
            "kicad", parts, pins,
            board_name=str(stats.get("name") or ""),
            unconnected_pad_count=unconnected,
            raw_stats={k: v for k, v in stats.items()
                       if k in ("tracks", "vias", "zones", "stackup_layers",
                                "footprints", "pads")},
        )

    async def run_drc(self) -> dict[str, Any]:
        from ..bridge.kicad_bridge import get_kicad_bridge, KiCadNotReachableError
        from .kicad_drc import run_kicad_cli_drc
        br = get_kicad_bridge()
        try:
            cli = br.kicad_cli_path()
            board = br.board_file_path()
        except KiCadNotReachableError as e:
            raise BackendUnavailableError(str(e)) from None
        return await run_kicad_cli_drc(cli, board)

    async def run_erc(self) -> dict[str, Any]:
        from ..bridge.kicad_bridge import get_kicad_bridge, KiCadNotReachableError
        from .kicad_drc import run_kicad_cli_erc
        br = get_kicad_bridge()
        try:
            cli = br.kicad_cli_path()
            sch = br.sch_file_path()
        except KiCadNotReachableError as e:
            raise BackendUnavailableError(str(e)) from None
        return await run_kicad_cli_erc(cli, sch)


class AltiumBackend:
    name = "altium"

    async def _bridge(self):
        from ..bridge import get_bridge
        from ..bridge.exceptions import AltiumError
        bridge = get_bridge()
        return bridge, AltiumError

    async def health(self) -> dict[str, Any]:
        bridge, AltiumError = await self._bridge()
        try:
            return await bridge.send_command_async("application.ping", {})
        except AltiumError as e:
            raise BackendUnavailableError(str(e)) from None

    async def snapshot(self) -> DesignSnapshot:
        bridge, AltiumError = await self._bridge()
        try:
            # One BOM read carries designators, values, footprints, library
            # references and every pin's net -- a complete snapshot.
            bom = await bridge.send_command_async(
                "project.get_bom", {"limit": "5000"})
        except AltiumError as e:
            raise BackendUnavailableError(str(e)) from None
        parts: list[dict[str, Any]] = []
        pins: list[dict[str, Any]] = []
        unconnected = 0
        for comp in (bom or {}).get("components", []):
            ref = str(comp.get("designator", ""))
            parts.append({
                "refdes": ref,
                "value": comp.get("comment", ""),
                "footprint": comp.get("footprint", ""),
                "lib_ref": comp.get("lib_ref", ""),
            })
            for pin in comp.get("pins", []):
                net = str(pin.get("net", "") or "").strip()
                pins.append({"refdes": ref, "pin": pin.get("pin", ""),
                             "net": net})
                if not net:
                    unconnected += 1
        return DesignSnapshot.build(
            "altium", parts, pins,
            board_name=str((bom or {}).get("project_path", "") or ""),
            unconnected_pad_count=unconnected,
        )

    async def run_drc(self) -> dict[str, Any]:
        bridge, AltiumError = await self._bridge()
        try:
            raw = await bridge.send_command_async("pcb.run_drc", {})
        except AltiumError as e:
            raise BackendUnavailableError(str(e)) from None
        raw = raw or {}
        violations = raw.get("violations", []) or []
        return {
            "ok": True,
            "source": "altium",
            "violation_count": raw.get("violation_count", len(violations)),
            "violations": violations[:200],
        }

    async def run_erc(self) -> dict[str, Any]:
        bridge, AltiumError = await self._bridge()
        try:
            await bridge.send_command_async("generic.run_erc", {})
            raw = await bridge.send_command_async(
                "generic.get_erc_violations", {})
        except AltiumError as e:
            raise BackendUnavailableError(str(e)) from None
        raw = raw or {}
        violations = raw.get("violations", raw.get("items", [])) or []
        return {
            "ok": True,
            "source": "altium",
            "violation_count": raw.get("violation_count", len(violations)),
            "violations": violations[:200],
        }


_BACKENDS = {"altium": AltiumBackend, "kicad": KiCadBackend}


def resolve_backend(name: Optional[str] = None):
    """Return the adapter for ``name`` (or the active backend if None).

    Under the ``both`` backend, or any unrecognised value, the default
    (Altium) is used; pass an explicit name to target the other.
    """
    key = (name or active_backend_name()).strip().lower()
    cls = _BACKENDS.get(key, AltiumBackend)
    return cls()

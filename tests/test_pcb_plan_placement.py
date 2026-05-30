# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Integration tests for the pcb_plan_placement MCP tool.

No Altium: a routing fake bridge supplies pcb.get_components,
pcb.get_board_outline, project.get_connectivity_batch, and the board-wide
ePadObject query. These lock down the tool wiring and -- most
importantly -- the rotation geometry (pad -> component spatial join,
back-rotation to rotation-0 pin offsets, and the centroid->origin move
math), which cannot be checked live here.
"""

from __future__ import annotations

import pytest

from eda_agent.tools import pcb as pcb_module


def _capture_tool(monkeypatch, fake_bridge):
    monkeypatch.setattr(pcb_module, "get_bridge", lambda: fake_bridge)
    captured = {}

    class DummyMcp:
        def tool(self):
            def decorator(fn):
                captured[fn.__name__] = fn
                return fn
            return decorator

    pcb_module.register_pcb_tools(DummyMcp())
    return captured["pcb_plan_placement"]


class _RoutingBridge:
    """Fake bridge that answers each command from a scripted scenario."""

    def __init__(self, components, outline, connectivity, pads):
        self._components = components
        self._outline = outline
        self._connectivity = connectivity
        self._pads = pads
        self.calls: list[tuple[str, dict]] = []

    async def send_command_async(self, command, params=None, timeout=None):
        self.calls.append((command, params or {}))
        if command == "pcb.get_components":
            return {"components": self._components}
        if command == "pcb.get_board_outline":
            return {"bounding_rect": self._outline}
        if command == "project.get_connectivity_batch":
            return {"components": self._connectivity}
        if command == "generic.query_objects":
            return {"objects": self._pads}
        if command == "pcb.batch_move_components":
            return {"moves_applied": params.get("moves", "").count("|") + 1}
        return {"ok": True}


def _bbox(cx, cy, w, h):
    return {
        "x1": cx - w / 2, "y1": cy - h / 2,
        "x2": cx + w / 2, "y2": cy + h / 2,
        "width": w, "height": h,
    }


def _scenario():
    """A horizontal 2-pin R1 between an anchor above (net A) and below
    (net B). Rotating R1 90 deg aligns its pins with the anchors."""
    components = [
        {"designator": "R1", "x": 1000, "y": 1000, "rotation": 0,
         "layer": "TopLayer", "bbox": _bbox(1000, 1000, 700, 200)},
        {"designator": "A1", "x": 1000, "y": 2000, "rotation": 0,
         "layer": "TopLayer", "bbox": _bbox(1000, 2000, 100, 100)},
        {"designator": "A2", "x": 1000, "y": 0, "rotation": 0,
         "layer": "TopLayer", "bbox": _bbox(1000, 0, 100, 100)},
    ]
    outline = {"left": 0, "bottom": -500, "right": 2000, "top": 2500}
    connectivity = [
        {"designator": "R1", "pins": [{"net": "A"}, {"net": "B"}]},
        {"designator": "A1", "pins": [{"net": "A"}]},
        {"designator": "A2", "pins": [{"net": "B"}]},
    ]
    pads = [
        {"X": "1300", "Y": "1000", "Net": "A"},   # R1 pin A (right end)
        {"X": "700", "Y": "1000", "Net": "B"},    # R1 pin B (left end)
        {"X": "1000", "Y": "2000", "Net": "A"},   # A1
        {"X": "1000", "Y": "0", "Net": "B"},      # A2
    ]
    return _RoutingBridge(components, outline, connectivity, pads)


@pytest.mark.asyncio
async def test_dry_run_rotates_two_pin_part(monkeypatch):
    bridge = _scenario()
    tool = _capture_tool(monkeypatch, bridge)
    out = await tool(designators=["R1"])  # only R1 movable; anchors fixed

    assert out["dry_run"] is True
    assert out["pin_count"] == 4          # 2 (R1) + 1 (A1) + 1 (A2)
    assert out["movable_count"] == 1
    assert out["fixed_count"] == 2
    # R1 should be re-oriented to point its pins at the anchors.
    assert out["rotated_count"] == 1
    r1_moves = [m for m in out["moves"] if m["designator"] == "R1"]
    assert len(r1_moves) == 1
    assert r1_moves[0]["to"]["rotation"] in (90.0, 270.0)
    # Pin-aware HPWL must not get worse.
    assert out["hpwl_after"] <= out["hpwl_before"]
    assert out["overlap_pairs_after"] == 0


@pytest.mark.asyncio
async def test_optimize_rotation_false_skips_pad_query(monkeypatch):
    bridge = _scenario()
    tool = _capture_tool(monkeypatch, bridge)
    out = await tool(designators=["R1"], optimize_rotation=False)

    assert out["pin_count"] == 0
    assert out["rotated_count"] == 0
    # No pad query issued when rotation optimization is off.
    assert not any(c == "generic.query_objects" for c, _ in bridge.calls)


@pytest.mark.asyncio
async def test_apply_emits_rotation_in_batch_op(monkeypatch):
    bridge = _scenario()
    tool = _capture_tool(monkeypatch, bridge)
    out = await tool(designators=["R1"], apply=True)

    assert out["dry_run"] is False
    move_calls = [p for c, p in bridge.calls if c == "pcb.batch_move_components"]
    assert len(move_calls) == 1
    ops = move_calls[0]["moves"]
    assert "R1," in ops
    # Rotation field present (90.0 or 270.0) in the packed op.
    assert "90.0" in ops or "270.0" in ops


@pytest.mark.asyncio
async def test_no_board_outline_falls_back_to_error(monkeypatch):
    bridge = _scenario()
    bridge._outline = None
    tool = _capture_tool(monkeypatch, bridge)
    out = await tool(designators=["R1"], region=None)
    assert out.get("error") == "NO_BOARD_OUTLINE"


@pytest.mark.asyncio
async def test_explicit_region_skips_outline_query(monkeypatch):
    bridge = _scenario()
    tool = _capture_tool(monkeypatch, bridge)
    out = await tool(
        designators=["R1"],
        region={"x1": 0, "y1": -500, "x2": 2000, "y2": 2500},
    )
    assert out["region"] == {"x1": 0.0, "y1": -500.0, "x2": 2000.0, "y2": 2500.0}
    assert not any(c == "pcb.get_board_outline" for c, _ in bridge.calls)

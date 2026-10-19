# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The placement job, and the tool that moves its parts on a board.

Offline: the job runs on a synthetic board, and the apply tool talks to a
fake bridge through the suite's fail-closed isolation, so nothing here
can reach a running Altium.
"""

from __future__ import annotations

import random

import pytest

from eda_agent.design.jobs import get_job_store
from eda_agent.layout.bench import scramble_placement
from eda_agent.layout.model import Component, Layer, LayoutBoard, Pad, PadCopper, Rule
from eda_agent.layout.place.job import place_job
from tests.conftest import install_bridge_fake

BOARD_FILE = r"C:\boards\demo\demo.PcbDoc"


def _part(ref, x, y, a, b, locked=False):
    comp = Component(ref, x=x, y=y, locked=locked,
                     courtyard=[(x - 20, y - 10), (x + 20, y - 10), (x + 20, y + 10), (x - 20, y + 10)],
                     courtyard_source="courtyard")
    pads = [Pad(ref, "1", x - 10, y, net=a, copper=[PadCopper("TopLayer", "rect", 10, 12)]),
            Pad(ref, "2", x + 10, y, net=b, copper=[PadCopper("TopLayer", "rect", 10, 12)])]
    return comp, pads


def _board() -> LayoutBoard:
    rng = random.Random(1)
    b = LayoutBoard(name="demo.PcbDoc", outline=[(0, 0), (800, 0), (800, 500), (0, 500)],
                    layers=[Layer("TopLayer", "signal", 0), Layer("BottomLayer", "signal", 1)],
                    rules=[Rule("Clearance", "0", "All", "All", 1, True, {"gap": 6.0},
                                "Clearance Constraint (Gap=6mil) (All),(All)")])
    for ref, x, a, bb, locked in (("J1", 60, "N0", "X0", True), ("J2", 740, "N6", "X1", True)):
        c, p = _part(ref, x, 250, a, bb, locked)
        b.components.append(c)
        b.pads += p
    for k in range(6):
        c, p = _part(f"R{k + 1}", rng.uniform(100, 700), rng.uniform(60, 440), f"N{k}", f"N{k + 1}")
        b.components.append(c)
        b.pads += p
    return scramble_placement(b, seed=3)


def test_the_job_returns_whole_mil_moves_and_judges_them():
    res = place_job({"board": _board()})
    s = res["summary"]
    assert s["parts_moved"] == len(res["moves"]) > 0 and s["parts_fixed"] == 2
    assert s["failed"] == [] and s["overlaps"] == [] and s["violations"] == 0
    assert s["hpwl_after"] < s["hpwl_before"]
    for m in res["moves"]:
        assert set(m) == {"designator", "x", "y", "rotation"}
        assert isinstance(m["x"], int) and isinstance(m["y"], int)
        assert m["designator"] not in ("J1", "J2"), "fixed parts stay"


def test_an_outline_is_not_reported_as_the_boards_routing():
    from eda_agent.layout.model import Track
    b = _board()
    b.tracks.append(Track("Mechanical1", 0, 0, 800, 0, 5.0))
    notes = place_job({"board": b})["notes"]
    assert not any("tracks and" in n for n in notes)
    b.tracks.append(Track("TopLayer", 100, 100, 200, 100, 8.0, net="N1"))
    notes = place_job({"board": b})["notes"]
    assert any("1 tracks and 0 vias" in n for n in notes)


def test_the_job_moves_only_the_parts_asked_for():
    res = place_job({"board": _board(), "parts": ["R1", "R2"]})
    assert {m["designator"] for m in res["moves"]} <= {"R1", "R2"}


class _Bridge:
    def __init__(self, focused):
        self.focused = focused
        self.calls = []

    async def send_command_async(self, command, params=None, timeout=None):
        self.calls.append((command, params or {}))
        if command == "pcb.get_layout_model":
            return {"file": self.focused}
        if command == "pcb.batch_move_components":
            return {"moves_applied": len(params["moves"].split("|")), "failed": 0}
        raise AssertionError(command)


def _tool(monkeypatch, tmp_path, bridge, name):
    install_bridge_fake(monkeypatch, tmp_path, bridge)
    from eda_agent.tools import route as route_module
    monkeypatch.setattr(route_module, "get_bridge", lambda: bridge)
    captured = {}

    class DummyMcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    route_module.register_route_tools(DummyMcp())
    return captured[name]


def _finished_job():
    store = get_job_store()
    jid = store.submit("layout_place", place_job, {"board": _board()})
    assert store.wait(jid, timeout=60)
    return jid


@pytest.mark.asyncio
async def test_apply_refuses_when_another_board_is_focused(monkeypatch, tmp_path):
    bridge = _Bridge(focused=r"C:\clients\other\other.PcbDoc")
    tool = _tool(monkeypatch, tmp_path, bridge, "pcb_autoplace_apply")
    out = await tool(job_id=_finished_job(), expect_file=BOARD_FILE, checkpoint=False)
    assert "error" in out
    assert [c for c, _ in bridge.calls] == ["pcb.get_layout_model"], "nothing was moved"


@pytest.mark.asyncio
async def test_apply_moves_every_part_to_the_whole_mil_the_job_judged(monkeypatch, tmp_path):
    bridge = _Bridge(focused=BOARD_FILE)
    tool = _tool(monkeypatch, tmp_path, bridge, "pcb_autoplace_apply")
    jid = _finished_job()
    res = get_job_store().get(jid).result
    out = await tool(job_id=jid, expect_file=BOARD_FILE, checkpoint=False)
    assert out["moved"] == out["expected"] == len(res["moves"]) and out["failed"] == 0
    sent = "|".join(p["moves"] for c, p in bridge.calls if c == "pcb.batch_move_components")
    for m in res["moves"]:
        assert f"{m['designator']},{m['x']},{m['y']}," in sent


@pytest.mark.asyncio
async def test_apply_refuses_a_job_of_another_kind(monkeypatch, tmp_path):
    bridge = _Bridge(focused=BOARD_FILE)
    tool = _tool(monkeypatch, tmp_path, bridge, "pcb_autoplace_apply")
    out = await tool(job_id="nope", expect_file=BOARD_FILE, checkpoint=False)
    assert "error" in out and not bridge.calls

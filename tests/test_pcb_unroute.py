# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""pcb_unroute: which board it acts on, and what it asks the script for.

Offline, through the suite's fail-closed bridge isolation. What the script
then removes is checked on a live board (release verification step 20):
nothing here can see a footprint's copper survive.
"""

from __future__ import annotations

import pytest

from tests.conftest import install_bridge_fake

BOARD = r"C:\boards\demo\demo.PcbDoc"


class _Bridge:
    def __init__(self, focused):
        self.focused = focused
        self.calls = []

    async def send_command_async(self, command, params=None, timeout=None):
        self.calls.append((command, params or {}))
        if command == "pcb.get_layout_model":
            return {"file": self.focused}
        if command == "pcb.unroute":
            return {"file": params["expect_file"], "tracks": 12, "arcs": 2, "vias": 3,
                    "nets": 4, "kept_locked": 1, "failed": 0}
        raise AssertionError(command)


def _tool(monkeypatch, tmp_path, bridge):
    install_bridge_fake(monkeypatch, tmp_path, bridge)
    from eda_agent.tools import pcb as pcb_module
    monkeypatch.setattr(pcb_module, "get_bridge", lambda: bridge)
    captured = {}

    class DummyMcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    pcb_module.register_pcb_tools(DummyMcp())
    return captured["pcb_unroute"]


@pytest.mark.asyncio
async def test_another_focused_board_is_refused_before_anything_is_sent(monkeypatch, tmp_path):
    bridge = _Bridge(focused=r"C:\clients\other\other.PcbDoc")
    tool = _tool(monkeypatch, tmp_path, bridge)
    out = await tool(expect_file=BOARD, checkpoint=False)
    assert "error" in out
    assert [c for c, _ in bridge.calls] == ["pcb.get_layout_model"]


@pytest.mark.asyncio
async def test_the_script_is_given_the_boards_own_path_and_the_nets(monkeypatch, tmp_path):
    # Altium spells the path its own way; the script compares it exactly,
    # so it is sent back as Altium reported it, not as the caller typed it.
    bridge = _Bridge(focused=BOARD.upper())
    tool = _tool(monkeypatch, tmp_path, bridge)
    out = await tool(expect_file=BOARD, nets=["GND", "SDA"], checkpoint=False)
    assert out["tracks"] == 12 and out["kept_locked"] == 1
    sent = dict(bridge.calls)["pcb.unroute"]
    assert sent == {"expect_file": BOARD.upper(), "nets": "GND,SDA", "include_locked": "false"}


@pytest.mark.asyncio
async def test_every_net_is_an_empty_list_on_the_wire(monkeypatch, tmp_path):
    bridge = _Bridge(focused=BOARD)
    tool = _tool(monkeypatch, tmp_path, bridge)
    await tool(expect_file=BOARD, include_locked=True, checkpoint=False)
    sent = dict(bridge.calls)["pcb.unroute"]
    assert sent["nets"] == "" and sent["include_locked"] == "true"


@pytest.mark.asyncio
@pytest.mark.parametrize("nets", [[], ["A,B"]])
async def test_a_net_list_the_wire_cannot_carry_is_refused(monkeypatch, tmp_path, nets):
    # An empty list would reach the script as "every net".
    bridge = _Bridge(focused=BOARD)
    tool = _tool(monkeypatch, tmp_path, bridge)
    out = await tool(expect_file=BOARD, nets=nets, checkpoint=False)
    assert "error" in out and not bridge.calls

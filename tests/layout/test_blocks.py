# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Blocks found from the netlist of small synthetic boards: which parts
belong together, and in what role, before anything is placed."""

from __future__ import annotations

from eda_agent.layout.place.blocks import Netlist, find_blocks, supply_nets
from tests.layout.test_floorplan import board, header, mcu_board, quad, sot6, two_pin


def _blocks(b, movable=None):
    if movable is None:
        movable = {c.ref for c in b.components if not c.locked}
    return {bl.name: bl for bl in find_blocks(b, movable)}


def _owner(blocks):
    return {m.ref: bl.name for bl in blocks.values() for m in bl.members}


def _ic(b, ref, x, vcc=(3,), extra=None, footprint="QFP"):
    nets = [f"{ref}S{k}" for k in range(32)]
    for k in vcc:
        nets[k] = "VCC"
    for k in (7, 15, 23, 31):
        nets[k] = "GND"
    for k, n in (extra or {}).items():
        nets[k] = n
    quad(b, ref, x, 800, nets, footprint=footprint)


def test_an_ics_block_holds_its_decoupling_pull_up_and_chain_in_their_roles():
    b = mcu_board()
    blocks = _blocks(b)
    u1 = blocks["U1"]
    role = {m.ref: m for m in u1.members}
    assert set(role) == {"U1", "C1", "C2", "R1", "R2", "D1"}
    pad = {i: p for i, p in enumerate(b.pads)}
    assert role["C1"].role == role["C2"].role == "decap"
    assert pad[role["C1"].anchor].name == "4" == pad[role["C2"].anchor].name
    assert role["R1"].role == "pull" and pad[role["R1"].anchor].name == "21"
    assert role["R2"].rank == 1 and pad[role["R2"].anchor].name == "12"
    assert (role["D1"].role, role["D1"].rank, role["D1"].parent) == ("chain", 2, "R2")


def test_every_movable_part_is_in_exactly_one_block():
    b = mcu_board()
    two_pin(b, "R30", 1500, 1300, "LONE1", "LONE2")
    movable = {c.ref for c in b.components if not c.locked}
    seen = [m.ref for bl in find_blocks(b, movable) for m in bl.members if m.ref in movable]
    assert sorted(seen) == sorted(movable)


def test_a_pull_up_on_a_bus_does_not_make_the_bus_a_supply():
    # SDA reaches two ICs and has a resistor to the supply: shaped like a
    # supply with a capacitor, but a resistor is not a capacitor.
    b = board(2400, 1600)
    _ic(b, "U1", 600, extra={20: "SDA"})
    _ic(b, "U2", 1800, extra={20: "SDA"})
    two_pin(b, "R1", 300, 200, "SDA", "VCC")
    two_pin(b, "C1", 400, 200, "VCC", "GND")
    two_pin(b, "C2", 500, 200, "VCC", "GND")
    power = supply_nets(Netlist(b))
    assert "VCC" in power and "GND" in power and "SDA" not in power
    blocks = _blocks(b)
    m = blocks[_owner(blocks)["R1"]].member("R1")
    assert m.role == "pull"


def test_capacitors_spread_over_the_ics_of_a_supply_and_alike_ics_get_alike_shares():
    # An MCU and three identical sensors on one supply, six capacitors:
    # one each, then the two left over may not split the sensors unevenly.
    b = board(3000, 1600)
    _ic(b, "U1", 400)
    for k, x in enumerate((1000, 1600, 2200)):
        sot6(b, f"U{k + 2}", x, 800, [f"A{k}", "GND", f"B{k}", f"F{k}", "VCC", f"E{k}"])
    for k in range(6):
        two_pin(b, f"C{k + 1}", 200 + 80 * k, 200, "VCC", "GND")
    blocks = _blocks(b)
    caps = {h: sum(1 for m in blocks[h].members if m.role == "decap") for h in ("U1", "U2", "U3", "U4")}
    assert all(n >= 1 for n in caps.values()) and sum(caps.values()) == 6
    assert caps["U2"] == caps["U3"] == caps["U4"], caps


def test_capacitors_go_over_an_ics_supply_pins_one_each_first():
    b = board(1600, 1600)
    _ic(b, "U1", 800, vcc=(3, 11, 19))
    for k in range(3):
        two_pin(b, f"C{k + 1}", 200 + 80 * k, 200, "VCC", "GND")
    u1 = _blocks(b)["U1"]
    pins = sorted(b.pads[m.anchor].name for m in u1.members if m.role == "decap")
    assert pins == ["12", "20", "4"]


def test_a_connector_keeps_the_parts_on_its_pins_and_what_hangs_off_them_is_its_own():
    b = board(1600, 1000)
    header(b, "J1", 80, 500, ["A1", "A2", "GND"])
    two_pin(b, "R1", 600, 600, "A1", "N1")      # on a connector pin
    two_pin(b, "D1", 700, 600, "N1", "GND")     # behind R1
    two_pin(b, "R2", 600, 400, "A2", "GND")
    for k in range(6):                           # enough ground to be ground
        two_pin(b, f"R{k + 10}", 900, 100 + 60 * k, f"X{k}", "GND")
    blocks = _blocks(b)
    owner = _owner(blocks)
    assert owner["R1"] == owner["R2"] == "J1" and blocks["J1"].anchored
    assert owner["D1"] != "J1"


def test_small_circuits_no_head_reaches_form_their_own_blocks_and_repeat_as_channels():
    # Two transistor stages, each with a base resistor and a collector
    # load, wired to nothing with five pads.
    b = board(1600, 1000)
    for k, x in enumerate((400, 1000)):
        b_ = f"B{k}"
        from eda_agent.layout.model import Component, Pad, PadCopper
        b.components.append(Component(f"Q{k + 1}", footprint="SOT23", x=x, y=500,
                                      courtyard=[(x - 40, 460), (x + 40, 460), (x + 40, 540), (x - 40, 540)],
                                      courtyard_source="courtyard"))
        for name, dx, dy, net in (("1", -20, -25, b_), ("2", 20, -25, "GND"), ("3", 0, 25, f"C{k}")):
            b.pads.append(Pad(f"Q{k + 1}", name, x + dx, 500 + dy, net=net,
                              copper=[PadCopper("TopLayer", "rect", 16, 16)]))
        two_pin(b, f"R{k + 1}", x, 300, f"IN{k}", b_)
        two_pin(b, f"R{k + 11}", x, 700, f"C{k}", "VCC")
    for k in range(6):
        two_pin(b, f"C{k + 1}", 200 + 80 * k, 100, "VCC", "GND")
    blocks = _blocks(b)
    owner = _owner(blocks)
    assert owner["R1"] == owner["R11"] == "Q1" and owner["R2"] == owner["R12"] == "Q2"
    assert blocks["Q1"].kind == blocks["Q2"].kind == "minor"
    assert blocks["Q1"].channel_set == blocks["Q2"].channel_set >= 0
    assert (blocks["Q1"].channel_index, blocks["Q2"].channel_index) == (0, 1)

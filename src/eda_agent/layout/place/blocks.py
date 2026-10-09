# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Blocks: the sub-circuits a person would recognise on the board.

A layout reads as organised when each sub-circuit sits together: an IC
with its decoupling, pull-ups and filters round it, a connector with the
parts on its lines, a row of passives that belong together. This module
finds those groups from the netlist; ``compose`` lays each one out and
``floorplan`` places them.

The board model carries no grouping a block could come from: rooms are
read with their outline and a component-class scope, but not the class's
members. So the blocks are inferred.

**Supplies.** Supply and ground nets reach most parts and would tie every
part to every IC, so they never decide membership. A net is a supply
when a plane or a pour carries it, when it has more pads than an
ordinary net, when it is the largest net, or when it looks like one: a
capacitor from it to a known supply, and it feeds an IC through two pins
or more, or two ICs, or carries two such capacitors; or it is big and
carries two such capacitors, IC or none.

**Heads.** A part with five pads or more heads a block (an IC, or a
connector by its designator), and so does a fixed part with two pads or
more: it stands where it does for a mechanical reason and its parts
gather round it. A part of three or four pads across two supplies (a
regulator, a switch on a rail) heads a block of its own for its
capacitors.

**Members.** Each other part joins the head it shares the most ordinary
nets with; a diode prefers a connector, anything else an IC, when two
heads share as many. A two-pad part across a supply and ground is
decoupling: it joins an IC with a pin on that supply, the capacitors of
one supply spread over its ICs (one each first, then by how many supply
pins each has), and over each IC's pins. A part reached only through
another member's net joins the same block one rank further out, except
through the members of a fixed head: a connector keeps the parts on its
pins, and what hangs off them is a circuit of its own.

**Small blocks.** Among what no head reaches, a part of three or four
pads (a transistor, an LED) heads a small block of the parts wired to it.

**Passive blocks.** What is left is grouped by the nets its parts share;
parts on supplies alone are grouped by supply.

**Channels.** Blocks with the same footprints wired the same way are
copies of one circuit: they form a set, laid out as identical tiles.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from ..bench import FIXED_PREFIXES
from ..model import LayoutBoard

#: A part with at least this many pads heads a block.
HEAD_PADS = 5
#: A net with more pads than this is a supply whatever else it looks like
#: (the placer's ``BIG_NET``).
SUPPLY_PADS = 24
#: The largest net is ground only if it is at least this big.
GROUND_PADS = 6
#: Designators of parts that are not ICs whatever their pad count: they
#: are fixed for mechanical reasons (``bench.FIXED_PREFIXES``).
NOT_IC = FIXED_PREFIXES

#: Column order of members on one head pin: decoupling closest.
ROLE_ORDER = {"head": -1, "decap": 0, "bridge": 1, "pull": 2, "signal": 3, "chain": 4, "row": 5}


def natural(ref: str) -> list:
    """Designators in the order people count them: R2 before R10."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", ref)]


def prefix(ref: str) -> str:
    m = re.match(r"[A-Za-z_]*", ref)
    return m.group(0).upper() if m else ""


@dataclass
class Member:
    ref: str
    role: str                 # head | decap | bridge | pull | signal | chain | row
    rank: int = 0             # 0 the head, 1 on a head pin, 2.. chained further out
    anchor: int = -1          # board pad index of the head pin its column hangs from
    anchor2: int = -1         # a bridge's second head pin
    parent: str = ""          # the member it is chained to, else the head
    net: str = ""             # the net it reaches its parent by


@dataclass
class Block:
    name: str
    head: str = ""
    kind: str = "passive"     # ic | power | anchor | minor | passive
    members: list[Member] = field(default_factory=list)
    # The head is fixed: the block is built round it where it stands.
    anchored: bool = False
    channel_set: int = -1
    channel_index: int = -1
    signature: str = ""

    def refs(self) -> list[str]:
        return [m.ref for m in self.members]

    def member(self, ref: str) -> Member | None:
        for m in self.members:
            if m.ref == ref:
                return m
        return None


class Netlist:
    """Pads by part and by net, as indices into the board's pads."""

    def __init__(self, board: LayoutBoard):
        self.board = board
        self.pads_of: dict[str, list[int]] = {}
        self.by_net: dict[str, list[int]] = {}
        for i, p in enumerate(board.pads):
            if p.comp:
                self.pads_of.setdefault(p.comp, []).append(i)
            if p.net:
                self.by_net.setdefault(p.net, []).append(i)
        self.nets_of = {r: {board.pads[i].net for i in ps if board.pads[i].net}
                        for r, ps in self.pads_of.items()}

    def refs_on(self, net: str) -> set[str]:
        return {self.board.pads[i].comp for i in self.by_net.get(net, ()) if self.board.pads[i].comp}


def supply_nets(nl: Netlist) -> set[str]:
    """Supply and ground nets, known by what they connect (see the module
    notes). The only name read is a capacitor's designator: a pull-up
    from a supply to a bus line looks like a capacitor to ground in every
    other way, and taken as one it made the bus a supply."""
    b = nl.board
    sizes = {n: len(ps) for n, ps in nl.by_net.items()}
    power = {l.plane_net for l in b.layers if l.plane_net}
    power |= {r.net for r in b.regions if r.net and r.kind in ("plane", "pour_boundary")}
    power |= {n for n, k in sizes.items() if k > SUPPLY_PADS}
    if sizes:
        top = max(sizes, key=lambda n: (sizes[n], n))
        if sizes[top] >= GROUND_PADS:
            power.add(top)
    two = [(r, ps) for r, ps in nl.pads_of.items() if len(ps) == 2]
    caps = [(r, ps) for r, ps in two if prefix(r) == "C"] or two
    ics = {r for r, ps in nl.pads_of.items() if len(ps) >= HEAD_PADS and prefix(r) not in NOT_IC}
    for _ in range(4):
        count: dict[str, int] = {}
        for _, ps in caps:
            a, c = (b.pads[i].net for i in ps)
            if not a or not c or a == c:
                continue
            for n, other in ((a, c), (c, a)):
                if other in power and n not in power:
                    count[n] = count.get(n, 0) + 1
        grown = set()
        for n, k in count.items():
            if sizes.get(n, 0) < 3:
                continue
            on_ic: dict[str, int] = {}
            for i in nl.by_net[n]:
                q = b.pads[i].comp
                if q in ics:
                    on_ic[q] = on_ic.get(q, 0) + 1
            if on_ic and (k >= 2 or max(on_ic.values()) >= 2 or len(on_ic) >= 2):
                grown.add(n)
            elif k >= 2 and sizes[n] >= GROUND_PADS:
                # A big net with capacitors to a supply is the other half of
                # it, IC or none: on a board of discrete parts the supply
                # and its return can be as big as each other.
                grown.add(n)
        if not grown:
            break
        power |= grown
    return power


def _head_kind(ref: str, npads: int, nets: set[str], power: set[str], movable: bool) -> str:
    if npads >= HEAD_PADS:
        return "anchor" if prefix(ref) in NOT_IC else "ic"
    if not movable:
        return "anchor" if npads >= 2 and nets else ""
    if npads in (3, 4) and len(nets & power) >= 2 and len(nets) >= 3:
        return "power"
    return ""


def find_blocks(board: LayoutBoard, movable: set[str]) -> list[Block]:
    """The board's blocks. ``movable`` are the parts that may be placed;
    a fixed part can head a block (built round it where it stands) but is
    never a member. Every movable part lands in exactly one block."""
    nl = Netlist(board)
    pads = board.pads
    power = supply_nets(nl)
    nets_of = nl.nets_of
    signal = {r: ns - power for r, ns in nets_of.items()}

    heads: dict[str, str] = {}
    for r, ps in nl.pads_of.items():
        kind = _head_kind(r, len(ps), nets_of[r], power, r in movable)
        if kind:
            heads[r] = kind
    free = sorted((r for r in movable if r in nl.pads_of and r not in heads), key=natural)
    member: dict[str, Member] = {}
    owner: dict[str, str] = {}          # member -> head

    # Decoupling first: it is the one membership supplies decide.
    rail_caps: dict[str, list[str]] = {}
    for r in free:
        ps = nl.pads_of[r]
        if len(ps) != 2:
            continue
        a, c = pads[ps[0]].net, pads[ps[1]].net
        if a and c and a != c and a in power and c in power:
            rail = min((a, c), key=lambda n: (len(nl.by_net[n]), n))
            rail_caps.setdefault(rail, []).append(r)
    for rail, caps in sorted(rail_caps.items()):
        _spread_decaps(rail, caps, heads, nl, member, owner)

    # A fixed head keeps the parts on its pins only: what hangs off those
    # is a circuit of its own (a connector's conditioning, a ring of LED
    # channels), and chained to the connector it all piled up round it.
    _attach(free, heads, nl, signal, power, member, owner, grow=lambda h: h in movable)

    # What no head reached: parts of three or four pads (a transistor, an
    # LED) head small blocks of their own, so repeated small circuits come
    # out as channels.
    rest = [r for r in free if r not in member]
    minor = {r: "minor" for r in rest if len(nl.pads_of[r]) >= 3 and signal[r]}
    _attach([r for r in rest if r not in minor], minor, nl, signal, power, member, owner,
            grow=lambda h: True)
    led = {owner[r] for r in owner}
    heads.update({h: k for h, k in minor.items() if h in led})

    blocks: list[Block] = []
    by_head: dict[str, list[Member]] = {}
    for r, h in owner.items():
        by_head.setdefault(h, []).append(member[r])
    for h in sorted(heads, key=natural):
        mine = by_head.get(h, [])
        if h not in movable and not mine:
            continue
        mine.sort(key=lambda m: (m.rank, ROLE_ORDER[m.role], natural(m.ref)))
        blocks.append(Block(h, h, heads[h], [Member(h, "head")] + mine, anchored=h not in movable))

    rest = [r for r in sorted(movable, key=natural) if r not in member and r not in heads]
    blocks += _passive_blocks(rest, nl, signal, power)
    _channels(blocks, board, nl, power)
    return blocks


def _spread_decaps(rail: str, caps: list[str], heads: dict[str, str], nl: Netlist,
                   member: dict[str, Member], owner: dict[str, str]) -> None:
    """Give a supply's capacitors to the ICs on it: one each first, then
    by supply pins, and on each IC over its pins on the supply. With no IC
    on the supply, a regulator-like head takes them, then a connector.

    ICs alike (one footprint, as many pins on the supply) are given
    capacitors together, one more each, while there are enough for all of
    them: given one at a time, four identical channels came out with two,
    one, one and one, and were no longer copies of one circuit."""
    pads = nl.board.pads
    comp = {c.ref: c for c in nl.board.components}
    pins = {h: [i for i in nl.pads_of[h] if pads[i].net == rail] for h in heads}
    cands = [h for h in heads if heads[h] in ("ic", "power") and pins[h]]
    if not cands:
        cands = [h for h in heads if pins[h]]
    if not cands:
        return
    weight = {h: (len(pins[h]) if heads[h] == "ic" else 1) for h in cands}
    cands.sort(key=lambda h: (-weight[h], natural(h)))
    alike: dict[tuple, list[str]] = {}
    for h in cands:
        alike.setdefault((comp[h].footprint if h in comp else h, len(pins[h]), heads[h]), []).append(h)
    classes = sorted(alike.values(), key=lambda g: cands.index(g[0]))
    given = {h: 0 for h in cands}
    plan: list[str] = []
    for h in cands[:len(caps)]:
        plan.append(h)
        given[h] += 1
    while len(plan) < len(caps):
        left = len(caps) - len(plan)
        fits = [g for g in classes if len(g) <= left]
        if fits:
            g = min(fits, key=lambda g: (given[g[0]] / weight[g[0]], classes.index(g)))
        else:
            g = [min(cands, key=lambda q: (given[q] / weight[q], cands.index(q)))]
        for h in g:
            plan.append(h)
            given[h] += 1
    on_pin: dict[int, int] = {}
    for c, h in zip(caps, sorted(plan, key=cands.index)):
        pin = min(pins[h], key=lambda i: (on_pin.get(i, 0), natural(pads[i].name), i))
        on_pin[pin] = on_pin.get(pin, 0) + 1
        member[c] = Member(c, "decap", 1, pin, -1, h, rail)
        owner[c] = h


_PREF = {"ic": 0, "power": 1, "minor": 1, "anchor": 2}
_DIODE_PREF = {"anchor": 0, "ic": 1, "power": 2, "minor": 2}


def _attach(pool: list[str], heads: dict[str, str], nl: Netlist, signal, power,
            member: dict[str, Member], owner: dict[str, str], grow) -> None:
    """Parts of ``pool`` to the head they share the most ordinary nets
    with; then the parts of ``pool`` reached only through those members'
    nets, one rank further out per net. ``grow(head)`` says whether a
    head's members are chained to."""
    added = []
    for r in pool:
        if r in member or r in heads:
            continue
        shared: dict[str, int] = {}
        for n in signal[r]:
            for q in nl.refs_on(n):
                if q in heads and q != r:
                    shared[q] = shared.get(q, 0) + 1
        if not shared:
            continue
        pref = _DIODE_PREF if prefix(r) == "D" else _PREF
        h = min(shared, key=lambda q: (-shared[q], pref[heads[q]], -len(nl.pads_of[q]), natural(q)))
        member[r] = _direct(r, h, nl, signal, power)
        owner[r] = h
        added.append(r)
    eligible = set(pool)
    frontier = [r for r in added if member[r].role != "decap" and grow(owner[r])]
    rank = 2
    while frontier:
        reach: dict[str, dict[str, int]] = {}
        for m in frontier:
            for n in signal[m]:
                for q in nl.refs_on(n):
                    if q in eligible and q not in member and q not in heads:
                        reach.setdefault(q, {})
                        reach[q][m] = reach[q].get(m, 0) + 1
        nxt = []
        for q in sorted(reach, key=natural):
            par = min(reach[q], key=lambda m: (-reach[q][m], natural(m)))
            link = sorted(signal[q] & signal[par])[0]
            member[q] = Member(q, "chain", rank, member[par].anchor, member[par].anchor2,
                               par, link)
            owner[q] = owner[par]
            nxt.append(q)
        frontier = nxt
        rank += 1


def _direct(r: str, h: str, nl: Netlist, signal, power) -> Member:
    """A part on a head pin: its role, and the pin its column hangs from."""
    pads = nl.board.pads
    head_pin = {}
    for i in sorted(nl.pads_of[h], key=lambda i: (natural(pads[i].name), i)):
        head_pin.setdefault(pads[i].net, i)
    shared = sorted(signal[r] & set(head_pin), key=lambda n: (natural(pads[head_pin[n]].name), n))
    ps = nl.pads_of[r]
    if len(ps) == 2:
        a, c = pads[ps[0]].net, pads[ps[1]].net
        if a in signal[r] and c in signal[r] and a != c and a in head_pin and c in head_pin:
            first, second = sorted((a, c), key=lambda n: (natural(pads[head_pin[n]].name), n))
            return Member(r, "bridge", 1, head_pin[first], head_pin[second], h, first)
        if (a in power) != (c in power):
            link = c if a in power else a
            return Member(r, "pull", 1, head_pin[link], -1, h, link)
    return Member(r, "signal", 1, head_pin[shared[0]], -1, h, shared[0])


def _passive_blocks(rest: list[str], nl: Netlist, signal, power) -> list[Block]:
    """Parts no head reaches, grouped by the ordinary nets they share;
    parts on supplies alone (or on nothing), by their supply."""
    pads = nl.board.pads
    pool = set(rest)
    seen: set[str] = set()
    groups: list[list[str]] = []
    on_rail: dict[str, list[str]] = {}
    for r in rest:
        if r in seen:
            continue
        if not signal.get(r):
            nets = sorted(nl.nets_of.get(r, ()), key=lambda n: (len(nl.by_net[n]), n))
            on_rail.setdefault(nets[0] if nets else "", []).append(r)
            seen.add(r)
            continue
        group, todo = [], [r]
        seen.add(r)
        while todo:
            q = todo.pop()
            group.append(q)
            for n in signal.get(q, ()):
                for i in nl.by_net.get(n, ()):
                    o = pads[i].comp
                    if o in pool and o not in seen:
                        seen.add(o)
                        todo.append(o)
        groups.append(sorted(group, key=natural))
    groups += [sorted(g, key=natural) for _, g in sorted(on_rail.items())]
    out = []
    for g in groups:
        name = g[0] if len(g) == 1 else f"{g[0]}..{g[-1]}"
        out.append(Block(name, "", "passive", [Member(r, "row") for r in g]))
    return out


# -- channels ----------------------------------------------------------------

def _signature(block: Block, board: LayoutBoard, nl: Netlist, power: set[str]) -> tuple[str, list[str]]:
    """A hash of the block's footprints and wiring that copies of one
    circuit share, and each member's label (members of two copies with
    the same label correspond). Weisfeiler-Lehman refinement over the
    members, each labelled by footprint, role and per pad whether its net
    is a supply, inside the block, or leaves it."""
    pads = board.pads
    refs = set(block.refs())
    comp = {c.ref: c for c in board.components}
    label: dict[str, str] = {}
    for r in block.refs():
        flags = []
        for i in nl.pads_of.get(r, ()):
            n = pads[i].net
            if not n:
                f = "-"
            elif n in power:
                f = "p"
            elif nl.refs_on(n) <= refs:
                f = "i"
            else:
                f = "x"
            flags.append(f"{pads[i].name}:{f}")
        m = block.member(r)
        fp, side = (comp[r].footprint, comp[r].side) if r in comp else ("", "")
        label[r] = f"{fp}|{side}|{m.role if m else ''}|{','.join(sorted(flags))}"
    edges: dict[str, list[tuple[str, str, str]]] = {r: [] for r in refs}
    for r in refs:
        for i in nl.pads_of.get(r, ()):
            n = pads[i].net
            if not n or n in power:
                continue
            for j in nl.by_net[n]:
                q = pads[j].comp
                if q in refs and q != r:
                    edges[r].append((pads[i].name, pads[j].name, q))
    for _ in range(3):
        label = {r: _h(label[r] + "#" + ";".join(sorted(f"{a}>{b}>{label[q]}" for a, b, q in edges[r])))
                 for r in refs}
    return _h("|".join(sorted(label.values()))), [label[r] for r in block.refs()]


def _h(s: str) -> str:
    # hashlib, not hash(): str hashes change from one process to the next.
    return hashlib.blake2b(s.encode("utf-8"), digest_size=10).hexdigest()


def _channels(blocks: list[Block], board: LayoutBoard, nl: Netlist, power: set[str]) -> None:
    """Mark sets of blocks that are copies of one circuit, in channel
    order (by head, or first member), each member list put in the order
    that makes copies correspond member for member."""
    sigs: dict[str, list[tuple[Block, list[str]]]] = {}
    for b in blocks:
        if b.anchored:
            continue
        sig, labels = _signature(b, board, nl, power)
        b.signature = sig
        sigs.setdefault(sig, []).append((b, labels))
    k = 0
    for sig in sorted(sigs):
        group = sigs[sig]
        if len(group) < 2:
            continue
        group.sort(key=lambda bl: natural(bl[0].head or bl[0].members[0].ref))
        for idx, (b, labels) in enumerate(group):
            order = sorted(range(len(b.members)),
                           key=lambda j: (b.members[j].role != "head", labels[j], natural(b.members[j].ref)))
            b.members = [b.members[j] for j in order]
            b.channel_set, b.channel_index = k, idx
        k += 1

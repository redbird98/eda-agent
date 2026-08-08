# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Run the EasyEDA->Altium plan through the REAL tool functions.

Signature checking (test_plan_steps_match_tool_api) proves the argument
NAMES exist. It cannot prove the values survive the tool body: a step
can name every parameter correctly and still explode on a type
coercion, an empty required string, or a payload builder that assumes a
shape the plan does not produce.

So this drives each step through the actual registered coroutine with a
recording bridge underneath. No Altium, no simulator -- the simulator
implements only 6 of the 10 actions this plan uses, and per the
project's own caveat a green simulator run says little about real
Pascal anyway. What it does say is that the plan's values flow through
the Python tool layer and come out as a well-formed bridge command.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "easyeda_soic8.json"


@pytest.fixture
def wired(altium_tool_harness):
    """The shared harness from conftest, under this file's own name.

    Shared rather than copied so the KiCad importer's plan test and this
    one cannot drift into checking different things.
    """
    return altium_tool_harness


def _plan():
    from eda_agent.libimport.easyeda import parse_component
    from eda_agent.libimport.easyeda.altium import build_altium_plan

    comp = parse_component(json.loads(FIXTURE.read_text(encoding="utf-8")))
    return build_altium_plan(comp, "T.SchLib", "T.PcbLib")


def test_every_plan_step_runs_without_raising(wired):
    """The whole plan, executed in order, through the real tools."""
    fns, bridge = wired
    plan = _plan()
    assert len(plan["steps"]) >= 10, "plan too small to be a real check"

    failures: list[str] = []
    for i, step in enumerate(plan["steps"], 1):
        fn = fns.get(step["tool"])
        if fn is None:
            failures.append(f"step {i}: {step['tool']} not registered")
            continue
        try:
            asyncio.run(fn(**step["args"]))
        except Exception as exc:  # noqa: BLE001 - collect, do not abort
            failures.append(
                f"step {i}: {step['tool']} raised "
                f"{type(exc).__name__}: {exc}")
    assert not failures, "\n".join(failures)

    # Every step should have produced a bridge command; a tool that
    # silently no-ops would leave the library half-built with no error.
    assert len(bridge.calls) >= len(plan["steps"]), (
        f"{len(plan['steps'])} steps produced only {len(bridge.calls)} "
        f"bridge commands; a step is silently doing nothing")


def test_pin_payload_survives_the_tool_layer(wired):
    """Pin geometry must reach the bridge, not just be accepted.

    lib_add_pins takes a list of dicts and flattens it into a payload
    string. That transform is where a plan's values would quietly get
    dropped, and the resulting symbol would have pins in the wrong place
    with no error anywhere.
    """
    fns, bridge = wired
    plan = _plan()
    step = next(s for s in plan["steps"] if s["tool"] == "lib_add_pins")
    asyncio.run(fns["lib_add_pins"](**step["args"]))

    cmd, params = next(c for c in bridge.calls if "add_pins" in c[0])
    blob = json.dumps(params)
    for pin in step["args"]["pins"]:
        assert pin["designator"] in blob, f"pin {pin['designator']} lost"
        assert str(pin["x"]) in blob, f"pin {pin['designator']} x lost"
    # Rotation is the field that was wrong for every pin before the
    # convention fix, so assert it specifically rather than trusting the
    # designator check to stand in for it.
    assert "rotation" in blob or "orientation" in blob


def test_footprint_pads_reach_the_bridge(wired):
    fns, bridge = wired
    plan = _plan()
    step = next(s for s in plan["steps"]
                if s["tool"] == "lib_add_footprint_pads")
    asyncio.run(fns["lib_add_footprint_pads"](**step["args"]))

    cmd, params = next(c for c in bridge.calls if "add_footprint_pads" in c[0])
    blob = json.dumps(params)
    for pad in step["args"]["pads"]:
        assert pad["designator"] in blob, f"pad {pad['designator']} lost"


def test_no_step_silently_discards_part_of_its_payload(wired):
    """"It did not raise" is not the same as "it did the work".

    Several bulk tools filter their input and report the count rather
    than failing: lib_add_footprint_pads drops any pad with a blank
    designator and returns ``skipped_invalid``. A plan can therefore
    execute with no exception while quietly losing geometry.

    That is not hypothetical. The hole handling in this converter did
    exactly that: it emitted a pad with an empty designator, the plan
    looked right, and the pad never reached Altium.
    """
    fns, bridge = wired
    plan = _plan()

    losses = []
    for i, step in enumerate(plan["steps"], 1):
        fn = fns.get(step["tool"])
        if fn is None:
            continue
        result = asyncio.run(fn(**step["args"]))
        if not isinstance(result, dict):
            continue
        for key in ("skipped_invalid", "skipped", "failed", "dropped"):
            count = result.get(key)
            if isinstance(count, int) and count:
                losses.append(f"step {i} {step['tool']}: {key}={count}")
    assert not losses, (
        "steps executed without error but discarded part of their "
        "payload:\n  " + "\n  ".join(losses))

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Smoke tests for design MCP tools, they register and dispatch correctly."""

from __future__ import annotations

import asyncio
import json

import pytest

from eda_agent.design.discipline import get_discipline
from eda_agent.design.plan import DesignPlan, Net, Part, PinRef, Sheet


def _valid_plan_json() -> str:
    plan = DesignPlan(
        spec="LED + 1k on 5V",
        summary="LED tied to GND through 1k on the V5 rail.",
        sheets=[Sheet(name="main")],
        parts=[
            Part(refdes="R1", lib_ref="RES_0805", value="1k", sheet="main"),
            Part(refdes="D1", lib_ref="LED_RED", sheet="main"),
        ],
        nets=[
            Net(
                name="V5",
                is_power=True,
                pins=[PinRef(refdes="R1", pin="1"), PinRef(refdes="D1", pin="A")],
            ),
            Net(
                name="LED_K",
                pins=[PinRef(refdes="R1", pin="2"), PinRef(refdes="D1", pin="A")],
            ),
        ],
    )
    return plan.model_dump_json()


class _CapturingMCP:
    """Minimal stand-in for FastMCP, records registered tools."""

    def __init__(self) -> None:
        self.tools: dict[str, callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _registered_tools():
    from eda_agent.tools.design import register_design_tools

    fake = _CapturingMCP()
    register_design_tools(fake)
    return fake.tools


def test_register_exposes_expected_tools() -> None:
    tools = _registered_tools()
    assert set(tools.keys()) >= {
        "design_get_discipline",
        "design_snapshot_inventory",
        "design_validate_plan",
        "design_execute_plan",
        "design_validate",
    }


def test_get_discipline_returns_text_and_schema() -> None:
    tools = _registered_tools()
    result = asyncio.run(tools["design_get_discipline"]())
    assert "discipline" in result
    assert "schema" in result
    assert "Design Discipline" in result["discipline"]
    assert "DesignPlan" in result["schema"]["title"]


def test_validate_plan_accepts_valid_plan() -> None:
    tools = _registered_tools()
    result = asyncio.run(tools["design_validate_plan"](plan_json=_valid_plan_json()))
    assert result["ok"] is True
    assert "Plan valid" in result["summary"]


def test_validate_plan_rejects_invalid_json() -> None:
    tools = _registered_tools()
    result = asyncio.run(tools["design_validate_plan"](plan_json="not json"))
    assert result["ok"] is False
    assert any("invalid JSON" in e for e in result["errors"])


def test_validate_plan_rejects_schema_failure() -> None:
    tools = _registered_tools()
    bad = json.dumps({"spec": "x"})  # missing required fields
    result = asyncio.run(tools["design_validate_plan"](plan_json=bad))
    assert result["ok"] is False
    assert len(result["errors"]) > 0


def test_validate_plan_rejects_cross_check_failure() -> None:
    tools = _registered_tools()
    payload = json.loads(_valid_plan_json())
    payload["nets"][0]["pins"][0]["refdes"] = "R99"  # not in parts
    result = asyncio.run(tools["design_validate_plan"](plan_json=json.dumps(payload)))
    assert result["ok"] is False
    assert any("R99" in e for e in result["errors"])


def test_execute_plan_rejects_invalid_json() -> None:
    """Smoke check that the executor MCP tool surface validates input
    before any Altium round-trip; a real round-trip is covered by the
    executor unit tests via fake bridge."""
    tools = _registered_tools()
    result = asyncio.run(
        tools["design_execute_plan"](
            plan_json="not json",
            project_path="C:\\fake.PrjPcb",
        )
    )
    assert result["ok"] is False
    assert any("invalid JSON" in n for n in result["notes"])


# design.validate dispatches to the real bridge, covered by test_validator.py
# unit tests with a fake bridge. We do not exercise the MCP wrapper here
# because it would need either a fake bridge injection point or a running
# Altium instance.


def test_discipline_text_contains_key_rules() -> None:
    text = get_discipline()
    # Connectivity policy: ports > block-local wires > cross-block labels.
    assert "block-local" in text.lower()
    assert "port glyph" in text.lower()
    assert "datasheet" in text.lower()
    assert "nda" in text.lower()
    assert "DesignPlan JSON schema" in text

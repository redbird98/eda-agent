# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Tests for the tool-reference generator (roadmap 2.4)."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "gen_tool_reference", _REPO / "scripts" / "gen_tool_reference.py")
gen = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gen)


@pytest.fixture(scope="module")
def reference():
    return gen.build_reference()


@pytest.fixture(scope="module")
def tool_names():
    from mcp.server.fastmcp import FastMCP
    from eda_agent.tools import register_all_tools
    m = FastMCP("t")
    register_all_tools(m)
    return [t.name for t in asyncio.run(m.list_tools())]


def test_reference_lists_every_tool(reference, tool_names):
    for name in tool_names:
        assert f"`{name}`" in reference, f"{name} missing from the reference"


def test_reference_has_legend_and_header(reference):
    assert "# Tool reference" in reference
    assert "**Maturity**" in reference and "**Interaction**" in reference
    assert "Auto-generated" in reference


def test_reference_groups_key_categories(reference):
    for cat in ("## application", "## pcb", "## design", "## audit", "## meta"):
        assert cat in reference


def test_reference_flags_modal_tools(reference):
    # A modal tool must be shown as modal so a reader plans around it.
    lines = [ln for ln in reference.splitlines() if "proj_sync_pcb`" in ln]
    assert lines and "modal" in lines[0]


def test_pipe_in_summary_is_escaped(reference):
    # Every table row has exactly 4 content cells (5 pipes) — an unescaped
    # pipe in a summary would break the markdown table.
    for ln in reference.splitlines():
        if ln.startswith("| `"):
            assert ln.count("|") == 5, ln

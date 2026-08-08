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
    # Every table row has exactly 4 content cells (5 pipes): an unescaped
    # pipe in a summary would break the markdown table.
    for ln in reference.splitlines():
        if ln.startswith("| `"):
            assert ln.count("|") == 5, ln


def test_the_committed_reference_is_current(reference):
    """The file in docs/ must match what the generator produces now.

    Every check above reads freshly generated output, so all of them
    stay green while the COMMITTED file rots. That file is the one
    people actually read, and it carries per-tool maturity and
    interaction badges: a stale copy tells a reader an operation is
    safe or offline when the code says otherwise. Nothing regenerates
    it automatically, so nothing but this notices.

    Generation is deterministic (same input, byte-identical output), so
    this compares whole files rather than sampling.
    """
    committed = _REPO / "docs" / "TOOL_REFERENCE.md"
    assert committed.is_file(), f"{committed} is missing"
    on_disk = committed.read_text(encoding="utf-8")
    assert on_disk == reference, (
        "docs/TOOL_REFERENCE.md is out of date with the tool surface. "
        "Regenerate it with `python scripts/gen_tool_reference.py` and "
        "include the result in the same commit as the change that moved "
        "it.")

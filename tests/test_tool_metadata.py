# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Drift guard for the out-of-band tool-metadata registry.

``eda_agent.tools.metadata`` classifies every MCP tool by category /
maturity / interaction for the discovery layer, the docs generator, and
the bridge-audit interaction classes. These tests keep that registry
honest as tools come and go:

  - every registered tool resolves to a valid record (recognized
    category, allowed maturity, allowed interaction);
  - no override names a tool that no longer exists (stale-override drift);
  - the audit-identified modal/partial tools keep their classification.
"""

from __future__ import annotations

import asyncio

import pytest

from eda_agent.tools import metadata as M


@pytest.fixture(scope="module")
def tool_names() -> list[str]:
    from mcp.server.fastmcp import FastMCP
    from eda_agent.tools import register_backend

    # register_backend, NOT register_all_tools: the latter is only the
    # Altium-specific suite. The backend-agnostic registrars
    # (register_eda_tools, register_meta_tools, register_parts_tools)
    # are layered on top of it, so the inner call builds a surface no
    # real server serves and makes their overrides look stale.
    mcp = FastMCP("test")
    register_backend(mcp, "altium")
    tools = asyncio.run(mcp.list_tools())
    return sorted(t.name for t in tools)


def test_every_tool_has_a_recognized_category(tool_names):
    uncategorized = [n for n in tool_names if M.category_of(n) == "other"]
    assert not uncategorized, (
        "tools with an unrecognized name prefix (add the prefix to "
        f"_CATEGORY_BY_PREFIX in metadata.py): {uncategorized}"
    )


def test_every_tool_resolves_to_valid_metadata(tool_names):
    for name in tool_names:
        rec = M.tool_metadata(name)
        assert rec["maturity"] in M.MATURITIES, (name, rec)
        assert rec["interaction"] in M.INTERACTIONS, (name, rec)


def test_no_stale_overrides(tool_names):
    registered = set(tool_names)
    stale_interaction = [n for n in M.INTERACTION_OVERRIDES if n not in registered]
    stale_maturity = [n for n in M.MATURITY_OVERRIDES if n not in registered]
    assert not stale_interaction, f"stale interaction overrides: {stale_interaction}"
    assert not stale_maturity, f"stale maturity overrides: {stale_maturity}"


def test_modal_and_partial_tools_are_classified():
    # The bridge audit's human-in-the-loop tools must not silently regress
    # to ``silent`` -- an LLM planning around them needs the warning.
    assert M.interaction_of("proj_sync_pcb") == M.MODAL
    assert M.interaction_of("proj_sync_schematic") == M.MODAL
    assert M.interaction_of("pcb_add_teardrops") == M.MODAL
    assert M.interaction_of("pcb_place_components") == M.PARTIAL


def test_readonly_queries_are_not_marked_silent():
    for name in ("pcb_get_nets", "proj_get_bom", "audit_find_via_antennas",
                 "pcb_calc_impedance"):
        assert M.interaction_of(name) == M.READONLY, name


def test_catalog_is_sorted_by_category_then_name(tool_names):
    recs = M.catalog(tool_names)
    keys = [(r["category"], r["name"]) for r in recs]
    assert keys == sorted(keys)

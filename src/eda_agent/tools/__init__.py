# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""MCP Tools for Altium Designer.

Error-shape convention (two families, consistent within each):

- Bridge-backed tools (anything that round-trips Altium) report failures as
  ``{"error": "<message>", <count_field>: 0, ...}`` -- or raise, in which
  case the MCP layer surfaces the exception text. Successful payloads come
  from the Pascal handler verbatim.
- Offline calculators (``pcb_calc_*``, ``design_compute_*``, exporters)
  report ``{"ok": False, "reason": "<message>"}`` and ``{"ok": True, ...}``
  on success.

New tools should match the family they belong to rather than invent a third
shape.
"""

from .application import (
    register_application_tools,
    register_meta_tools,
)
from .project import register_project_tools
from .library import register_library_tools
from .generic import register_generic_tools
from .pcb import register_pcb_tools
from .review import register_review_tools
from .sim import register_sim_tools
from .design import register_design_tools
from .render import register_render_tools
from .audit import register_audit_tools
from .route import register_route_tools
from .kicad import register_kicad_tools
from .eda import register_eda_tools
from .calc import register_calc_tools
from .plan import register_plan_tools
from .parts import register_parts_tools

# Recognised backends. "altium" is the default and stays the full historical
# suite; "kicad" exposes only the KiCad-native tools; "both" unions them for
# the rare user who drives both EDA tools from one server.
BACKENDS = ("altium", "kicad", "both")
DEFAULT_BACKEND = "altium"

# "full" advertises every tool. "minimal" registers them all internally
# but advertises only tool_catalog + tool_invoke, for MCP clients that
# cap tool count or stall serializing hundreds of schemas at startup.
# See eda_agent.tools.registry for why this beats merging tools into
# generic dispatchers.
TOOLSETS = ("full", "minimal")
DEFAULT_TOOLSET = "full"


def register_all_tools(mcp):
    """Register all Altium tools with the MCP server."""
    register_application_tools(mcp)
    register_project_tools(mcp)
    register_library_tools(mcp)
    register_generic_tools(mcp)
    register_pcb_tools(mcp)
    register_review_tools(mcp)
    register_sim_tools(mcp)
    register_design_tools(mcp)
    register_render_tools(mcp)
    register_audit_tools(mcp)
    register_route_tools(mcp)


def register_backend(
    mcp, backend: str = DEFAULT_BACKEND, toolset: str = DEFAULT_TOOLSET,
) -> str:
    """Register the tool surface for one backend and return the name used.

    An unknown backend falls back to the default rather than raising, so a
    stray value in the environment can never leave the server toolless.

    Args:
        toolset: "full" advertises every tool. "minimal" advertises only
            tool_catalog and tool_invoke while keeping all the others
            reachable through them, for clients that cannot cope with a
            few hundred tools.
    """
    toolset = (toolset or DEFAULT_TOOLSET).strip().lower()
    if toolset not in TOOLSETS:
        toolset = DEFAULT_TOOLSET
    if toolset == "minimal":
        return _register_minimal(mcp, backend)
    return _register_full(mcp, backend)


def _register_minimal(mcp, backend: str) -> str:
    """Capture the whole surface, then advertise only the meta-tools."""
    from .registry import MINIMAL_TOOLS, ToolRegistry

    registry = ToolRegistry()
    name = _register_full(registry, backend)
    missing = [t for t in MINIMAL_TOOLS if t not in registry]
    if missing:
        # Advertising nothing usable would be worse than a full surface,
        # so fall back loudly rather than serve an unusable server.
        raise RuntimeError(
            f"minimal toolset needs {missing}, which the backend did not "
            f"register")
    for tool_name in MINIMAL_TOOLS:
        # These close over `registry`, so they discover and dispatch
        # across everything captured above even though the client sees
        # only these two.
        mcp.tool()(registry.get(tool_name).fn)
    return name


def _register_full(mcp, backend: str = DEFAULT_BACKEND) -> str:
    backend = (backend or DEFAULT_BACKEND).strip().lower()
    if backend not in BACKENDS:
        backend = DEFAULT_BACKEND
    if backend in ("altium", "both"):
        register_all_tools(mcp)
    if backend in ("kicad", "both"):
        register_kicad_tools(mcp)
    # The engineering calculators (pcb_calc_*) are pure physics and
    # EDA-independent. Altium's register_all_tools already defines them, so add
    # them only for a KiCad-only server to reach calculator parity without
    # double-registering under "both".
    if backend == "kicad":
        register_calc_tools(mcp)
        register_plan_tools(mcp)
    # The EDA-agnostic main-flow tools (review_design, get_board_info,
    # list_components, list_nets) work on whichever backend is active, so they
    # register for every backend.
    register_eda_tools(mcp)
    # tool_catalog / tool_invoke describe the tool surface itself rather than
    # any one EDA tool, so they belong on every backend too. They also have to
    # exist for the minimal toolset to be possible at all.
    register_meta_tools(mcp)
    # Part sourcing is EDA-agnostic: the providers answer about parts, not
    # about Altium or KiCad, so both backends get them.
    register_parts_tools(mcp)
    return backend


__all__ = [
    "register_all_tools",
    "register_backend",
    "register_kicad_tools",
    "register_eda_tools",
    "register_calc_tools",
    "register_plan_tools",
    "BACKENDS",
    "DEFAULT_BACKEND",
    "TOOLSETS",
    "DEFAULT_TOOLSET",
    "register_application_tools",
    "register_meta_tools",
    "register_parts_tools",
    "register_project_tools",
    "register_library_tools",
    "register_generic_tools",
    "register_pcb_tools",
    "register_review_tools",
    "register_sim_tools",
    "register_design_tools",
    "register_render_tools",
    "register_audit_tools",
    "register_route_tools",
    "register_kicad_tools",
]

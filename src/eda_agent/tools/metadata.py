# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Out-of-band metadata for the MCP tool surface.

FastMCP (mcp 1.5.x, pinned <2) has no ``tags``/``annotations`` field on its
tool model, so tool metadata cannot ride on the ``@mcp.tool()`` decorator.
This module keeps a decorator-independent registry instead, keyed by tool
name, that other layers consume:

- the future discovery layer (a ``tool_catalog`` meta-tool) filters by
  ``category`` / ``interaction`` so a client need not load 345 schemas;
- the docs generator groups by ``category`` and badges by ``maturity``;
- a CI drift test asserts every registered tool resolves here and that no
  override names a tool that no longer exists.

Three axes:

``category`` -- derived from the name prefix; 100% mechanical.

``maturity`` -- how well the tool is verified.
    ``offline``    pure Python, deterministic, fully covered by CI unit
                   tests; never touches Altium.
    ``simulator``  bridge-backed, and EVERY command it sends is
                   implemented by the in-repo Python Altium simulator, so
                   the whole call can be driven in CI with no Altium.
    ``live_only``  bridge-backed, and at least one command it sends has
                   no simulator handler; verifiable only against a real
                   Altium session.
  ``simulator`` is measured against the simulator itself rather than
  estimated, and a guard test fails if the two disagree. Read it as
  "exercisable without Altium", not as "a test exercises it today":
  those are different claims and only the first is enforced here.
  The Phase 1.3 nightly live-run is the intended source of a future
  promotion to a ``verified`` tier; when that lands, record per-tool
  results in ``MATURITY_OVERRIDES``.

``interaction`` -- what running the tool does to the Altium session.
    ``readonly``   queries / reads; no mutation.
    ``silent``     mutates, no dialog -- the loop stays responsive.
    ``modal``      pops a non-suppressible Altium dialog that BLOCKS the
                   polling loop until a human clicks (see the bridge audit).
    ``partial``    succeeds but leaves the job incomplete; a follow-up tool
                   is required to finish.
"""

from __future__ import annotations

# --- maturity tiers ---------------------------------------------------------
OFFLINE = "offline"
SIMULATOR = "simulator"
LIVE_ONLY = "live_only"
MATURITIES = (OFFLINE, SIMULATOR, LIVE_ONLY)

# --- interaction classes ----------------------------------------------------
READONLY = "readonly"
SILENT = "silent"
MODAL = "modal"
PARTIAL = "partial"
INTERACTIONS = (READONLY, SILENT, MODAL, PARTIAL)

# --- category derivation ----------------------------------------------------
# Ordered longest-prefix-first so ``application`` wins over ``app_`` etc.
_CATEGORY_BY_PREFIX = (
    ("app_", "application"),
    ("proj_", "project"),
    ("lib_", "library"),
    ("obj_", "generic"),
    ("sch_", "schematic"),
    ("pcb_", "pcb"),
    ("audit_", "audit"),
    ("design_", "design"),
    ("sim_", "simulation"),
    ("route_", "routing"),
    ("tool_", "meta"),
    ("part_", "parts"),
    ("kicad_", "kicad"),
)

# Tools every one of whose bridge commands the in-repo Altium simulator
# implements, so the whole call can be driven in CI with no Altium.
#
# This list is GROUND TRUTH, not an estimate, and is not maintained by
# hand: tests/test_simulator_maturity_is_measured.py probes the real
# simulator for every tool and fails unless this set matches EXACTLY, in
# both directions. Adding a simulator handler is what promotes a tool;
# the test tells you the new list.
#
# It replaced a per-CATEGORY guess (application / project / library /
# generic / schematic were assumed covered). That proxy was wrong on
# more than half the bridge-backed surface: 131 tools claimed coverage
# for commands the simulator answers with UNKNOWN_ACTION, and 18 it
# fully answers were published as live_only. A category is not a
# handler, and a tool is only covered when EVERY command it sends is,
# because one unanswered call ends the run just as surely.
_SIMULATOR_TOOLS = frozenset({
    "app_detach", "app_get_active_document", "app_get_version",
    "app_list_documents", "app_set_active_document",
    "audit_find_missing_decoupling",
    "audit_find_pin_net_name_mismatches",
    "audit_find_unconnected_ic_pins", "design_lint_report",
    "lib_add_footprint_arc", "lib_add_footprint_pad",
    "lib_add_footprint_pads", "lib_add_footprint_track",
    "lib_add_footprint_tracks", "lib_add_pins",
    "lib_add_symbol_rectangle", "lib_add_symbol_text",
    "lib_batch_rename", "lib_batch_set_params", "lib_create_footprint",
    "lib_create_ic_symbol", "lib_create_standard_footprint",
    "lib_create_symbol", "lib_diff_libraries",
    "lib_get_component_details", "lib_get_components",
    "lib_link_3d_model", "lib_link_footprint", "lib_search",
    "obj_create", "obj_delete", "obj_deselect_all", "obj_get_font_id",
    "obj_get_font_spec", "obj_modify", "obj_query", "obj_run_process",
    "obj_select", "obj_zoom", "pcb_bind_pad_nets",
    "pcb_build_from_project", "pcb_create_nets_from_list",
    "pcb_get_board_outline", "pcb_get_board_statistics",
    "pcb_get_components", "pcb_get_nets", "pcb_get_unrouted_nets",
    "pcb_get_vias", "pcb_move_components", "pcb_place_components",
    "pcb_place_tracks", "pcb_place_via", "pcb_run_drc",
    "proj_add_document", "proj_annotate", "proj_close", "proj_compile",
    "proj_create", "proj_cross_probe", "proj_export_bom_html",
    "proj_export_netlist", "proj_export_pdf", "proj_get_board_info",
    "proj_get_bom", "proj_get_component_info", "proj_get_focused",
    "proj_get_nets", "proj_get_parameters", "proj_get_stats",
    "proj_list_documents", "proj_open", "proj_remove_document",
    "proj_run_output", "proj_save", "proj_set_parameter",
})
# "parts" queries part providers (registries, local KiCad libraries)
# and never touches the Altium bridge, so it works with no Altium
# running, which is what "offline" means here.
_OFFLINE_CATEGORIES = frozenset({"routing", "meta", "parts"})
# pcb / audit / simulation -> live_only; design is mixed (mostly offline,
# handled by the offline classifier below).

# Tools that are pure Python regardless of their prefix (deterministic, no
# bridge round-trip). Substring match keeps the calculators in one rule.
_OFFLINE_SUBSTRINGS = ("_calc_", "calc_", "_compute_", "compute_")

# ...except calculators that read their input FROM THE BOARD. The
# substring rule above assumes a calc tool is pure maths; these query
# Altium first, so reporting them as offline sends anyone filtering for
# "works without Altium" straight into a bridge error.
_CALC_NEEDS_BRIDGE = frozenset({"pcb_calc_polygon_area"})

# design_* tools that are pure Python (per the design-agent surface: plan
# authoring, analysis, and derivation all run offline). The bridge-backed
# design tools are the explicit exceptions in _DESIGN_BRIDGE below.
_DESIGN_BRIDGE = frozenset(
    {
        "design_execute_plan",
        "design_audit_schematic",
        "design_validate",
        "design_review_snapshot",
        "design_snapshot_inventory",
        "design_datasheet_checklist",
        "design_learn_from_layout",
        # Both fetch board state over the bridge before reporting.
        "design_lint_report",
        "design_visual_review",
    }
)

# --- explicit interaction overrides (from the DelphiScript bridge audit) ----
# Only tools that are NOT plain readonly/silent under the default rules.
INTERACTION_OVERRIDES = {
    # Non-suppressible modal dialogs that block the single-threaded loop.
    "proj_sync_pcb": MODAL,          # ECO / Update-PCB dialog
    "proj_sync_schematic": MODAL,    # Update-Schematic dialog
    "pcb_add_teardrops": MODAL,      # Teardrop dialog
    "pcb_remove_teardrops": MODAL,   # Teardrop dialog
    # Succeeds but leaves the job incomplete.
    "pcb_place_components": PARTIAL,  # geometry only; needs pcb_build_from_project for nets
    # "diff" homograph: _READONLY_SUBSTRINGS carries "_diff_" for
    # COMPARISON tools (lib_diff_libraries, proj_get_differences), but
    # here it means DIFFERENTIAL. This one creates an object on the
    # board, and reporting it readonly tells anyone filtering for safe
    # operations the opposite of the truth.
    "pcb_create_diff_pair": SILENT,
    # Launches Altium's INTERACTIVE polygon mode: the user draws the
    # boundary by clicking. "silent" claimed it had completed a mutation,
    # so an agent would believe a polygon exists when none does.
    "pcb_start_polygon_placement": PARTIAL,
    # Renders the design and hands back an image; it changes nothing.
    # This one read as readonly only because it was WRONGLY classified
    # offline, and the offline->readonly shortcut carried it. Correcting
    # the maturity removed that shortcut and dropped it to "silent"
    # (mutates). State the truth explicitly rather than depending on a
    # side effect of another rule.
    "design_visual_review": READONLY,
    # part_fetch writes library files when given download_dir. The
    # "parts" category is offline, and offline falls back to READONLY,
    # which would advertise a tool that touches the filesystem as
    # read-only. Every other file-writing tool here (lib_easyeda_import,
    # lib_extract_cse_zip, proj_export_pdf, pcb_render_svg) is SILENT,
    # so match them. part_search never writes and stays readonly.
    "part_fetch": SILENT,
}

# --- explicit maturity overrides -------------------------------------------
# Populate from the Phase 1.3 nightly live-run as tools become verified.
MATURITY_OVERRIDES: dict[str, str] = {
    # Empty on purpose, and it should stay that way for SIMULATOR: that
    # tier is now derived from _SIMULATOR_TOOLS, which is measured
    # against the simulator and guarded by a test. Pinning a tool here
    # would let a claim outlive the handler that justified it, which is
    # the failure this file just came out of.
    #
    # Reserved for the Phase 1.3 nightly live-run, whose per-tool results
    # ARE external evidence and cannot be derived from the source.
}

# Readonly name markers. A tool whose name starts with / contains one of
# these does not mutate the design.
_READONLY_PREFIXES = ("get_", "list_", "find_", "query_", "audit_")
_READONLY_SUBSTRINGS = (
    "_get_", "_list_", "_find_", "_query_", "_describe_", "_preview_",
    "_review_", "_validate", "_compare_", "_calc_", "_compute_", "_snapshot",
    "_checklist", "_readiness", "_stats", "_search", "_diff_", "_report",
)


# The EDA-agnostic main-flow tools carry no prefix, so prefix derivation
# alone dropped them into "other" -- and "other" is not one of the
# categories tool_catalog documents. That made the product's own headline
# entry points invisible to a client discovering by category, which bites
# hardest under the minimal toolset where the catalog IS the interface.
_CATEGORY_BY_NAME = {
    "review_design": "core",
    "get_board_info": "core",
    "list_components": "core",
    "list_nets": "core",
    "run_drc": "core",
    "run_erc": "core",
}


def category_of(name: str) -> str:
    """Tool category from an explicit name, else the prefix.

    ``other`` only when neither matches, which a test treats as a defect
    rather than an acceptable default.
    """
    explicit = _CATEGORY_BY_NAME.get(name)
    if explicit:
        return explicit
    for prefix, cat in _CATEGORY_BY_PREFIX:
        if name.startswith(prefix):
            return cat
    return "other"


def _is_offline(name: str, category: str) -> bool:
    if category in _OFFLINE_CATEGORIES:
        return True
    if name in _CALC_NEEDS_BRIDGE:
        return False
    if any(s in name for s in _OFFLINE_SUBSTRINGS):
        return True
    if category == "design" and name not in _DESIGN_BRIDGE:
        return True
    return False


def maturity_of(name: str, category: str | None = None) -> str:
    if name in MATURITY_OVERRIDES:
        return MATURITY_OVERRIDES[name]
    category = category or category_of(name)
    if _is_offline(name, category):
        return OFFLINE
    if name in _SIMULATOR_TOOLS:
        return SIMULATOR
    return LIVE_ONLY


def interaction_of(name: str, category: str | None = None) -> str:
    if name in INTERACTION_OVERRIDES:
        return INTERACTION_OVERRIDES[name]
    category = category or category_of(name)
    if category == "audit":
        return READONLY
    if name.startswith(_READONLY_PREFIXES):
        return READONLY
    if any(s in name for s in _READONLY_SUBSTRINGS):
        return READONLY
    # Offline calculators/authoring tools don't touch Altium at all -> treat
    # as readonly with respect to the live session.
    if maturity_of(name, category) == OFFLINE:
        return READONLY
    return SILENT


def tool_metadata(name: str) -> dict[str, str]:
    """Full metadata record for one tool name."""
    category = category_of(name)
    return {
        "name": name,
        "category": category,
        "maturity": maturity_of(name, category),
        "interaction": interaction_of(name, category),
    }


def catalog(names) -> list[dict[str, str]]:
    """Metadata for many tools, sorted by (category, name)."""
    records = [tool_metadata(n) for n in names]
    records.sort(key=lambda r: (r["category"], r["name"]))
    return records

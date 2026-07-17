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
    ``simulator``  bridge-backed AND exercised by the in-repo Python Altium
                   simulator (application / project / library / generic
                   Pascal categories) in CI.
    ``live_only``  bridge-backed with NO simulator coverage (pcb / audit /
                   sim); today verified only against a real Altium session.
  This is a static starting estimate. The Phase 1.3 nightly live-run is the
  intended source of a future promotion to a ``verified`` tier; when that
  lands, record per-tool results in ``MATURITY_OVERRIDES``.

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
    ("kicad_", "kicad"),
)

# Which Pascal-dispatch category backs each tool category, and whether the
# in-repo Altium simulator implements that dispatch category. sch_ and obj_
# both dispatch to the Pascal ``generic`` category, which the simulator
# covers; pcb_ and audit_ have no simulator handler.
_SIMULATOR_COVERED_CATEGORIES = frozenset(
    {"application", "project", "library", "generic", "schematic"}
)
_OFFLINE_CATEGORIES = frozenset({"routing", "meta"})
# pcb / audit / simulation -> live_only; design is mixed (mostly offline,
# handled by the offline classifier below).

# Tools that are pure Python regardless of their prefix (deterministic, no
# bridge round-trip). Substring match keeps the calculators in one rule.
_OFFLINE_SUBSTRINGS = ("_calc_", "calc_", "_compute_", "compute_")

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
}

# --- explicit maturity overrides -------------------------------------------
# Populate from the Phase 1.3 nightly live-run as tools become verified.
MATURITY_OVERRIDES: dict[str, str] = {}

# Readonly name markers. A tool whose name starts with / contains one of
# these does not mutate the design.
_READONLY_PREFIXES = ("get_", "list_", "find_", "query_", "audit_")
_READONLY_SUBSTRINGS = (
    "_get_", "_list_", "_find_", "_query_", "_describe_", "_preview_",
    "_review_", "_validate", "_compare_", "_calc_", "_compute_", "_snapshot",
    "_checklist", "_readiness", "_stats", "_search", "_diff_", "_report",
)


def category_of(name: str) -> str:
    """Tool category from the name prefix; ``other`` if unrecognized."""
    for prefix, cat in _CATEGORY_BY_PREFIX:
        if name.startswith(prefix):
            return cat
    return "other"


def _is_offline(name: str, category: str) -> bool:
    if category in _OFFLINE_CATEGORIES:
        return True
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
    if category in _SIMULATOR_COVERED_CATEGORIES:
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

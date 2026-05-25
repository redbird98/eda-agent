# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Design-review orchestration tools.

One call to ``design_review_snapshot`` bundles 8-12 separate reads
(project info, components, nets, rules, DRC messages, unrouted nets,
sch/PCB diff, BOM, ...) into a single tool response. The response
also carries a ``_review_guidance`` block that holds the agent to
two disciplines, so every review it produces follows them instead of
relying on whatever the model happens to remember:

  - datasheet discipline: every device-function claim is grounded in
    the manufacturer datasheet, never the symbol or library metadata;
  - review-quality discipline: a finding must add analysis the raw
    tools do not -- specific, evidenced, actionable -- and is never a
    relayed ERC/DRC message nor a 'verify X' prompt handed back to
    the user.
"""

from __future__ import annotations

from typing import Any

from ..bridge import get_bridge
from .datasheet_hints import (
    DATASHEET_RULES,
    build_guidance_block,
    extract_unique_parts,
)


REVIEW_SECTIONS: dict[str, tuple[str, dict[str, Any], float]] = {
    # section_name: (command, params, timeout_seconds)
    "project_info":  ("project.get_focused",         {},  10.0),
    "project_options": ("project.get_project_options", {}, 10.0),
    "design_stats":  ("project.get_design_stats",    {},  20.0),
    "components":    ("pcb.get_components",          {},  20.0),
    "nets":          ("pcb.get_nets",                {},  10.0),
    "design_rules":  ("pcb.get_design_rules",        {},  10.0),
    "unrouted":      ("pcb.get_unrouted_nets",       {},  30.0),
    "diff":          ("project.get_design_differences", {}, 30.0),
    "messages":      ("project.get_messages",        {},  10.0),
    "board_stats":   ("pcb.get_board_statistics",    {},  10.0),
    # Slow / optional, only run on explicit request.
    "bom":           ("project.get_bom",             {},  60.0),
    "drc":           ("pcb.run_drc",                 {},  90.0),
    "erc":           ("generic.run_erc",             {},  90.0),
    "unconnected_pins": ("generic.get_unconnected_pins", {}, 60.0),
}

DEFAULT_SECTIONS = [
    "project_info",
    "design_stats",
    "components",
    "nets",
    "design_rules",
    "unrouted",
    "diff",
    "messages",
    "board_stats",
]


# ---------------------------------------------------------------------------
# Review-quality discipline.
#
# Datasheet discipline (datasheet_hints.DATASHEET_RULES) keeps device
# *facts* honest. This second discipline keeps the *findings* honest --
# it governs what may be surfaced as a review finding at all. It ships
# inside the _review_guidance block of every design_review_snapshot so
# the standard is enforced by the agent, not left to the model's recall.
#
# Each rule below is here because skipping it produced a useless review:
# relayed ERC categories the user already has in Altium's Messages panel;
# "verify X" prompts that hand the analysis back to the user; device
# functions guessed from symbol pin names; and normal design practice
# surfaced as noise.
# ---------------------------------------------------------------------------

REVIEW_DISCIPLINE: str = (
    "A finding must add analysis the raw tools do not: name the exact "
    "parts, pins and nets, back it with a netlist fact or a cited "
    "datasheet section, and state a concrete consequence and fix. Run "
    "ERC and DRC, but investigate every violation and surface only "
    "the verified-real ones as specific findings -- never relay the "
    "raw list. Never emit 'verify X' as a finding -- do the analysis. "
    "Normal design practice is not a finding."
)

REVIEW_QUALITY_RULES: list[str] = [
    "RUN ERC AND DRC, THEN TRIAGE -- never relay them, never drop "
    "them. ERC and DRC must still be run: a genuine violation can be "
    "one of the most important findings in the whole review, so a "
    "real one is never discarded. But the raw output is not the "
    "review, and much of it is false positives -- ERC routinely "
    "flags power and passive nets, unset pin electrical types, and "
    "intentional no-connects. After the datasheet-grounded part "
    "review, investigate EACH ERC/DRC violation one by one: trace it "
    "to the exact net and pins and use the datasheet understanding "
    "of the parts on that net to decide whether it is a real fault "
    "or a false positive. Surface every violation verified as real "
    "-- each as a specific finding (named net and pins, the actual "
    "electrical fault, the fix), severity-rated on its real impact. "
    "Drop the false positives. Never relay a raw category like 'N "
    "nets with floating inputs' -- the user already has Altium's "
    "Messages panel for that, and a relayed copy is worthless. The "
    "triage exists to cut false-positive noise so the genuine ERC "
    "and DRC issues stand out, not to dismiss ERC/DRC.",

    "NO HOMEWORK. Never emit 'verify X', 'confirm X' or 'check X "
    "against the datasheet' as a finding -- that hands the analysis "
    "back to the user, which is the opposite of a review. Fetch the "
    "datasheet, trace the net, and state the conclusion yourself. "
    "'Verify the decoupling' is not a finding; 'U3 pin 12 VDDA is on "
    "net +3V3 with no capacitor on that net' is.",

    "SPECIFIC, EVIDENCED, ACTIONABLE. Every finding names the exact "
    "components, pins and nets involved; is backed by either a "
    "netlist fact (stated as a netlist fact) or a datasheet section "
    "or page citation; and states the concrete electrical "
    "consequence and the concrete fix. A finding with no named parts "
    "and no citation is not a finding.",

    "TOPOLOGY vs FUNCTION. A netlist fact -- a single-pin net, a pin "
    "count, two pins shorted onto one net -- is directly observable "
    "and may be stated plainly. A device function -- what a pin "
    "does, a rating, a threshold, a default state -- is a datasheet "
    "fact and may NEVER be inferred from a symbol pin name, the "
    "Comment field, or the part number. A pin the symbol labels "
    "'OUT' is not known to be an output until the datasheet says so.",

    "NOT EVERYTHING IS A FINDING. Normal, expected design practice "
    "is noise: unused MCU GPIOs left unrouted, an unused logic "
    "output left open, declared no-connect pins. A review that is "
    "mostly noise is useless -- signal-to-noise is the metric. If "
    "you would not raise it with a senior engineer in a real design "
    "review, do not surface it.",

    "CALIBRATE SEVERITY. 'critical' and 'warning' are for real, "
    "evidenced problems that can cause a malfunction or a respin. "
    "Something you could not confirm is not 'critical'. Verified-OK "
    "and positive observations are 'info'. Do not inflate counts.",
]


def _extract_unique_parts(
    components: Any, bom: Any
) -> list[dict[str, str]]:
    """Thin wrapper around the shared extractor, kept for test compat."""
    return extract_unique_parts(components=components, bom=bom)


def _guidance_block(unique_parts: list[dict[str, str]]) -> dict[str, Any]:
    """Build the ``_review_guidance`` block.

    It carries both disciplines: the shared datasheet discipline (so
    device facts are grounded in the datasheet) and the review-quality
    discipline (so what gets surfaced as a finding is specific,
    evidenced, and adds analysis the raw sections do not).
    """
    block = build_guidance_block(unique_parts, context="design_review")
    block["__REVIEW_DISCIPLINE__"] = REVIEW_DISCIPLINE
    block["review_quality_rules"] = REVIEW_QUALITY_RULES
    return block


def register_review_tools(mcp):
    """Register design-review orchestration tools."""

    @mcp.tool()
    async def design_review_snapshot(
        sections: list[str] | None = None,
        include_bom: bool = True,
        run_drc: bool = False,
        run_erc: bool = False,
        force_recompile: bool = False,
    ) -> dict[str, Any]:
        """Fetch a comprehensive design-review snapshot in ONE tool call.

        PREFER THIS over running 8-12 individual review queries.
        A normal review (components, nets, rules, diff, messages, stats,
        unrouted, BOM) is one round-trip instead of one LLM turn per
        section. That's the single biggest time cost on a full review.

        CRITICAL, datasheet discipline (enforced via _review_guidance):
        Before drawing ANY conclusion about a component's pin
        function, voltage rating, or electrical spec, fetch the
        actual manufacturer datasheet and verify. The schematic
        symbol, footprint, and parameter fields are NOT ground
        truth - they are often wrong or outdated. Every proposed
        fix must cite the datasheet section or page you relied on.

        The response's ``_unique_parts`` field is the checklist of
        components whose datasheets you must have read before
        reviewing. Do not skip this step.

        CRITICAL, review-quality discipline (enforced via _review_guidance):
        A finding must ADD analysis the raw sections do not. Run ERC
        and DRC, but do NOT relay their raw output as findings -
        investigate each violation after the datasheet review, drop
        the false positives, and surface the real ones as specific,
        traced findings (a genuine ERC/DRC violation can be a top
        finding - never dropped). Do NOT emit "verify X" / "confirm
        X" as a finding - that hands the analysis back to the user.
        Every finding names the exact parts/pins/nets, is backed by a
        netlist fact or a cited datasheet, states a concrete
        consequence and fix, and is something you would raise in a
        real design review. The full rules are in
        _review_guidance["review_quality_rules"].

        Args:
            sections: Which snapshot sections to include. Defaults to
                the standard review set (project_info, design_stats,
                components, nets, design_rules, unrouted, diff,
                messages, board_stats). Available extras: "bom",
                "drc", "erc", "unconnected_pins", "project_options".
            include_bom: Convenience - adds "bom" to sections if True
                (default). BOM is the best source of manufacturer
                part numbers for datasheet lookup.
            run_drc: If True, runs DRC (slow, 30-90 s) and includes
                results. Off by default - assume the user already
                ran it, or run it separately when asked.
            run_erc: If True, runs ERC and includes results. Off by
                default.
            force_recompile: SaveAll + invalidate SmartCompile cache
                + recompile before gathering any sections. Use this
                when the user has been editing schematics in the
                Altium UI and you need a guaranteed-fresh netlist.
                Costs one extra ~5-10 s compile up-front.

        Returns:
            Dict with one key per requested section, plus:
              - _review_guidance: datasheet + review-quality rules
                Claude must follow when turning sections into findings
              - _unique_parts: list of {manufacturer, part_number,
                designators} to fetch datasheets for
              - _sections_failed: sections that errored (partial results
                are still usable)
        """
        bridge = get_bridge()

        # Force a fresh compile up-front if requested. Subsequent
        # SmartCompile calls inside each section will hit the newly
        # refreshed cache.
        if force_recompile:
            try:
                await bridge.send_command_async(
                    "project.force_recompile", {}, timeout=120.0
                )
            except Exception:
                # Non-fatal, individual sections still run; they'll
                # just use whatever compile state is current.
                pass

        requested = list(sections) if sections else list(DEFAULT_SECTIONS)
        if include_bom and "bom" not in requested:
            requested.append("bom")
        if run_drc and "drc" not in requested:
            requested.append("drc")
        if run_erc and "erc" not in requested:
            requested.append("erc")

        # Deduplicate while preserving order.
        seen: set[str] = set()
        ordered: list[str] = []
        for s in requested:
            if s in seen:
                continue
            seen.add(s)
            ordered.append(s)

        result: dict[str, Any] = {}
        failed: list[dict[str, str]] = []

        for section in ordered:
            spec = REVIEW_SECTIONS.get(section)
            if spec is None:
                failed.append({
                    "section": section,
                    "error": f"Unknown section '{section}'",
                })
                continue
            command, params, timeout = spec
            try:
                result[section] = await bridge.send_command_async(
                    command, params, timeout=timeout
                )
            except Exception as exc:
                failed.append({
                    "section": section,
                    "error": f"{type(exc).__name__}: {exc}",
                })

        unique_parts = _extract_unique_parts(
            result.get("components"), result.get("bom")
        )

        result["_unique_parts"] = unique_parts
        result["_review_guidance"] = _guidance_block(unique_parts)
        if failed:
            result["_sections_failed"] = failed
        result["_sections_fetched"] = [
            s for s in ordered if s in result and not s.startswith("_")
        ]

        return result

    @mcp.tool()
    async def datasheet_checklist() -> dict[str, Any]:
        """Return the datasheet-first discipline checklist for design review.

        Use this when you need the rules without pulling the full
        snapshot. The rules also ship inside the _review_guidance
        block of design_review_snapshot - no need to call this
        separately if you already ran a snapshot.

        Returns:
            Dict with datasheet_rules (list of rules) and a short
            action_required summary.
        """
        return {
            "datasheet_rules": DATASHEET_RULES,
            "action_required": (
                "For every unique manufacturer part number in the design, "
                "fetch the datasheet and verify pin function, voltage "
                "limits, and timing before proposing any fix. Library "
                "metadata is not authoritative."
            ),
        }

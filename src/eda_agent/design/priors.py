# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba
"""Apply learned placement priors as a post-Sugiyama bias.

The v1 "model" is just a dict: ``(part_role, anchor_role)`` ->
``(dx, dy, rotation)``. This module loads it from
``placement_priors.json`` (shipped in the package; see
``scripts/train/build_placement_priors.py``) and biases the post-layout
placements toward the user's recorded preferences.

Algorithm per part:
1. Find the part's anchor using the same heuristic as the learner --
   highest-pin-count netlist neighbor on a non-power, non-ground net,
   spatial-nearest fallback. Consistency between learner and applier
   matters: the prior says "this is where I want it relative to anchor
   X", so we need to look up the same X.
2. Look up the prior keyed by (part_role, anchor_role).
3. If present, snap the part's position to
   ``anchor_position + (dx, dy)`` (grid-aligned) and apply the
   rotation delta.
4. If not present, leave the placement untouched.

This is a one-pass bias, not iterative. Subsequent overlap-repair in
``compute_layout`` may push parts apart again; that's fine -- priors
are a hint, not a constraint.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from eda_agent.design.layout import PlacedPart
from eda_agent.design.plan import DesignPlan

logger = logging.getLogger("eda_agent.design.priors")


_DEFAULT_PRIORS_PATH = Path(__file__).resolve().parent / "placement_priors.json"


def _placement_priors_path() -> Path:
    """Resolve where to read priors from.

    Override path via the ``EDA_AGENT_PRIORS`` env var so callers can
    swap in a test fixture or a custom-trained model without touching
    the bundled file.
    """
    import os
    override = os.environ.get("EDA_AGENT_PRIORS")
    if override:
        return Path(override)
    return _DEFAULT_PRIORS_PATH


def load_priors(
    path: Optional[Path] = None,
) -> Optional[dict[str, dict[str, Any]]]:
    """Read the priors JSON, or fall back to canonical hand-authored ones.

    Lookup order:
      1. ``placement_priors.json`` learned from user edits (overrides built-ins).
      2. ``CANONICAL_PRIORS`` -- hand-authored, datasheet-anchored,
         shipped with the package. Always present.

    Returns the inner ``priors`` mapping keyed by ``"part_role|anchor_role"``.
    Always returns a usable dict; the canonical fallback ensures fresh
    installs get professional-looking output without needing votes.
    """
    p = path or _placement_priors_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            priors = data.get("priors")
            if isinstance(priors, dict) and priors:
                return priors
        except json.JSONDecodeError as exc:
            logger.warning("placement_priors.json at %s is corrupt: %s", p, exc)
    # Fall back to hand-authored canonical priors. These encode
    # "where this part type sits relative to its anchor" for common
    # role pairings the planner uses; lifted from datasheet typical-
    # application figures and EDA conventions.
    return dict(CANONICAL_PRIORS)


# Canonical role-pair offsets. Each entry is keyed
# "<part_role>|<anchor_role>" with (dx, dy, rotation_delta) in mils.
# Anchor convention: the highest-pin-count non-power netlist neighbor
# (same heuristic as learner._pick_anchor).
#
# These were authored from canonical EDA practice + datasheet typical-
# application circuits, NOT learned from votes. They're the "good
# default" for fresh installs.
CANONICAL_PRIORS: dict[str, dict[str, Any]] = {
    # --- IC decoupling caps (HF and bulk) ---
    "vcc_decoup|ic":   {"dx":    0, "dy":  400, "rotation": 0, "n_samples": 0,
                        "part_role": "vcc_decoup", "anchor_role": "ic"},
    "decoup_hf|ic":    {"dx":    0, "dy":  300, "rotation": 0, "n_samples": 0,
                        "part_role": "decoup_hf",  "anchor_role": "ic"},
    "decoup_bulk|ic":  {"dx":  200, "dy":  700, "rotation": 0, "n_samples": 0,
                        "part_role": "decoup_bulk","anchor_role": "ic"},
    "decoup_cap|ic":   {"dx":    0, "dy":  400, "rotation": 0, "n_samples": 0,
                        "part_role": "decoup_cap", "anchor_role": "ic"},
    # --- LED indicator chain (current-limit R + LED) ---
    "led_limit|ic":    {"dx": 1500, "dy": 1000, "rotation": 0, "n_samples": 0,
                        "part_role": "led_limit",  "anchor_role": "ic"},
    "indicator|ic":    {"dx": 2500, "dy": 1000, "rotation": 0, "n_samples": 0,
                        "part_role": "indicator",  "anchor_role": "ic"},
    "indicator|led_limit": {"dx": 1000, "dy": 0, "rotation": 0, "n_samples": 0,
                            "part_role": "indicator", "anchor_role": "led_limit"},
    # --- Feedback divider (regulator / op-amp) ---
    "fb_top|ic":       {"dx":  500, "dy": -500, "rotation": 270, "n_samples": 0,
                        "part_role": "fb_top",     "anchor_role": "ic"},
    "fb_bot|ic":       {"dx":  500, "dy":-1000, "rotation": 270, "n_samples": 0,
                        "part_role": "fb_bot",     "anchor_role": "ic"},
    # --- Power-in / power-out connectors ---
    "power_in|ic":     {"dx":-2000, "dy":    0, "rotation": 180, "n_samples": 0,
                        "part_role": "power_in",   "anchor_role": "ic"},
    "power_out|ic":    {"dx": 2000, "dy":    0, "rotation": 0, "n_samples": 0,
                        "part_role": "power_out",  "anchor_role": "ic"},
    "input_conn|ic":   {"dx":-2000, "dy":    0, "rotation": 180, "n_samples": 0,
                        "part_role": "input_conn", "anchor_role": "ic"},
    "output_conn|ic":  {"dx": 2000, "dy":    0, "rotation": 0, "n_samples": 0,
                        "part_role": "output_conn","anchor_role": "ic"},
    # --- Pullup / debounce ---
    "pullup|ic":       {"dx": -500, "dy":  500, "rotation": 270, "n_samples": 0,
                        "part_role": "pullup",     "anchor_role": "ic"},
    "debounce|ic":     {"dx": -500, "dy": -500, "rotation": 270, "n_samples": 0,
                        "part_role": "debounce",   "anchor_role": "ic"},
    # --- Filter network (R+C low-pass when anchored to upstream IC) ---
    "filter_r|ic":     {"dx":  800, "dy":    0, "rotation": 0, "n_samples": 0,
                        "part_role": "filter_r",   "anchor_role": "ic"},
    "filter_c|ic":     {"dx": 1600, "dy": -400, "rotation": 270, "n_samples": 0,
                        "part_role": "filter_c",   "anchor_role": "ic"},
    # --- Crystal load caps (symmetric) ---
    "crystal_cap_l|crystal": {"dx": -400, "dy": 0, "rotation": 270, "n_samples": 0,
                              "part_role": "crystal_cap_l","anchor_role": "crystal"},
    "crystal_cap_r|crystal": {"dx":  400, "dy": 0, "rotation": 270, "n_samples": 0,
                              "part_role": "crystal_cap_r","anchor_role": "crystal"},
}


def apply_placement_priors(
    placements: list[PlacedPart],
    plan: DesignPlan,
    priors: dict[str, dict[str, Any]],
    *,
    grid_mils: int = 100,
) -> list[PlacedPart]:
    """Shift placements toward the recorded preference for each role pair.

    Args:
        placements: Output of ``compute_layout(plan)`` -- the Sugiyama /
            force-directed placement decisions.
        plan: The plan that produced ``placements``; used to look up
            roles and pick anchors.
        priors: The ``priors`` sub-dict from ``placement_priors.json``.
        grid_mils: Snap-to-grid step for the biased positions. Altium's
            schematic grid is typically 100 mils; matching it keeps
            wires landing on grid intersections.

    Returns:
        A new list of PlacedPart objects with biased positions. The
        input is not mutated.
    """
    if not priors:
        return list(placements)

    role_by_refdes = {p.refdes: (p.role or "") for p in plan.parts}
    placement_by_refdes = {p.refdes: p for p in placements}

    # Place ICs / high-pin-count anchors first so their positions are
    # stable before passives get pinned relative to them. Anchor parts
    # are typically what other parts cluster around; biasing them risks
    # cascade movement, so we leave anchors untouched in this pass.
    pin_count_by_refdes = _pin_count_by_refdes(plan)
    anchor_set = {
        refdes
        for refdes, count in pin_count_by_refdes.items()
        if count >= 4
    }

    # Build a reverse-index: which priors apply when the part has role X?
    # Each entry is (anchor_role, prior_dict). Lets us check if ANY of the
    # roles present in the plan can serve as anchor for this part's role.
    priors_by_part_role: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for key, prior in priors.items():
        if "|" not in key:
            continue
        part_role, anchor_role = key.split("|", 1)
        priors_by_part_role.setdefault(part_role, []).append((anchor_role, prior))

    # Build a map of role -> highest-pin-count refdes with that role.
    # When applying a canonical prior, we anchor against the part with
    # the matching role (not the heuristic learner-anchor).
    refdes_by_role: dict[str, list[str]] = {}
    for refdes, role in role_by_refdes.items():
        if role:
            refdes_by_role.setdefault(role, []).append(refdes)

    # Per-(role, anchor) instance index so multiple same-role parts
    # (C1, C2, C3 all "decoup_cap") don't land on top of each other.
    # Each subsequent instance gets a small additional offset.
    instance_idx: dict[tuple[str, str], int] = {}

    out: list[PlacedPart] = []
    for placement in placements:
        if placement.refdes in anchor_set:
            out.append(placement)
            continue
        role = role_by_refdes.get(placement.refdes, "")
        if not role:
            out.append(placement)
            continue
        candidates = priors_by_part_role.get(role, [])
        # Find the first canonical prior whose anchor_role exists in the plan.
        chosen_anchor_refdes: Optional[str] = None
        chosen_prior: Optional[dict[str, Any]] = None
        for anchor_role, prior in candidates:
            anchor_refdes_list = refdes_by_role.get(anchor_role, [])
            if not anchor_refdes_list:
                continue
            # Pick the highest-pin-count match (so "ic" anchors to the
            # actual IC, not some other thing tagged with role=ic).
            chosen_anchor_refdes = max(
                anchor_refdes_list,
                key=lambda r: pin_count_by_refdes.get(r, 0),
            )
            chosen_prior = prior
            break
        if chosen_anchor_refdes is None or chosen_prior is None:
            out.append(placement)
            continue
        anchor_placement = placement_by_refdes.get(chosen_anchor_refdes)
        if anchor_placement is None:
            out.append(placement)
            continue
        # Per-instance offset: if there are 3 decoup caps anchored to U1,
        # space them out so they don't overlap. Step the second and
        # subsequent instances along the prior's primary axis.
        key = (role, chosen_anchor_refdes)
        idx = instance_idx.get(key, 0)
        instance_idx[key] = idx + 1
        dx = int(chosen_prior.get("dx", 0))
        dy = int(chosen_prior.get("dy", 0))
        # Step perpendicular to the primary axis to avoid stacking.
        if idx > 0:
            if abs(dx) >= abs(dy):
                # Primary axis is X; step in Y.
                dy += idx * 400 * (1 if idx % 2 else -1)
            else:
                dx += idx * 400 * (1 if idx % 2 else -1)
        new_x = anchor_placement.x_mils + dx
        new_y = anchor_placement.y_mils + dy
        new_x = (new_x // grid_mils) * grid_mils
        new_y = (new_y // grid_mils) * grid_mils
        new_rot = (placement.rotation + int(chosen_prior.get("rotation", 0))) % 360
        out.append(PlacedPart(
            refdes=placement.refdes,
            sheet=placement.sheet,
            x_mils=new_x,
            y_mils=new_y,
            rotation=new_rot,
        ))
    return out


def _pin_count_by_refdes(plan: DesignPlan) -> dict[str, int]:
    counts: dict[str, int] = {}
    for net in plan.nets:
        for pin_ref in net.pins:
            counts[pin_ref.refdes] = counts.get(pin_ref.refdes, 0) + 1
    return counts


def _pick_anchor(
    moved_refdes: str,
    plan: DesignPlan,
    placement_by_refdes: dict[str, PlacedPart],
) -> Optional[str]:
    """Same anchor heuristic as ``learner._pick_anchor``.

    Duplicated rather than imported to keep ``priors`` free of any
    learner-side imports (which would pull in os-environ, file I/O,
    etc.). Kept in sync by docstring + unit test parity.
    """
    candidates = {p.refdes for p in plan.parts if p.refdes != moved_refdes}
    placed_candidates = candidates & placement_by_refdes.keys()
    if not placed_candidates:
        return None

    moved_nets = {
        net.name for net in plan.nets
        if not (net.is_power or net.is_ground)
        for pin_ref in net.pins
        if pin_ref.refdes == moved_refdes
    }
    shared_count: dict[str, int] = {}
    if moved_nets:
        for net in plan.nets:
            if net.is_power or net.is_ground:
                continue
            if net.name not in moved_nets:
                continue
            for pin_ref in net.pins:
                if pin_ref.refdes == moved_refdes:
                    continue
                if pin_ref.refdes not in placed_candidates:
                    continue
                shared_count[pin_ref.refdes] = (
                    shared_count.get(pin_ref.refdes, 0) + 1
                )

    if shared_count:
        pin_counts = _pin_count_by_refdes(plan)
        best = max(
            shared_count.items(),
            key=lambda kv: (kv[1], pin_counts.get(kv[0], 0)),
        )
        return best[0]

    moved_p = placement_by_refdes.get(moved_refdes)
    if moved_p is None:
        return None
    mx, my = moved_p.x_mils, moved_p.y_mils
    best_refdes: Optional[str] = None
    best_d = 10**9
    for other in placed_candidates:
        op = placement_by_refdes.get(other)
        if op is None:
            continue
        d = abs(op.x_mils - mx) + abs(op.y_mils - my)
        if d < best_d:
            best_d = d
            best_refdes = other
    return best_refdes

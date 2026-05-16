# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba
"""Tests for the v1 priors aggregator + apply pass.

The aggregator (``scripts/train/build_placement_priors.py``) is exercised
via its ``aggregate`` function so we don't need argv plumbing.

Apply pass (``design.priors.apply_placement_priors``) is tested with
hand-built placements + priors dicts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from eda_agent.design.layout import PlacedPart
from eda_agent.design.plan import DesignPlan
from eda_agent.design.priors import (
    _pick_anchor,
    apply_placement_priors,
    load_priors,
)

# Add the train script to sys.path so we can import its aggregate fn.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "train"))
from build_placement_priors import aggregate, _mode_or_zero  # noqa: E402


_LIB = "/fake/lib.SchLib"


def _make_plan(parts: list[dict], nets: list[dict]) -> DesignPlan:
    return DesignPlan.model_validate({
        "spec": "x", "summary": "x",
        "sheets": [{"name": "main", "size": "A4"}],
        "parts": parts,
        "nets": nets,
    })


# ----------------------------- aggregator -----------------------------


def test_aggregate_collapses_role_pair_to_median():
    """Multiple edits for the same (part_role, anchor_role) pair should
    collapse to one entry with the MEDIAN (dx, dy) and the modal rotation."""
    rows = [
        {"part_role": "decoup_cap", "anchor_role": "ic",
         "dx_mils": 100, "dy_mils": 400, "rot_delta_deg": 0},
        {"part_role": "decoup_cap", "anchor_role": "ic",
         "dx_mils": 200, "dy_mils": 400, "rot_delta_deg": 0},
        {"part_role": "decoup_cap", "anchor_role": "ic",
         "dx_mils": 150, "dy_mils": 400, "rot_delta_deg": 90},
    ]
    payload = aggregate(rows, min_samples=2)
    assert payload["n_pairs"] == 1
    entry = payload["priors"]["decoup_cap|ic"]
    assert entry["dx"] == 150  # median of [100, 200, 150]
    assert entry["dy"] == 400
    assert entry["rotation"] == 0  # mode (2 of 3 are 0)
    assert entry["n_samples"] == 3


def test_aggregate_skips_pairs_below_min_samples():
    rows = [
        {"part_role": "loner", "anchor_role": "ic",
         "dx_mils": 100, "dy_mils": 0, "rot_delta_deg": 0},
        # Only one observation -> skipped at min_samples=2.
    ]
    payload = aggregate(rows, min_samples=2)
    assert payload["n_pairs"] == 0
    assert payload["priors"] == {}


def test_aggregate_keeps_pairs_at_min_samples_boundary():
    rows = [
        {"part_role": "r", "anchor_role": "ic",
         "dx_mils": 100, "dy_mils": 0, "rot_delta_deg": 0},
        {"part_role": "r", "anchor_role": "ic",
         "dx_mils": 100, "dy_mils": 0, "rot_delta_deg": 0},
    ]
    payload = aggregate(rows, min_samples=2)
    assert payload["n_pairs"] == 1


def test_mode_or_zero_breaks_ties_toward_zero():
    # Two values appear once each; pick the smaller-magnitude one.
    assert _mode_or_zero([0, 90]) == 0
    # Clear majority wins.
    assert _mode_or_zero([0, 0, 90, 180]) == 0
    # Equal counts for 0 and 90, pick 0 (smaller magnitude).
    assert _mode_or_zero([0, 90, 0, 90]) == 0
    # Equal-magnitude opposite signs: deterministically pick one (the
    # current impl picks -90 via lexicographic tuple comparison; either
    # answer is rotationally equivalent so we just assert non-zero).
    assert _mode_or_zero([-90, 90]) in (-90, 90)


# ------------------------------ apply pass ----------------------------


def test_apply_priors_shifts_passive_relative_to_anchor():
    """A decoup_cap with a prior of (0, 500) relative to ic should land
    500 mils above the IC, regardless of where Sugiyama put it."""
    plan = _make_plan(
        parts=[
            {"refdes": "U1", "lib_ref": "IC", "lib_path": _LIB,
             "status": "existing", "sheet": "main", "role": "ic"},
            {"refdes": "C1", "lib_ref": "CAP", "lib_path": _LIB,
             "status": "existing", "sheet": "main", "role": "decoup_cap"},
        ],
        nets=[
            {"name": "VCC", "is_power": True, "pins": [
                {"refdes": "U1", "pin": "8"},
                {"refdes": "C1", "pin": "1"}]},
            {"name": "GND", "is_ground": True, "pins": [
                {"refdes": "U1", "pin": "1"},
                {"refdes": "C1", "pin": "2"}]},
        ],
    )
    # Layout puts U1 at (4000, 5000) and C1 at (1000, 1000); prior wants
    # C1 directly above U1 by 500 mils.
    placements = [
        PlacedPart(refdes="U1", sheet="main", x_mils=4000, y_mils=5000, rotation=0),
        PlacedPart(refdes="C1", sheet="main", x_mils=1000, y_mils=1000, rotation=0),
    ]
    priors = {
        "decoup_cap|ic": {
            "dx": 0, "dy": 500, "rotation": 0,
            "n_samples": 5, "part_role": "decoup_cap", "anchor_role": "ic",
        }
    }
    out = apply_placement_priors(placements, plan, priors)
    out_by_refdes = {p.refdes: p for p in out}
    # U1 (anchor, pin_count >= 4 ... wait it only has 2 pins here, so
    # the anchor_set excludes it. Let me check the algorithm again):
    # Actually U1 has 2 pins; anchor_set requires pin_count >= 4 to
    # protect it from biasing. With 2 pins it's eligible for bias too,
    # but it has no prior keyed by "ic|<anything>" so it's untouched.
    assert out_by_refdes["U1"].x_mils == 4000
    assert out_by_refdes["U1"].y_mils == 5000
    # C1 moves to U1's position + (0, 500), snapped to 100-mil grid.
    assert out_by_refdes["C1"].x_mils == 4000
    assert out_by_refdes["C1"].y_mils == 5500


def test_apply_priors_no_priors_passthrough():
    """Empty / None priors should pass placements through unchanged."""
    plan = _make_plan(
        parts=[
            {"refdes": "R1", "lib_ref": "RES", "lib_path": _LIB,
             "status": "existing", "sheet": "main", "role": "r"},
            {"refdes": "C1", "lib_ref": "CAP", "lib_path": _LIB,
             "status": "existing", "sheet": "main", "role": "c"},
        ],
        nets=[
            {"name": "N", "pins": [
                {"refdes": "R1", "pin": "1"},
                {"refdes": "C1", "pin": "1"}]},
        ],
    )
    placements = [
        PlacedPart(refdes="R1", sheet="main", x_mils=1000, y_mils=1000, rotation=0),
        PlacedPart(refdes="C1", sheet="main", x_mils=2000, y_mils=2000, rotation=0),
    ]
    out = apply_placement_priors(placements, plan, {})
    assert out == placements


def test_apply_priors_unknown_role_pair_passthrough():
    """A part whose (part_role, anchor_role) isn't in priors stays put."""
    plan = _make_plan(
        parts=[
            {"refdes": "R1", "lib_ref": "RES", "lib_path": _LIB,
             "status": "existing", "sheet": "main", "role": "feedback_r"},
            {"refdes": "U1", "lib_ref": "IC", "lib_path": _LIB,
             "status": "existing", "sheet": "main", "role": "ic"},
        ],
        nets=[
            {"name": "OUT", "pins": [
                {"refdes": "U1", "pin": "1"},
                {"refdes": "R1", "pin": "1"}]},
            {"name": "FB", "pins": [
                {"refdes": "U1", "pin": "2"},
                {"refdes": "R1", "pin": "2"}]},
        ],
    )
    placements = [
        PlacedPart(refdes="U1", sheet="main", x_mils=4000, y_mils=5000, rotation=0),
        PlacedPart(refdes="R1", sheet="main", x_mils=1500, y_mils=1500, rotation=0),
    ]
    priors = {
        "decoup_cap|ic": {"dx": 0, "dy": 500, "rotation": 0,
                          "n_samples": 5, "part_role": "decoup_cap",
                          "anchor_role": "ic"},
    }
    out = apply_placement_priors(placements, plan, priors)
    # R1's pair is "feedback_r|ic", not in the priors -> unchanged.
    r1 = next(p for p in out if p.refdes == "R1")
    assert r1.x_mils == 1500 and r1.y_mils == 1500


def test_apply_priors_snaps_to_grid():
    """Biased positions must land on the 100-mil grid."""
    plan = _make_plan(
        parts=[
            {"refdes": "U1", "lib_ref": "IC", "lib_path": _LIB,
             "status": "existing", "sheet": "main", "role": "ic"},
            {"refdes": "C1", "lib_ref": "CAP", "lib_path": _LIB,
             "status": "existing", "sheet": "main", "role": "decoup_cap"},
        ],
        nets=[
            {"name": "SIG", "pins": [
                {"refdes": "U1", "pin": "1"},
                {"refdes": "C1", "pin": "1"}]},
        ],
    )
    placements = [
        PlacedPart(refdes="U1", sheet="main", x_mils=4023, y_mils=5067, rotation=0),
        PlacedPart(refdes="C1", sheet="main", x_mils=1000, y_mils=1000, rotation=0),
    ]
    priors = {
        "decoup_cap|ic": {
            "dx": 47, "dy": 213, "rotation": 0,  # non-grid offsets
            "n_samples": 3, "part_role": "decoup_cap", "anchor_role": "ic",
        }
    }
    out = apply_placement_priors(placements, plan, priors, grid_mils=100)
    c1 = next(p for p in out if p.refdes == "C1")
    # (4023 + 47) = 4070 -> snap to 4000
    # (5067 + 213) = 5280 -> snap to 5200
    assert c1.x_mils % 100 == 0
    assert c1.y_mils % 100 == 0


def test_load_priors_missing_file_falls_back_to_canonical(tmp_path: Path):
    """No on-disk learned priors -> falls back to CANONICAL_PRIORS shipped
    with the package (was: returned None). The canonical fallback ensures
    fresh installs still get professional-looking output."""
    priors = load_priors(tmp_path / "does_not_exist.json")
    assert priors is not None
    assert len(priors) > 0
    # Canonical priors should include at least the decoup-cap-near-IC pattern.
    assert "vcc_decoup|ic" in priors or "decoup_cap|ic" in priors


def test_load_priors_reads_priors_subdict(tmp_path: Path):
    priors_file = tmp_path / "priors.json"
    priors_file.write_text(json.dumps({
        "version": 1,
        "trained_at": "now",
        "n_edits": 5,
        "priors": {"r|ic": {"dx": 100, "dy": 0, "rotation": 0, "n_samples": 5}},
    }), encoding="utf-8")
    priors = load_priors(priors_file)
    assert priors is not None
    assert "r|ic" in priors


def test_priors_anchor_matches_learner_heuristic():
    """The apply-side anchor picker must match the learner's heuristic
    so prior keys look up the same role pair that produced them."""
    plan = _make_plan(
        parts=[
            {"refdes": "U1", "lib_ref": "IC", "lib_path": _LIB,
             "status": "existing", "sheet": "main", "role": "ic"},
            {"refdes": "R1", "lib_ref": "RES", "lib_path": _LIB,
             "status": "existing", "sheet": "main", "role": "fb_r"},
        ],
        nets=[
            {"name": "SIG", "pins": [
                {"refdes": "U1", "pin": "1"},
                {"refdes": "R1", "pin": "1"}]},
        ],
    )
    placements = {
        "U1": PlacedPart(refdes="U1", sheet="main", x_mils=4000, y_mils=5000, rotation=0),
        "R1": PlacedPart(refdes="R1", sheet="main", x_mils=1000, y_mils=1000, rotation=0),
    }
    anchor = _pick_anchor("R1", plan, placements)
    assert anchor == "U1"

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba
"""Tests for the layout-quality scorer.

The scorer is the heart of the iteration loop: it has to rank layouts
the way a human reviewer would. These tests pin the relative ordering
on specific failure modes.
"""

from __future__ import annotations

import pytest

from eda_agent.design.canvas import (
    PowerPort,
    SchematicCanvas,
    Sheet,
    SymbolInstance,
    WireSegment,
)
from eda_agent.design.quality import score_canvas
from eda_agent.design.symbols import SymbolBBox, SymbolModel, SymbolPin


@pytest.fixture(autouse=True)
def _force_heuristic_scorer(monkeypatch, tmp_path):
    """These tests assert against the HARDCODED heuristic weights;
    point EDA_AGENT_QUALITY_MODEL at a missing file so any learned
    model that happens to be on disk is ignored for this test module."""
    from eda_agent.design import quality
    monkeypatch.setenv("EDA_AGENT_QUALITY_MODEL", str(tmp_path / "no_model.json"))
    quality.reset_model_cache()
    yield
    quality.reset_model_cache()


def _passive() -> SymbolModel:
    return SymbolModel(
        lib_path="/x.SchLib", lib_ref="R",
        pins=(
            SymbolPin(designator="1", name="1", x=-100, y=0,
                      orientation=2, length=100, electrical_type="passive"),
            SymbolPin(designator="2", name="2", x=100, y=0,
                      orientation=0, length=100, electrical_type="passive"),
        ),
        body_bbox=SymbolBBox(x_min=-50, y_min=-30, x_max=50, y_max=30),
    )


def _canvas_with(*instances: SymbolInstance) -> SchematicCanvas:
    c = SchematicCanvas()
    c.add_sheet(Sheet(name="main"))
    for inst in instances:
        c.add_instance(inst)
    return c


def test_empty_canvas_scores_zero():
    score = score_canvas(SchematicCanvas())
    assert score.total == 0.0


def test_compact_layout_scores_lower_than_spread():
    """Two near-square layouts: compact one beats spread one."""
    sym = _passive()
    # Compact: parts in 600x600 bbox.
    compact = _canvas_with(
        SymbolInstance(refdes="R1", symbol=sym, x=1000, y=1000, rotation=0),
        SymbolInstance(refdes="R2", symbol=sym, x=1500, y=1000, rotation=0),
        SymbolInstance(refdes="R3", symbol=sym, x=1000, y=1500, rotation=0),
        SymbolInstance(refdes="R4", symbol=sym, x=1500, y=1500, rotation=0),
    )
    # Spread: same 4 parts but stretched 4x along y.
    spread = _canvas_with(
        SymbolInstance(refdes="R1", symbol=sym, x=1000, y=1000, rotation=0),
        SymbolInstance(refdes="R2", symbol=sym, x=1500, y=1000, rotation=0),
        SymbolInstance(refdes="R3", symbol=sym, x=1000, y=5000, rotation=0),
        SymbolInstance(refdes="R4", symbol=sym, x=1500, y=5000, rotation=0),
    )
    compact_score = score_canvas(compact)
    spread_score = score_canvas(spread)
    # Spread has worse aspect ratio + (when wires are present) longer
    # wires. Empty-wire case: only aspect_ratio_penalty differs.
    assert spread_score.aspect_ratio_penalty > compact_score.aspect_ratio_penalty


def test_wire_crossings_increase_score():
    """An extra wire crossing should bump the total badness."""
    sym = _passive()
    base = _canvas_with(
        SymbolInstance(refdes="R1", symbol=sym, x=1000, y=1000, rotation=0),
        SymbolInstance(refdes="R2", symbol=sym, x=2000, y=2000, rotation=0),
    )
    # Two non-crossing wires.
    base.add_wires([
        WireSegment(x1=900, y1=1000, x2=900, y2=2000, sheet="main", net="A"),
        WireSegment(x1=2100, y1=1000, x2=2100, y2=2000, sheet="main", net="B"),
    ])
    # Same canvas but with two crossing wires.
    crossing = _canvas_with(
        SymbolInstance(refdes="R1", symbol=sym, x=1000, y=1000, rotation=0),
        SymbolInstance(refdes="R2", symbol=sym, x=2000, y=2000, rotation=0),
    )
    crossing.add_wires([
        WireSegment(x1=500, y1=1500, x2=2500, y2=1500, sheet="main", net="A"),
        WireSegment(x1=1500, y1=500, x2=1500, y2=2500, sheet="main", net="B"),
    ])
    s_base = score_canvas(base)
    s_cross = score_canvas(crossing)
    assert s_cross.wire_crossings == 1
    assert s_base.wire_crossings == 0
    assert s_cross.total > s_base.total


def test_body_overlap_scores_strongly_penalized():
    """Overlapping bodies are an illegal layout; expect a big penalty."""
    sym = _passive()
    overlapping = _canvas_with(
        SymbolInstance(refdes="R1", symbol=sym, x=1000, y=1000, rotation=0),
        # R2 placed directly on top of R1.
        SymbolInstance(refdes="R2", symbol=sym, x=1000, y=1000, rotation=0),
    )
    s = score_canvas(overlapping)
    assert s.body_overlaps == 1
    # Overlap weight is 1000, single overlap should dominate the score.
    assert s.total >= 1000


def test_more_power_ports_scores_higher():
    """Each extra power port adds a fixed badness."""
    sym = _passive()
    one_port = _canvas_with(
        SymbolInstance(refdes="R1", symbol=sym, x=1000, y=1000, rotation=0),
    )
    one_port.add_power_ports([PowerPort(text="GND", x=900, y=800, style="gnd_power")])
    five_ports = _canvas_with(
        SymbolInstance(refdes="R1", symbol=sym, x=1000, y=1000, rotation=0),
    )
    for i in range(5):
        five_ports.add_power_ports([PowerPort(
            text="GND", x=900 + i * 100, y=800, style="gnd_power",
        )])
    assert score_canvas(five_ports).total > score_canvas(one_port).total


def test_score_breakdown_explains_total():
    """score.breakdown sums to score.total -- the dashboard expectation."""
    sym = _passive()
    canvas = _canvas_with(
        SymbolInstance(refdes="R1", symbol=sym, x=1000, y=1000, rotation=0),
        SymbolInstance(refdes="R2", symbol=sym, x=2000, y=1500, rotation=0),
    )
    canvas.add_wires([
        WireSegment(x1=900, y1=1000, x2=2100, y2=1500, sheet="main", net="A"),
    ])
    score = score_canvas(canvas)
    assert abs(sum(score.breakdown.values()) - score.total) < 1e-6

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The design report is written from the journal and from nothing else.

Its body has a fixed order, every section appears whether or not anything
was logged for it, and an empty one says so in a line: a report that drops
the stack-up section reads as a board with no stack-up question, which is
a different claim from "nobody recorded one". Gate evidence is read the
way the harness reads it, so the report cannot pass a stage the harness
sent back. All journals here are synthetic.
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest

from eda_agent.design.report import NOTHING, SECTIONS, build_report, topic_of
from eda_agent.design.session import STAGES, SessionStore


def _store(tmp_path):
    return SessionStore(tmp_path / "sessions")


def _sections(md: str) -> dict[str, str]:
    """Heading -> body, in order."""
    parts = re.split(r"^## (.+)$", md, flags=re.M)
    return {parts[i].strip(): parts[i + 1].strip() for i in range(1, len(parts), 2)}


def _full_run(tmp_path):
    j = _store(tmp_path).start("4-layer USB sensor board", session_id="full")
    for stage in STAGES:
        data = {}
        if stage == "rules_stackup":
            data = {"layers": 4, "impedance": {"usb_diff_ohms": 90, "width_mils": 7.2}}
        elif stage == "pours_tuning":
            data = {"plane_region_audit": {"counts": {"islands": 0, "pieces": 3},
                                           "nets": [{"layer": "L2", "net": "GND",
                                                     "pieces": 1, "one_piece": True}]},
                    "drc.violations": 0,
                    "pcb_calc_length_match": {"worst_skew_ps": 1.5, "skew_budget_ps": 5,
                                              "all_matched": True,
                                              "members": [{"net": "USB_P", "length_mils": 1200,
                                                           "mismatch_mils": 3, "skew_ps": 0.4,
                                                           "within_tolerance": True}]},
                    "critical_routes": [{"net": "USB_P", "length_mils": 1200, "skew_ps": 0.4,
                                         "layer_changes": 0, "return_vias": 2,
                                         "exceptions": "none"}]}
        elif stage == "verification":
            data = {"return_via_audit": {"counts": {"signal_vias": 4, "with_return": 3,
                                                    "exceptions": 1},
                                         "scope": "the nets asked for",
                                         "exceptions": [{"net": "CLK", "at": [10, 20],
                                                         "nearest_return_mils": 55.0}]},
                    "proj_run_erc": {"violation_count": 0}}
        j.stage_result(stage, "ok", verdict=f"{stage} done", data=data)
    j.note("Connector and regulator on the left edge; MCU central", topic="placement")
    j.note("Via-in-pad on U1's exposed pad, filled and capped, for heat", topic="via_in_pad")
    j.note("Buck input loop kept under 5 mm", topic="power")
    j.note("Designators outside courtyards, none under parts", topic="silkscreen")
    j.note("Buck start-up transient", topic="simulation",
           data={"simulations": [{"name": "startup", "overshoot_pct": 4.2}]})
    j.note("Chose a 4-layer stack for the USB pair")
    j.note("odd topic", topic="banana")
    j.plan_revision(2, summary="Added ESD diodes")
    return j


#: The order the report promises, written out rather than read from
#: SECTIONS: compared with SECTIONS, a reordered SECTIONS would agree
#: with itself.
ORDER = ["Placement strategy", "Stack-up and impedance", "Critical routes",
         "Power paths", "Planes and fan-out", "Silkscreen policy",
         "Verification results", "Simulations", "Open issues"]


def test_every_section_appears_in_order(tmp_path):
    md = build_report(_full_run(tmp_path))
    headings = re.findall(r"^## (.+)$", md, flags=re.M)
    # The preamble comes first, the ordered body after it.
    assert headings == ["Requirement", "Stage outcomes", "Decisions"] + ORDER
    assert [h for _, h in SECTIONS] == ORDER


def test_an_empty_section_says_nothing_was_recorded(tmp_path):
    j = _store(tmp_path).start("bare", session_id="bare")
    for stage in STAGES:
        j.stage_result(stage, "ok")
    sections = _sections(build_report(j))
    for _, heading in SECTIONS:
        assert sections[heading] == NOTHING, (heading, sections[heading])
    assert sections["Decisions"] == NOTHING
    assert sections["Requirement"] == "bare"


def test_a_fresh_session_reports_its_unreached_stages_as_open(tmp_path):
    j = _store(tmp_path).start("", session_id="fresh")
    sections = _sections(build_report(j))
    assert sections["Requirement"] == NOTHING
    assert "Stages not reached: " + ", ".join(STAGES) in sections["Open issues"]


def test_notes_land_in_the_section_their_topic_names(tmp_path):
    sections = _sections(build_report(_full_run(tmp_path)))
    assert "MCU central" in sections["Placement strategy"]
    assert "Via-in-pad on U1" in sections["Planes and fan-out"]
    assert "Buck input loop" in sections["Power paths"]
    assert "Designators outside courtyards" in sections["Silkscreen policy"]
    assert "Buck start-up transient" in sections["Simulations"]
    assert "overshoot_pct 4.2" in sections["Simulations"]
    # No topic, or one the report does not know: a decision.
    assert "Chose a 4-layer stack" in sections["Decisions"]
    assert "odd topic" in sections["Decisions"]
    assert "Plan revision 2: Added ESD diodes" in sections["Decisions"]


def test_topic_synonyms():
    assert topic_of("Via-in-pad") == "planes"
    assert topic_of("fanout") == "planes"
    assert topic_of("impedance") == "stackup"
    assert topic_of("Critical Routes") == "critical_routes"
    assert topic_of("") == "decision"
    assert topic_of("anything else") == "decision"


def test_logged_numbers_appear_where_they_belong(tmp_path):
    sections = _sections(build_report(_full_run(tmp_path)))
    stack = sections["Stack-up and impedance"]
    assert "rules_stackup done" in stack and "usb_diff_ohms 90" in stack and "layers: 4" in stack
    routes = sections["Critical routes"]
    assert "| USB_P | 1200 | 0.4 | 0 | 2 | none |" in routes
    assert "worst skew 1.5 ps against a budget of 5 ps" in routes
    assert "exception: net CLK at (10, 20), nearest return via 55.0 mil" in routes
    planes = sections["Planes and fan-out"]
    assert "GND on L2: 1 piece(s), one piece" in planes
    verification = sections["Verification results"]
    assert "Gate for pours_tuning: pass" in verification
    assert "Gate for verification: incomplete" in verification
    assert "| return_via_audit | exceptions | listed | 1 | yes |" in verification


def test_a_stage_the_harness_sent_back_is_reported_as_failing(tmp_path):
    j = _store(tmp_path).start("x", session_id="sent_back")
    for stage in STAGES[:STAGES.index("placement")]:
        j.stage_result(stage, "ok")
    j.stage_result("placement", "ok", data={"placement_audit": {
        "counts": {"overlaps": 2}, "overlaps": [{"a": "R1", "b": "R2", "at": [5, 6]}]}})
    md = build_report(j)
    sections = _sections(md)
    assert re.search(r"^\| placement \| ok \| 1 \| fail \|$", md, flags=re.M)
    assert "Gate for placement fails: placement_audit.overlaps = 2, wants == 0" \
        in sections["Open issues"]
    assert "overlap: a R1, b R2 at (5, 6)" in sections["Placement strategy"]
    assert "| placement_audit | overlaps | == 0 | 2 | NO |" in sections["Verification results"]


def test_a_blocked_run_shows_the_question_and_its_answer(tmp_path):
    j = _store(tmp_path).start("x", session_id="asked")
    j.blocked("Which connector?", stage="requirement")
    assert "Waiting on the user: Which connector?" in _sections(build_report(j))["Open issues"]
    j.resolved("USB-C")
    sections = _sections(build_report(j))
    assert "Asked: Which connector? Answered: USB-C" in sections["Decisions"]
    assert "Waiting on the user" not in sections["Open issues"]


def test_the_report_has_no_em_dash(tmp_path):
    assert chr(0x2014) not in build_report(_full_run(tmp_path))


def test_an_old_note_without_a_topic_still_reads(tmp_path):
    """A journal line written before notes had topics."""
    import json

    j = _store(tmp_path).start("x", session_id="old")
    with j.path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"seq": 2, "ts": "2026-01-01T00:00:00", "kind": "note",
                            "payload": {"text": "an old note"}}) + "\n")
    assert "an old note" in _sections(build_report(j))["Decisions"]


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------

@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.setattr("eda_agent.config.get_config",
                        lambda: SimpleNamespace(workspace_dir=tmp_path / "ws"))
    captured = {}

    class _Mcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    from eda_agent.tools.design import register_design_tools
    register_design_tools(_Mcp())
    return captured


def _run(fn, **kw):
    return asyncio.run(fn(**kw))


def test_the_tool_returns_the_report_and_writes_it_where_asked(tools, tmp_path):
    sid = _run(tools["design_session_start"], requirement="tool run")["session_id"]
    _run(tools["design_session_log"], event="note", session_id=sid, text="Silk kept off pads",
         topic="silkscreen")
    out = _run(tools["design_session_report"], session_id=sid)
    assert out["path"] == "" and "Silk kept off pads" in out["markdown"]

    target = tmp_path / "out" / "report.md"
    target.parent.mkdir()
    out = _run(tools["design_session_report"], session_id=sid, output_path=str(target))
    assert out["path"] == str(target.resolve())
    assert target.read_text(encoding="utf-8") == out["markdown"]


def test_the_tool_refuses_a_path_inside_the_package(tools):
    import eda_agent
    from pathlib import Path

    package = Path(eda_agent.__file__).resolve().parent
    _run(tools["design_session_start"], requirement="x")
    for inside in (package / "report.md", package / "design" / "report.md"):
        out = _run(tools["design_session_report"], output_path=str(inside))
        assert "error" in out and "package" in out["error"], out
        assert not inside.exists()


def test_the_tool_refuses_a_missing_folder_and_an_unknown_session(tools, tmp_path):
    _run(tools["design_session_start"], requirement="x")
    out = _run(tools["design_session_report"], output_path=str(tmp_path / "nope" / "r.md"))
    assert "error" in out and "does not exist" in out["error"]
    assert "error" in _run(tools["design_session_report"], session_id="no-such-session")

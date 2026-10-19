# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The live view follows what is written to the board, not only the jobs.

It showed the engine's jobs and nothing after them: applying a placement or
a routing to Altium left the view on the job's last frame with no sign the
board had changed, a board edited by hand could not be shown without saving
it as JSON, and a run that logged to its journal showed nothing in the
decision log unless it also called design_live_note.
"""

from __future__ import annotations

import pytest

from eda_agent.design import live
from eda_agent.design.jobs import get_job_store
from eda_agent.layout.place.job import place_job
from tests.layout.test_autoplace_tools import BOARD_FILE, _board, _Bridge, _tool


def _done_place_job():
    store = get_job_store()
    jid = store.submit("layout_place", place_job, {"board": _board()})
    assert store.wait(jid, timeout=60)
    return jid, store.get(jid).result


@pytest.mark.asyncio
async def test_an_apply_republishes_the_board_its_own_job_published(monkeypatch, tmp_path):
    # The fake bridge moves the workspace, so it is installed before the job
    # publishes: one workspace throughout, as in a real server.
    bridge = _Bridge(focused=BOARD_FILE)
    tool = _tool(monkeypatch, tmp_path, bridge, "pcb_autoplace_apply")
    jid, res = _done_place_job()
    placed_version = res["live_version"]
    assert placed_version, "the job says which version it published"
    # Something else is published in between: the apply must not show that.
    other = _board()
    other.name = "other.PcbDoc"
    live.publish(other, "another board", "manual")

    out = await tool(job_id=jid, expect_file=BOARD_FILE, checkpoint=False)

    doc = live.read_board()
    assert out["live_view"]["version"] == doc["version"] > placed_version
    assert doc["kind"] == "applied" and doc["snapshot"]["board"] == "demo.PcbDoc"
    assert doc["snapshot"] == live.snapshot(live.board_of(placed_version)[0],
                                            live.board_of(placed_version)[1])
    assert any("Applied to Altium" in d["text"] for d in live.decisions(newest=True, limit=5))


@pytest.mark.asyncio
async def test_an_apply_says_so_when_the_jobs_board_is_no_longer_held(monkeypatch, tmp_path):
    bridge = _Bridge(focused=BOARD_FILE)
    tool = _tool(monkeypatch, tmp_path, bridge, "pcb_autoplace_apply")
    jid, _ = _done_place_job()
    monkeypatch.setattr(live, "_history", {})
    out = await tool(job_id=jid, expect_file=BOARD_FILE, checkpoint=False)
    assert out["moved"] == out["expected"]
    assert out["live_view"].startswith("not updated")


def test_only_recent_versions_are_kept():
    first = live.publish(_board(), "", "manual")["version"]
    for _ in range(live.KEEP_VERSIONS):
        live.publish(_board(), "", "manual")
    assert live.board_of(first) is None
    assert live.publish_applied(first, "late") is None
    assert live.publish_applied(None, "none") is None


def _live_tools(monkeypatch):
    from eda_agent.tools import live as live_tools
    captured = {}

    class DummyMcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    live_tools.register_live_tools(DummyMcp())
    return captured


@pytest.mark.asyncio
async def test_the_board_in_altium_is_read_only_when_asked(monkeypatch):
    from eda_agent.layout import read_altium

    reads = []

    def fake_read(expect_file):
        reads.append(expect_file)
        return _board()

    monkeypatch.setattr(read_altium, "read_live_board", fake_read)
    snap = _live_tools(monkeypatch)["design_live_snapshot"]

    out = await snap(note="after a hand edit", expect_file=BOARD_FILE)
    assert reads == [BOARD_FILE] and out["board"] == "demo.PcbDoc"
    await snap(note="again")
    assert reads == [BOARD_FILE], "republishing the last board reads nothing"


@pytest.mark.asyncio
async def test_a_wrong_board_is_refused_and_nothing_is_published(monkeypatch):
    from eda_agent.layout import read_altium

    def fake_read(expect_file):
        raise read_altium.WrongBoard("the focused board is another one")

    monkeypatch.setattr(read_altium, "read_live_board", fake_read)
    before = (live.read_board() or {}).get("version")
    out = await _live_tools(monkeypatch)["design_live_snapshot"](expect_file=BOARD_FILE)
    assert "another one" in out["error"]
    assert (live.read_board() or {}).get("version") == before


@pytest.mark.asyncio
async def test_journal_entries_show_in_the_decision_log():
    from eda_agent.tools import design as design_tools
    captured = {}

    class DummyMcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    design_tools.register_design_tools(DummyMcp())
    started = await captured["design_session_start"](requirement="a test board")
    sid = started["session_id"]
    await captured["design_session_log"](event="note", session_id=sid, topic="placement",
                                         text="U1 at the board centre, its decaps west")
    await captured["design_session_log"](event="stage_result", session_id=sid,
                                         stage="placement", status="ok", text="no overlaps")
    recent = live.decisions(newest=True, limit=5)
    assert {"section": "placement", "text": "U1 at the board centre, its decaps west"}.items() \
        <= next(d for d in recent if "decaps west" in d["text"]).items()
    assert any(d["text"] == "placement: ok. no overlaps" for d in recent)


@pytest.mark.asyncio
async def test_a_routing_apply_republishes_the_routed_board(monkeypatch, tmp_path):
    from tests.layout import test_autoroute_tools as rt

    bridge = rt._Bridge(focused=rt.BOARD_FILE)
    tool = rt._apply_tool(monkeypatch, tmp_path, bridge)
    jid = rt._finished_job()
    routed_version = get_job_store().get(jid).result["live_version"]
    out = await tool(job_id=jid, expect_file=rt.BOARD_FILE, checkpoint=False)

    doc = live.read_board()
    assert out["live_view"]["version"] == doc["version"] > routed_version
    assert doc["kind"] == "applied"
    assert doc["snapshot"]["tracks"] == live.snapshot(*live.board_of(routed_version))["tracks"]
    assert any("tracks" in d["text"] and "Applied to Altium" in d["text"]
               for d in live.decisions(newest=True, limit=5))

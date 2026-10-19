# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""lib_search says what it searched and what it could not.

A user with a database library got 0 hits and no hint that the DbLib was
never read: the search walks open .SchLib files only. An empty result read
as "no such part". The reply now names the libraries searched and every
installed library it skipped, with the reason.

Installed DbLibs are searched too (library.query_dblib), by default since
the live check on AD26 passed; include_dblibs=False switches it off.
tests/test_dblib_tools.py covers the searched case.
"""

from __future__ import annotations

import pytest

from eda_agent.tools import library as library_module

OPEN_LIB = r"C:\libs\Passives.SchLib"
FREE_LIB = r"C:\libs\Spare.SchLib"
CLOSED_LIB = r"C:\libs\Closed.SchLib"
DBLIB = r"C:\libs\Company.DbLib"
INTLIB = r"C:\libs\Vendor.IntLib"
PCBLIB = r"C:\libs\Passives.PcbLib"


class _Bridge:
    def __init__(self, fail=()):
        self.calls: list[str] = []
        self.fail = set(fail)

    async def send_command_async(self, command, params=None, timeout=None):
        self.calls.append(command)
        if command in self.fail:
            raise RuntimeError("no reply")
        if command == "library.search":
            return {"query": "x", "count": 0, "results": []}
        if command == "application.get_open_documents":
            return [
                {"file_path": OPEN_LIB, "document_kind": "SCHLIB"},
                {"file_path": FREE_LIB, "document_kind": "SCHLIB"},
                {"file_path": r"C:\proj\board.PcbDoc", "document_kind": "PCB"},
            ]
        if command == "library.get_installed_libraries":
            return {"libraries": [
                {"library_path": OPEN_LIB, "library_type": "source"},
                {"library_path": CLOSED_LIB, "library_type": "source"},
                {"library_path": PCBLIB, "library_type": "source"},
                {"library_path": DBLIB, "library_type": "database"},
                {"library_path": INTLIB, "library_type": "integrated"},
            ]}
        raise AssertionError(command)


def _tool(monkeypatch, bridge):
    monkeypatch.setattr(library_module, "get_bridge", lambda: bridge)
    captured = {}

    class DummyMcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    library_module.register_library_tools(DummyMcp())
    return captured["lib_search"]


@pytest.mark.asyncio
async def test_an_empty_search_names_the_database_library_it_did_not_read(monkeypatch):
    # The DbLib's own search fails here, which is the case where it must
    # still be named as unread.
    out = await _tool(monkeypatch, _Bridge(fail={"library.query_dblib"}))(query="LM358")

    assert out["count"] == 0
    assert out["libraries_searched"] == [OPEN_LIB, FREE_LIB]
    skipped = {s["library_path"]: s for s in out["not_searched"]}
    assert set(skipped) == {DBLIB, INTLIB, CLOSED_LIB}
    assert skipped[DBLIB]["library_type"] == "database"
    assert "database" in skipped[DBLIB]["reason"]
    assert "not evidence" in out["coverage_note"] and "database" in out["coverage_note"]


@pytest.mark.asyncio
async def test_one_named_library_is_all_that_was_searched_and_nothing_else_is_asked(monkeypatch):
    bridge = _Bridge()
    out = await _tool(monkeypatch, bridge)(query="LM358", library_path=OPEN_LIB)

    assert out["libraries_searched"] == [OPEN_LIB]
    assert bridge.calls == ["library.search"]


@pytest.mark.asyncio
async def test_a_failed_coverage_read_never_fails_the_search(monkeypatch):
    out = await _tool(monkeypatch, _Bridge(fail={"library.get_installed_libraries"}))(query="LM358")

    assert out["count"] == 0 and out["results"] == []
    assert out["libraries_searched"] == [OPEN_LIB, FREE_LIB]
    assert out["not_searched"] is None and "coverage_note" not in out


@pytest.mark.asyncio
async def test_a_search_error_is_returned_as_it_was(monkeypatch):
    class _Err(_Bridge):
        async def send_command_async(self, command, params=None, timeout=None):
            self.calls.append(command)
            return {"error": "No workspace"}

    bridge = _Err()
    out = await _tool(monkeypatch, bridge)(query="LM358")
    assert out.get("error") == "No workspace"
    assert bridge.calls == ["library.search"]



@pytest.mark.asyncio
async def test_installed_dblibs_are_searched_unless_switched_off(monkeypatch):
    bridge = _Bridge()
    await _tool(monkeypatch, bridge)(query="LM358")
    assert "library.query_dblib" in bridge.calls
    bridge = _Bridge()
    out = await _tool(monkeypatch, bridge)(query="LM358", include_dblibs=False)
    assert "library.query_dblib" not in bridge.calls
    skipped = {s["library_path"]: s for s in out["not_searched"]}
    assert "include_dblibs" in skipped[DBLIB]["reason"]

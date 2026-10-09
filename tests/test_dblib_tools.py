# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The DbLib tools, and lib_search reading installed database libraries.

A user with a DbLib got 0 hits from lib_search, which read open .SchLib
files only. lib_search now searches every installed DbLib as well, and four
tools reach a DbLib directly. Driven here through a fake bridge: what each
tool sends, what it refuses before sending, and that a password in any
reply is blanked before the reply leaves the tool.
"""

from __future__ import annotations

import pytest

from eda_agent.library_db import redact_connection_text, redact_reply
from eda_agent.tools import library as library_module
from tests.test_dblib_pascal_helpers import REDACTION_CASES

OPEN_LIB = r"C:\libs\Passives.SchLib"
DBLIB = r"C:\libs\Company.DbLib"
DBLIB2 = r"C:\libs\Second.DbLib"
INTLIB = r"C:\libs\Vendor.IntLib"

_HIT = {"library_path": DBLIB, "table": "Opamps", "key": "OP-0001",
        "key_field": "Part Number", "symbol_ref": "OPAMP",
        "symbol_library": "Opamps.SchLib",
        "footprints": [{"ref": "SOIC8", "library": "IC.PcbLib"}],
        "description": "LM358 dual op-amp", "matched_field": "Description",
        "matched_value": "LM358 dual op-amp"}


def _dblib_reply(hits=(), searched=True, error=None, scan_capped=False):
    return {"query": "x", "count": len(hits), "truncated": False,
            "rows_scanned": 40, "scan_capped": scan_capped,
            "libraries": [{"library_path": DBLIB, "searched": searched,
                           "error": error, "tables_searched": ["Opamps"],
                           "tables_failed": []}],
            "results": list(hits)}


class _Bridge:
    """Answers the commands lib_search and the DbLib tools send."""

    def __init__(self, schlib_hits=0, dblibs=(DBLIB,), dblib_reply=None,
                 fail=(), reply=None, dblib_type="database"):
        self.calls: list[tuple[str, dict]] = []
        self.schlib_hits = schlib_hits
        self.dblibs = list(dblibs)
        self.dblib_type = dblib_type
        self.dblib_reply = dblib_reply if dblib_reply is not None else _dblib_reply([_HIT])
        self.fail = set(fail)
        self.reply = reply

    async def send_command_async(self, command, params=None, timeout=None):
        self.calls.append((command, dict(params or {})))
        if command in self.fail:
            raise RuntimeError("Provider=SQLOLEDB;Password=s3cr3t: login failed")
        if self.reply is not None:
            return self.reply
        if command == "library.search":
            n = self.schlib_hits
            return {"query": "x", "count": n, "truncated": False, "results": [
                {"name": f"R{i}", "alias_name": "", "description": "",
                 "library_path": OPEN_LIB, "part_count": 1} for i in range(n)]}
        if command == "application.get_open_documents":
            return [{"file_path": OPEN_LIB, "document_kind": "SCHLIB"}]
        if command == "library.get_installed_libraries":
            return {"libraries": [
                {"library_path": OPEN_LIB, "library_type": "source"},
                {"library_path": INTLIB, "library_type": "integrated"},
                *({"library_path": p, "library_type": self.dblib_type}
                  for p in self.dblibs),
            ]}
        if command == "library.query_dblib":
            return self.dblib_reply
        raise AssertionError(command)

    def sent(self, command):
        return [p for c, p in self.calls if c == command]


def _tools(monkeypatch, bridge):
    monkeypatch.setattr(library_module, "get_bridge", lambda: bridge)
    captured = {}

    class DummyMcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    library_module.register_library_tools(DummyMcp())
    return captured


# ---------------------------------------------------------------------------
# lib_search folds the installed DbLibs in
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_installed_dblib_is_searched_and_its_rows_join_the_results(monkeypatch):
    bridge = _Bridge(schlib_hits=1)
    out = await _tools(monkeypatch, bridge)["lib_search"](query="LM358", include_dblibs=True)

    (sent,) = bridge.sent("library.query_dblib")
    assert sent == {"library_path": DBLIB, "query": "LM358", "limit": "99"}
    assert out["count"] == 2
    schlib, dblib = out["results"]
    assert schlib["source"] == "schlib"
    assert dblib["source"] == "dblib" and dblib["name"] == "OP-0001"
    assert dblib["library_path"] == DBLIB and dblib["symbol_ref"] == "OPAMP"
    assert dblib["footprints"] == [{"ref": "SOIC8", "library": "IC.PcbLib"}]
    assert out["libraries_searched"] == [OPEN_LIB, DBLIB]
    assert out["libraries_searched_count"] == 2
    assert [s["library_path"] for s in out["not_searched"]] == [INTLIB]
    assert "1 open .SchLib file(s) and 1 database library" in out["coverage_note"]
    assert out["dblib_searches"][0]["rows_scanned"] == 40
    assert out["truncated"] is False


@pytest.mark.asyncio
async def test_include_dblibs_false_leaves_the_dblib_unread_and_says_so(monkeypatch):
    bridge = _Bridge()
    out = await _tools(monkeypatch, bridge)["lib_search"](query="LM358", include_dblibs=False)

    assert bridge.sent("library.query_dblib") == []
    assert out["count"] == 0
    skipped = {s["library_path"]: s for s in out["not_searched"]}
    assert "include_dblibs" in skipped[DBLIB]["reason"]
    assert out["libraries_searched"] == [OPEN_LIB]


@pytest.mark.asyncio
async def test_a_full_result_list_leaves_the_dblib_unread_rather_than_claimed(monkeypatch):
    bridge = _Bridge(schlib_hits=5)
    out = await _tools(monkeypatch, bridge)["lib_search"](query="R", limit=5, include_dblibs=True)

    assert bridge.sent("library.query_dblib") == []
    assert out["count"] == 5 and out["truncated"] is True
    skipped = {s["library_path"]: s for s in out["not_searched"]}
    assert "limit" in skipped[DBLIB]["reason"]
    assert DBLIB not in out["libraries_searched"]


@pytest.mark.asyncio
async def test_a_dblib_that_cannot_connect_stays_unsearched_with_its_code(monkeypatch):
    bridge = _Bridge(dblib_reply=_dblib_reply(searched=False, error="CONNECT_FAILED"))
    out = await _tools(monkeypatch, bridge)["lib_search"](query="LM358", include_dblibs=True)

    skipped = {s["library_path"]: s for s in out["not_searched"]}
    assert "CONNECT_FAILED" in skipped[DBLIB]["reason"]
    assert DBLIB not in out["libraries_searched"]
    assert "not evidence" in out["coverage_note"]


@pytest.mark.asyncio
async def test_a_dblib_search_that_raises_never_fails_the_search_or_leaks(monkeypatch):
    bridge = _Bridge(schlib_hits=1, fail={"library.query_dblib"})
    out = await _tools(monkeypatch, bridge)["lib_search"](query="LM358", include_dblibs=True)

    assert out["count"] == 1
    reason = {s["library_path"]: s for s in out["not_searched"]}[DBLIB]["reason"]
    assert "failed" in reason and "s3cr3t" not in reason and "***" in reason


@pytest.mark.asyncio
async def test_the_limit_is_shared_across_dblibs(monkeypatch):
    bridge = _Bridge(dblibs=(DBLIB, DBLIB2),
                     dblib_reply=_dblib_reply([_HIT, dict(_HIT, key="OP-0002")]))
    out = await _tools(monkeypatch, bridge)["lib_search"](query="op", limit=3, include_dblibs=True)

    first, second = bridge.sent("library.query_dblib")
    assert first["limit"] == "3" and second["limit"] == "1"
    assert out["count"] == 3 and out["truncated"] is True


@pytest.mark.asyncio
async def test_a_dblib_whose_type_was_lost_is_still_searched(monkeypatch):
    # Installed but absent from the available list reads library_type
    # "unknown"; the .DbLib file is still a database library.
    bridge = _Bridge(dblib_type="unknown")
    out = await _tools(monkeypatch, bridge)["lib_search"](query="LM358", include_dblibs=True)

    assert len(bridge.sent("library.query_dblib")) == 1
    assert DBLIB in out["libraries_searched"]


@pytest.mark.asyncio
async def test_library_path_naming_a_dblib_searches_that_dblib_alone(monkeypatch):
    bridge = _Bridge()
    out = await _tools(monkeypatch, bridge)["lib_search"](query="LM358", library_path=DBLIB)

    assert [c for c, _ in bridge.calls] == ["library.query_dblib"]
    assert out["libraries_searched"] == [DBLIB]
    assert out["results"][0]["source"] == "dblib"


# ---------------------------------------------------------------------------
# The direct DbLib tools
# ---------------------------------------------------------------------------

_SECRET_REPLY = {
    "library_path": DBLIB,
    "connection": {"provider": "SQLOLEDB", "kind": "sql_server",
                   "redacted": "Provider=SQLOLEDB;User ID=sa;Password=s3cr3t"},
    "tables": [{"name": "Opamps", "settings": {"Note": 'pwd="s3cr3t"'}}],
}


@pytest.mark.asyncio
async def test_info_refuses_what_is_not_a_dblib_and_sends_nothing(monkeypatch):
    bridge = _Bridge()
    tools = _tools(monkeypatch, bridge)
    for bad in ("", r"C:\libs\Passives.SchLib", r"C:\libs\Vendor.IntLib"):
        out = await tools["lib_dblib_info"](library_path=bad)
        assert out["success"] is False
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_info_sends_its_flag_and_never_returns_a_password(monkeypatch):
    bridge = _Bridge(reply=_SECRET_REPLY)
    out = await _tools(monkeypatch, bridge)["lib_dblib_info"](
        library_path=DBLIB, with_fields=False)

    assert bridge.calls == [("library.get_dblib_info",
                             {"library_path": DBLIB, "with_fields": "false"})]
    assert "s3cr3t" not in repr(out)
    assert out["connection"]["redacted"].endswith("Password=***")
    assert out["tables"][0]["name"] == "Opamps"


@pytest.mark.asyncio
async def test_every_dblib_tool_redacts_its_reply(monkeypatch):
    bridge = _Bridge(reply=_SECRET_REPLY)
    tools = _tools(monkeypatch, bridge)
    outs = [
        await tools["lib_dblib_search"](query="x"),
        await tools["lib_dblib_get_record"](library_path=DBLIB, table="T", key="K"),
        await tools["sch_place_dblib_component"](
            library_path=DBLIB, table="T", key="K", x=100, y=200),
    ]
    for out in outs:
        assert "s3cr3t" not in repr(out)


@pytest.mark.asyncio
async def test_search_sends_columns_and_refuses_what_it_cannot_send(monkeypatch):
    bridge = _Bridge(reply={"count": 0, "results": []})
    search = _tools(monkeypatch, bridge)["lib_dblib_search"]

    await search(query="10k", library_path=DBLIB, table="Resistors",
                 fields=["Value", "Manufacturer Part Number"], limit=7, max_rows=500)
    assert bridge.calls[-1] == ("library.query_dblib", {
        "query": "10k", "limit": "7", "max_rows": "500", "library_path": DBLIB,
        "table": "Resistors", "fields": "Value|Manufacturer Part Number"})

    n = len(bridge.calls)
    assert (await search(query=" "))["success"] is False
    assert (await search(query="x", fields=["a|b"]))["success"] is False
    assert (await search(query="x", fields=[""]))["success"] is False
    assert (await search(query="x", limit=0))["success"] is False
    assert (await search(query="x", limit=1001))["success"] is False
    assert (await search(query="x", max_rows=0))["success"] is False
    assert (await search(query="x", library_path=OPEN_LIB))["success"] is False
    assert len(bridge.calls) == n


@pytest.mark.asyncio
async def test_record_and_place_send_what_the_handlers_read(monkeypatch):
    bridge = _Bridge(reply={"found": True})
    tools = _tools(monkeypatch, bridge)

    await tools["lib_dblib_get_record"](library_path=DBLIB, table="Opamps",
                                        key="OP-0001", key_field="Corp PN")
    assert bridge.calls[-1] == ("library.get_dblib_record", {
        "library_path": DBLIB, "table": "Opamps", "key": "OP-0001",
        "key_field": "Corp PN"})

    await tools["sch_place_dblib_component"](
        library_path=DBLIB, table="Opamps", key="OP-0001", x=1000, y=2000,
        rotation=90, designator="U7", sheet_path=r"C:\p\main.SchDoc")
    assert bridge.calls[-1] == ("library.place_dblib_component", {
        "library_path": DBLIB, "table": "Opamps", "key": "OP-0001",
        "x": "1000", "y": "2000", "rotation": "90", "designator": "U7",
        "sheet_path": r"C:\p\main.SchDoc"})

    n = len(bridge.calls)
    assert (await tools["sch_place_dblib_component"](
        library_path=DBLIB, table="Opamps", key="OP-0001", x=0, y=0,
        rotation=45))["success"] is False
    assert (await tools["lib_dblib_get_record"](
        library_path=DBLIB, table="", key="K"))["success"] is False
    assert (await tools["lib_dblib_get_record"](
        library_path=DBLIB, table="T", key=" "))["success"] is False
    assert len(bridge.calls) == n


# ---------------------------------------------------------------------------
# The Python redactor, on the strings the Pascal one faces
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("conn,gone,kept", REDACTION_CASES)
def test_the_python_redactor_blanks_every_password(conn, gone, kept):
    out = redact_connection_text(conn)
    for frag in gone:
        assert frag not in out, f"{frag!r} survived in {out!r}"
    for frag in kept:
        assert frag in out, f"{frag!r} was lost from {out!r}"


def test_redaction_reaches_nested_values_and_leaves_keys_alone():
    reply = {"a": ["x", {"b": "User=u;Pwd=hunter2;"}], "Password=k": 3,
             "t": ("token = abc",)}
    out = redact_reply(reply)
    assert "hunter2" not in repr(out) and "abc" not in repr(out)
    assert out["Password=k"] == 3
    assert redact_connection_text("Password=***") == "Password=***"


@pytest.mark.asyncio
async def test_a_database_librarys_count_is_reported_unknown_not_as_altiums_number(monkeypatch):
    # MEASURED: Altium counted a five-row DbLib as 0, then 1.
    class _B:
        async def send_command_async(self, command, params=None, timeout=None):
            return {"libraries": [
                {"library_path": DBLIB, "library_type": "database", "component_count": 1},
                {"library_path": INTLIB_PATH, "library_type": "integrated", "component_count": 7}]}
    out = await _tools(monkeypatch, _B())["lib_get_installed_libraries"](with_counts=True)
    by = {lib["library_path"]: lib for lib in out["libraries"]}
    assert by[DBLIB]["component_count"] is None and "lib_dblib_search" in by[DBLIB]["count_note"]
    assert by[INTLIB_PATH]["component_count"] == 7


INTLIB_PATH = r"C:\libs\Vendor.IntLib"

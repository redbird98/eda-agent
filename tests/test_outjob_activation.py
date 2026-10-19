# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""An OutJob must be the ACTIVE document before its container is run.

Measured on AD25 25.4.2.15 zh-CN: an OutJob that was open (even focused
with IServerDocument.Focus) but not the active document ran its PDF
container cleanly and wrote zero files. Calling ShowDocument, which is
what app_set_active_document does, made the same run write the PDF.

Proj_RunOutJob used to call only OutJobDoc.Focus. It now calls
Client.ShowDocument first. When nothing is written anyway, the Python
judge names the active-document cause instead of blaming only the
OutJob's output switches.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from eda_agent.tools import project as project_mod

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "altium"


def _routine(unit: str, name: str) -> str:
    code = (SCRIPTS / unit).read_text(encoding="utf-8", errors="replace")
    start = re.search(rf"(?mi)^(?:Function|Procedure) {name}\b", code)
    assert start, f"missing routine {name}"
    end = re.search(r"(?m)^End;", code[start.start():])
    assert end, f"routine {name} has no top-level end"
    return code[start.start():start.start() + end.end()]


def test_proj_run_outjob_activates_the_outjob_before_running():
    body = _routine("Project.pas", "Proj_RunOutJob")
    show = body.index("Client.ShowDocument(OutJobDoc)")
    run = body.index("RunProcess('WorkspaceManager:")
    assert show < run, "the OutJob must be shown before the container runs"
    assert "Client.OpenDocument('OUTPUTJOB'" in body


def test_zero_files_names_the_active_document_cause(monkeypatch, tmp_path):
    """The judge must say what to check, not only that nothing appeared."""
    captured: dict = {}

    class FakeBridge:
        async def send_command_async(self, command, params=None, timeout=None):
            return {
                "container_name": params.get("container_name", ""),
                "container_type": "Publish",
                "output_dir": str(tmp_path),
                "process_issued": True,
            }

    monkeypatch.setattr(project_mod, "get_bridge", lambda: FakeBridge())

    class DummyMcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

        def __getattr__(self, name):
            return lambda *a, **k: (lambda fn: fn)

    project_mod.register_project_tools(DummyMcp())

    out = asyncio.run(captured["proj_run_outjob"](container_name="PDF"))
    assert out["success"] is False
    assert "ACTIVE document" in out["reason"]
    assert "app_set_active_document" in out["reason"]

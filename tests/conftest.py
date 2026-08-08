# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Shared fixtures for DelphiScript logic tests and end-to-end integration tests.

These tests validate the PURE LOGIC portions of the EDA Agent DelphiScript code
by reimplementing them in Python and testing against identical inputs/outputs.
Any divergence between the Python reimplementation and expected behavior IS a bug.
"""

import inspect
import json
import pytest
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

from tests.altium_simulator import AltiumSimulator, SIM_PROTOCOL_VERSION


# Binary Altium fixtures that stay local-only: they are large binaries that
# also embed a local machine's absolute paths, so they are deliberately not
# committed. Tests that need them run wherever the file is present (a dev box)
# and skip where it is absent (CI, a fresh clone). The skip is applied
# automatically below so individual tests do not each need a guard.
_FIXTURE_DIR = Path(__file__).resolve().parent / "integration" / "fixtures"
_LOCAL_ONLY_FIXTURES = ("main.SchDoc", "EDAAgentTest.PcbDoc",
                        "EDAAgentTest_ICs.SchLib", "EDAAgentTest_Export.OutJob")
# Committed fixtures that are only useful together with a local-only one: the
# project files aggregate main.SchDoc, so a project test needs it too.
_AGGREGATOR_FIXTURES = ("EDAAgentTest.PrjPcb", "EDAAgentTest.PrjPcbStructure")


def pytest_collection_modifyitems(config, items):
    """Skip tests that need a local-only binary fixture when it is absent."""
    missing = [n for n in _LOCAL_ONLY_FIXTURES if not (_FIXTURE_DIR / n).exists()]
    if not missing:
        return
    aggregators_broken = "main.SchDoc" in missing
    skip = pytest.mark.skip(
        reason="needs a local-only binary Altium fixture not committed to the "
               "repo (" + ", ".join(missing) + ")")
    for item in items:
        func = getattr(item, "function", None)
        mod = getattr(item, "module", None)
        if func is None or mod is None:
            continue
        try:
            src = inspect.getsource(func)
        except (OSError, TypeError):
            continue
        needs = False
        # A module-level Path variable (FIXTURE, SCH, PRJ, LIB, ...) that points
        # at a fixtures file which is now missing, or at a project aggregator
        # whose sheet is missing, and that the test actually references.
        for var, val in vars(mod).items():
            if not isinstance(val, Path):
                continue
            if "fixtures" not in str(val).replace("\\", "/"):
                continue
            broken = (not val.exists()) or (
                aggregators_broken and val.name in _AGGREGATOR_FIXTURES)
            if broken and re.search(r"\b" + re.escape(var) + r"\b", src):
                needs = True
                break
        # A test that builds a fixtures path inline (the "fixtures" segment plus
        # a missing filename appear in its own source). The "fixtures" check
        # keeps tests that make a same-named file in a temp dir from matching.
        if not needs and "fixtures" in src and any(n in src for n in missing):
            needs = True
        if needs:
            item.add_marker(skip)


@pytest.fixture
def workspace_dir():
    """Create a temporary workspace directory for IPC tests."""
    with tempfile.TemporaryDirectory(prefix="eda_agent_test_") as tmpdir:
        yield Path(tmpdir)


def request_path_for(workspace_dir: Path, request_id: str) -> Path:
    """Path to request_<id>.json in the workspace."""
    return workspace_dir / f"request_{request_id}.json"


def response_path_for(workspace_dir: Path, request_id: str) -> Path:
    """Path to response_<id>.json in the workspace."""
    return workspace_dir / f"response_{request_id}.json"


@pytest.fixture
def altium_sim(tmp_path):
    """Start an AltiumSimulator pointing at a temp workspace directory."""
    sim = AltiumSimulator(str(tmp_path))
    sim.start()
    yield sim
    sim.stop()


@pytest.fixture
def e2e_bridge(altium_sim):
    """Create a real AltiumBridge wired to the simulator's workspace.

    Calls the real ``AltiumBridge()`` constructor so every instance attribute
    is initialized identically to production. Bypassing ``__init__`` via
    ``__new__`` was the source of repeated test breakage as bridge internals
    evolved; don't reintroduce that pattern.
    """
    from eda_agent.config import AltiumConfig, MCPRuntimeConfig
    from eda_agent.bridge.altium_bridge import AltiumBridge

    test_config = AltiumConfig(
        workspace_dir=altium_sim.workspace_dir,
        runtime=MCPRuntimeConfig(
            py_poll_interval_seconds=0.01,
            py_poll_timeout_seconds=5.0,
        ),
    )

    class FakeProcessManager:
        def is_altium_running(self):
            return True

        def get_altium_info(self):
            from eda_agent.bridge.process_manager import AltiumProcessInfo
            return AltiumProcessInfo(pid=12345, name="X2.exe", exe_path="C:\\X2.exe")

    with patch("eda_agent.bridge.altium_bridge.get_config", return_value=test_config):
        bridge = AltiumBridge()

    bridge.process_manager = FakeProcessManager()
    bridge._attached = True
    yield bridge
    try:
        bridge.detach()
    except Exception:
        pass


def write_request(workspace: Path, request_id: str, command: str, params: dict) -> Path:
    """Write a request_<id>.json file in the exact format the bridge uses.

    Returns the path of the written file.
    """
    data = {
        "protocol_version": SIM_PROTOCOL_VERSION,
        "id": request_id,
        "command": command,
        "params": params,
    }
    path = request_path_for(workspace, request_id)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def write_response(workspace: Path, request_id: str, success: bool,
                   data=None, error=None) -> Path:
    """Write a response_<id>.json file in the exact format Altium produces."""
    resp = {
        "protocol_version": SIM_PROTOCOL_VERSION,
        "id": request_id,
        "success": success,
        "data": data,
        "error": error,
    }
    path = response_path_for(workspace, request_id)
    path.write_text(json.dumps(resp), encoding="utf-8")
    return path


def parse_response(path: Path) -> dict:
    """Read and parse a response_<id>.json file."""
    return json.loads(path.read_text(encoding="utf-8"))


def validate_success_response(resp: dict, request_id: str) -> None:
    """Assert that a response dict is a valid success response."""
    assert resp["id"] == request_id
    assert resp["success"] is True
    assert resp["error"] is None
    assert resp.get("protocol_version") == SIM_PROTOCOL_VERSION


def validate_error_response(resp: dict, request_id: str,
                            expected_code: str = None) -> None:
    """Assert that a response dict is a valid error response."""
    assert resp["id"] == request_id
    assert resp["success"] is False
    assert resp["data"] is None
    assert resp["error"] is not None
    assert "code" in resp["error"]
    assert "message" in resp["error"]
    if expected_code:
        assert resp["error"]["code"] == expected_code

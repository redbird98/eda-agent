# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Shared fixtures for DelphiScript logic tests and end-to-end integration tests.

These tests validate the PURE LOGIC portions of the EDA Agent DelphiScript code
by reimplementing them in Python and testing against identical inputs/outputs.
Any divergence between the Python reimplementation and expected behavior IS a bug.
"""

import inspect
import os
import json
import pytest
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

from tests.altium_simulator import AltiumSimulator, SIM_PROTOCOL_VERSION


#: Bridge-command verb prefixes that change the design. Defined once
#: here because two guards classify commands with it and a second copy
#: drifts: test_maturity_matches_reality checks that no readonly tool
#: issues one, and test_integration_suite_is_non_destructive checks that
#: no live-Altium test sends one. They must agree on what "changes
#: something" means or the two claims stop lining up.
MUTATING_COMMAND_VERBS = (
    "place_", "set_", "add_", "delete_", "remove_", "create_", "modify_",
    "move_", "clear_", "apply_", "rename_", "batch_", "update_", "install_",
    "link_", "import_", "save_", "fix_", "repair_", "convert_", "split_",
)


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.005,
               message: str = "") -> None:
    """Block until ``predicate()`` is true, or fail after ``timeout``.

    Replaces ``time.sleep(0.1); assert condition``. That pattern races
    the simulator's polling thread: it passes on an idle machine and
    fails when the thread does not get scheduled inside the fixed
    window. It cost a full-suite run here, where
    ``test_bad_request_still_removed`` failed at 94% under load and then
    passed in isolation, which is the least useful kind of failure.

    Waiting on the condition instead is both faster in the normal case
    (it returns as soon as the poller acts, typically well under the old
    sleep) and only fails after a timeout long enough that a failure
    means the behaviour is actually wrong.
    """
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return
        _time.sleep(interval)
    raise AssertionError(
        message or f"condition not met within {timeout}s")


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
    """Start an AltiumSimulator pointing at a temp workspace directory.

    The simulator's poll loop swallows every exception so one bad request
    cannot kill the daemon thread and silence the rest of the run. It
    records them on ``sim.errors`` instead, and the docstring there
    promises that gives "the next failure something to say beyond 'no
    response'" -- but nothing read the list, so a swallowed crash still
    surfaced only as a bridge timeout, which is indistinguishable from a
    slow machine.

    Reading it here is what makes that promise real: a request handler
    that raises now fails the test that caused it, with the traceback.
    """
    sim = AltiumSimulator(str(tmp_path))
    sim.start()
    yield sim
    sim.stop()
    if sim.errors:
        raise AssertionError(
            f"the simulator's poll loop swallowed {len(sim.errors)} "
            f"exception(s); any timeout in this test is a consequence, "
            f"not a slow machine:\n\n" + "\n\n".join(sim.errors[:3]))


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


@pytest.fixture(autouse=True, scope="session")
def _isolate_workspace_pointer(tmp_path_factory):
    r"""Keep the whole test session away from the machine-global pointer.

    ``C:\ProgramData\eda-agent\workspace-path.txt`` tells a running
    Altium script which directory to poll, and the script reads it once at
    startup. A test that writes its tmp workspace there silently redirects
    a live polling loop at a throwaway folder: the script keeps reporting
    healthy with zero requests while every real call times out.

    ``config.write_workspace_pointer`` already refuses to touch the real
    path under pytest; this fixture is the second layer, so any code path
    that reaches the pointer another way still lands on a scratch file.
    """
    scratch = tmp_path_factory.mktemp("pointer") / "workspace-path.txt"
    os.environ["EDA_AGENT_POINTER_FILE"] = str(scratch)
    yield
    os.environ.pop("EDA_AGENT_POINTER_FILE", None)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "network: test genuinely needs the internet; exempt from the "
        "session-wide block. Use sparingly, it makes CI depend on a "
        "third party being reachable.")
    config.addinivalue_line(
        "markers",
        "kicad_libs: test reads the KiCad symbol/footprint libraries "
        "installed on this machine. Skipped when KiCad is absent, so CI "
        "stays green without it; the value is checking a converter "
        "against a real corpus instead of only hand-written fixtures.")


@pytest.fixture(autouse=True)
def _no_network(request):
    """Fail loudly on any unstubbed network access.

    Verified before adding: the whole suite passes with urllib blocked
    and makes ZERO attempts, so nothing legitimate is being taken away.

    The failure this prevents is a slow one. A test that quietly starts
    fetching passes on the author's machine and later fails as an
    unexplained TIMEOUT for someone behind a proxy, or whenever the
    upstream service is down -- with nothing in the message pointing at
    the network as the cause.

    Opt out with ``@pytest.mark.network`` if a test truly needs it.
    """
    if request.node.get_closest_marker("network"):
        yield
        return

    import urllib.request

    real_urlopen = urllib.request.urlopen
    real_open = urllib.request.OpenerDirector.open

    def deny(*args, **kwargs):
        target = str(args[0])[:120] if args else "?"
        raise AssertionError(
            f"network access attempted ({target}); stub the fetch helper "
            f"instead, or mark the test @pytest.mark.network")

    urllib.request.urlopen = deny
    urllib.request.OpenerDirector.open = deny
    try:
        yield
    finally:
        urllib.request.urlopen = real_urlopen
        urllib.request.OpenerDirector.open = real_open


@pytest.fixture(scope="session", autouse=True)
def _close_leftover_event_loop():
    """Close the event loop pytest-asyncio leaves behind at session end.

    pytest_asyncio's ``_provide_clean_event_loop`` creates a fresh loop
    during fixture teardown and sets it current without ever closing it.
    Python then collects it at interpreter shutdown, where __del__ closes
    its self-pipe socket AFTER Windows has run WSACleanup, printing an
    ignored "OSError: [WinError 10093]".

    Nothing fails because of it, but a traceback in the teardown output
    of every green run teaches people to skim past tracebacks, which is
    the actual cost.
    """
    yield
    import asyncio

    policy = asyncio.get_event_loop_policy()
    try:
        # Read the stored loop WITHOUT get_event_loop(), which would
        # create one and reintroduce exactly what we are cleaning up.
        loop = getattr(policy, "_local", None)
        loop = getattr(loop, "_loop", None)
    except Exception:
        return
    if loop is not None and not loop.is_closed():
        loop.close()


class RecordingBridge:
    """A bridge that accepts every command and remembers it.

    Lets a generated Altium plan be driven through the real tool
    coroutines with no Altium and no simulator. Signature checks prove
    argument NAMES exist; this proves the VALUES survive the tool body,
    which is where a payload builder quietly drops geometry.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def send_command(self, command, params=None, **kwargs):
        self.calls.append((command, dict(params or {})))
        return {"success": True, "result": {}}

    async def send_command_async(self, command, params=None, **kwargs):
        return self.send_command(command, params, **kwargs)


@pytest.fixture
def altium_tool_harness(monkeypatch):
    """Every library/application tool wired to one recording bridge.

    Shared by the plan-execution tests for each importer, so a fix to
    the harness covers all of them rather than one copy of it.

    Returns ``(tools_by_name, bridge)``.
    """
    from eda_agent.tools import application as app_mod
    from eda_agent.tools import library as lib_mod

    bridge = RecordingBridge()
    for mod in (app_mod, lib_mod):
        monkeypatch.setattr(mod, "get_bridge", lambda: bridge)

    captured: dict = {}

    class _Capture:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

        def prompt(self, *a, **k):
            return self.tool()

        def resource(self, *a, **k):
            return self.tool()

    app_mod.register_application_tools(_Capture())
    app_mod.register_meta_tools(_Capture())
    lib_mod.register_library_tools(_Capture())
    return captured, bridge


def pytest_sessionfinish(session, exitstatus):
    """Finalize orphaned event loops while Windows sockets still work.

    An event loop that becomes unreachable without being closed is
    finalized whenever the collector happens to reach it. Left to
    interpreter shutdown that is AFTER Windows has run WSACleanup, so
    BaseEventLoop.__del__ closing its self-pipe raises, and every full
    run ends with an ignored "OSError: [WinError 10093]" printed under a
    green summary. Stderr noise on a passing suite is how people learn
    to stop reading stderr.

    Collecting here runs those same __del__ methods at a point where the
    sockets are still valid, so they close quietly. Nothing is
    suppressed: a loop that is still REACHABLE is untouched and would
    still be reported.

    Measured with an instrumented run: 614 loops constructed across the
    suite, 0 still open at this point, i.e. the ones that error at
    shutdown are unreachable-but-not-yet-collected rather than leaked by
    a fixture. The prior rate was roughly 4 runs in 6, so a single clean
    run is not proof on its own.
    """
    import gc

    gc.collect()

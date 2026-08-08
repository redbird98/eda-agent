# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Fast offline health checks.

These never touch Altium. Use them as a quick precheck in scripts.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from eda_agent.config import (
    WORKSPACE_POINTER_FILE,
    workspace_pointer_file,
    get_config,
)
from eda_agent.diag.checks import Check, Severity, Status


def _check_workspace_dir() -> Check:
    cfg = get_config()
    ws = cfg.workspace_dir
    if not ws.exists():
        return Check(
            name="workspace dir exists",
            status=Status.FAIL,
            message=f"{ws} does not exist",
            fix=(
                "Run `eda-agent install-scripts` to create it, or set "
                "EDA_AGENT_WORKSPACE to an existing path."
            ),
        )
    if not os.access(ws, os.W_OK):
        return Check(
            name="workspace dir writable",
            status=Status.FAIL,
            message=f"{ws} not writable",
            fix="Check the directory permissions.",
        )
    return Check(
        name="workspace dir",
        status=Status.PASS,
        message=str(ws),
    )


def _check_pointer_file() -> Check:
    # Resolve through the same helper the writer uses so an
    # EDA_AGENT_POINTER_FILE override is diagnosed, not the real file.
    pointer = workspace_pointer_file()
    if not pointer.exists():
        return Check(
            name="workspace pointer file",
            status=Status.FAIL,
            message=f"{pointer} missing",
            fix=(
                "Run `eda-agent install-scripts` to create the pointer "
                "file. DelphiScript reads it to find the IPC workspace."
            ),
        )
    try:
        contents = pointer.read_text(encoding="ascii").strip()
    except OSError as exc:
        return Check(
            name="workspace pointer file readable",
            status=Status.FAIL,
            message=f"could not read pointer file: {exc}",
            fix="Check file permissions or recreate via `install-scripts`.",
        )

    cfg_path = str(get_config().workspace_dir)
    pointer_path = contents.rstrip("\\")
    cfg_path_norm = cfg_path.rstrip("\\")
    if Path(pointer_path).resolve() != Path(cfg_path_norm).resolve():
        return Check(
            name="workspace pointer matches config",
            status=Status.FAIL,
            message=(
                f"pointer={pointer_path!r} but config={cfg_path_norm!r}, "
                "Python and Pascal will write to different directories"
            ),
            fix="Re-run `eda-agent install-scripts` to refresh the pointer.",
        )
    return Check(
        name="workspace pointer",
        status=Status.PASS,
        message=str(pointer),
    )


def _check_bundled_scripts() -> Check:
    from eda_agent.cli import get_bundled_scripts_path

    scripts = get_bundled_scripts_path()
    prj = scripts / "Altium_API.PrjScr"
    if not prj.exists():
        return Check(
            name="bundled DelphiScript project",
            status=Status.FAIL,
            message=f"{prj} not found",
            fix="Reinstall the package: `pip install --force-reinstall eda-agent`.",
        )
    return Check(
        name="bundled DelphiScript project",
        status=Status.PASS,
        message=str(prj),
    )


def _script_version(main_pas: Path) -> str | None:
    """SCRIPT_VERSION out of a Main.pas, or None if unreadable."""
    try:
        text = main_pas.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"SCRIPT_VERSION\s*=\s*'([^']+)'", text)
    return match.group(1) if match else None


def _check_deployed_scripts_current() -> Check:
    """The deployed copy must be the bundled one.

    Altium compiles whatever sits in the install directory, not what is
    in this checkout. So a Pascal fix that was never reinstalled
    reproduces exactly as before, and the only other place that shows is
    ``app_ping``'s version, which needs Altium running to read.

    That makes a stale deploy the one live-session problem worth
    catching offline: it costs a whole session to diagnose from the
    inside, and one file read to spot from the outside.

    WARN rather than FAIL. Nothing offline is affected, and a developer
    who has never installed the scripts is not in a broken state.
    """
    from ..cli import get_bundled_scripts_path, get_default_scripts_dest

    bundled = Path(get_bundled_scripts_path()) / "Main.pas"
    deployed = get_default_scripts_dest() / "Main.pas"

    if not deployed.exists():
        return Check(
            name="deployed scripts",
            status=Status.SKIP,
            message=f"none installed at {deployed.parent}",
            fix="Run `eda-agent install-scripts` before using Altium.",
        )

    want = _script_version(bundled)
    have = _script_version(deployed)
    if want is None or have is None:
        return Check(
            name="deployed scripts",
            status=Status.WARN,
            message="could not read SCRIPT_VERSION from both copies",
            fix="Check that Main.pas is readable in both locations.",
        )
    if want != have:
        return Check(
            name="deployed scripts current",
            status=Status.WARN,
            message=f"installed {have}, this tree has {want}",
            fix=(
                "Run `python -m eda_agent.server install-scripts --force`, "
                "then reload the script project in Altium. Until then "
                "Altium runs the older code and a fixed bug will "
                "reproduce."
            ),
        )
    return Check(
        name="deployed scripts current",
        status=Status.PASS,
        message=f"{have}, matches this tree",
    )


def _check_bridge_constructable() -> Check:
    """Construct the bridge object without sending a request.

    A failure here usually means a missing dependency (psutil, pywin32).
    """
    try:
        from eda_agent.bridge import get_bridge
        get_bridge()
    except Exception as exc:
        return Check(
            name="bridge constructable",
            status=Status.FAIL,
            message=f"bridge import/construct failed: {exc}",
            fix="Reinstall with `pip install --force-reinstall eda-agent`.",
        )
    return Check(name="bridge constructable", status=Status.PASS)


def run_health_checks() -> list[Check]:
    """Order matters, earlier failures often explain later ones."""
    return [
        _check_workspace_dir(),
        _check_pointer_file(),
        _check_bundled_scripts(),
        _check_deployed_scripts_current(),
        _check_bridge_constructable(),
    ]

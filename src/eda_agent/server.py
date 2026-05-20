# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""EDA Agent MCP Server - Main entry point + CLI subcommands."""

import argparse
import logging
import sys
from pathlib import Path
from mcp.server.fastmcp import FastMCP

from .tools import register_all_tools
from .config import get_config

logger = logging.getLogger("eda_agent")


def setup_logging() -> None:
    """Configure logging for the MCP server."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    )
    root = logging.getLogger("eda_agent")
    root.addHandler(handler)
    root.setLevel(logging.INFO)


# Create global FastMCP instance
mcp = FastMCP("eda-agent")

# Register all tools
register_all_tools(mcp)


def _spawn_dashboard_background(host: str, port: int) -> None:
    """Start the local web dashboard on a background thread.

    Run in-process with the MCP server so the user never has to launch a
    separate ``eda-agent dashboard`` command. The Flask app shares the
    same workspace dir as the MCP server (read from get_config()), so
    its log tailer + sentinel watcher pick up activity immediately.

    Failures are swallowed: a port conflict, a missing Flask install, or
    any other startup error must NOT block the MCP server -- stdio is
    the critical path. A note in the log lets the user know we tried.
    """
    import os
    import threading

    if os.environ.get("EDA_AGENT_NO_DASHBOARD") == "1":
        logger.info("dashboard disabled via EDA_AGENT_NO_DASHBOARD=1")
        return

    def _run():
        try:
            from .web.dashboard import create_app
            app = create_app()
            # Werkzeug emits its own startup banner to stderr that the MCP
            # client treats as protocol noise. Silence it without losing
            # actual error logs.
            import logging as _log
            _log.getLogger("werkzeug").setLevel(_log.WARNING)
            # threaded=True so SSE doesn't block other endpoints.
            app.run(host=host, port=port, debug=False, threaded=True,
                    use_reloader=False)
        except OSError as e:
            logger.warning("dashboard could not bind %s:%s (%s); "
                           "Open Dashboard button will be a no-op",
                           host, port, e)
        except Exception as e:
            logger.warning("dashboard background thread crashed: %s", e)

    t = threading.Thread(target=_run, name="dashboard-server", daemon=True)
    t.start()
    logger.info("dashboard scheduled on http://%s:%s/", host, port)


def serve_mcp() -> int:
    """Start the MCP server on stdio. This is the default mode -- it's
    what an MCP-compatible client calls when it invokes `eda-agent` with no args."""
    setup_logging()
    logger.info("Starting EDA Agent MCP Server")

    config = get_config()
    config.ensure_workspace()
    logger.info("Workspace directory: %s", config.workspace_dir)

    # Auto-launch the web dashboard in-process. The Altium-side
    # "Open Dashboard" button writes a sentinel that this app's background
    # watcher picks up and opens in the user's default browser.
    _spawn_dashboard_background(host="127.0.0.1", port=8766)

    mcp.run(transport="stdio")
    return 0


def main() -> int:
    """CLI entry point.

    Subcommands:
      serve             -- run the MCP server (default when no args given)
      scripts-path      -- print the path to the bundled DelphiScript files
      install-scripts   -- copy bundled scripts to a chosen directory

    IMPORTANT: when invoked with no arguments, this MUST start the MCP
    server on stdio -- MCP-compatible clients rely on that behaviour.
    """
    parser = argparse.ArgumentParser(
        prog="eda-agent",
        description=(
            "MCP server bridge for Altium Designer. "
            "Run with no arguments to start the MCP server on stdio."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # serve -- default when no args given
    subparsers.add_parser(
        "serve",
        help="Run the MCP server on stdio (default when no args given)",
    )

    # scripts-path
    subparsers.add_parser(
        "scripts-path",
        help="Print the path to the bundled DelphiScript files",
    )

    # install-scripts
    install_p = subparsers.add_parser(
        "install-scripts",
        help="Copy bundled scripts to a directory of your choice",
    )
    install_p.add_argument(
        "--dest",
        help=r"Destination directory (default: %%USERPROFILE%%\EDA Agent\scripts)",
    )
    install_p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing scripts without prompting",
    )

    # health -- offline, fast
    subparsers.add_parser(
        "health",
        help="Fast offline preconditions (workspace, pointer file, scripts)",
    )

    # doctor -- full preflight, talks to Altium
    doctor_p = subparsers.add_parser(
        "doctor",
        help="Full preflight: workspace + Altium + version + canary IPC calls",
    )
    doctor_p.add_argument(
        "--library",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "Optional .SchLib path to test reachability. Repeat for "
            "multiple libs. The doctor never crawls; it only tests "
            "paths you supply."
        ),
    )
    doctor_p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the text report.",
    )

    # dashboard -- local web UI for the MCP bridge
    dash_p = subparsers.add_parser(
        "dashboard",
        help=(
            "Launch the local web dashboard. Open http://127.0.0.1:8766 "
            "to see live MCP activity, performance, and health. The "
            "in-Altium status form has an 'Open Dashboard' button that "
            "auto-launches this server's URL via a workspace sentinel."
        ),
    )
    dash_p.add_argument("--host", default="127.0.0.1")
    dash_p.add_argument("--port", type=int, default=8766)
    dash_p.add_argument("--debug", action="store_true")

    # vote -- pairwise-preference vote UI in the browser
    vote_p = subparsers.add_parser(
        "vote",
        help=(
            "Launch the pairwise layout-preference vote UI in your "
            "browser. Generates two layouts of the same plan; you click "
            "the better one. Builds training data for the quality model."
        ),
    )
    vote_p.add_argument("--plan", required=True, type=Path,
                        help="Path to the DesignPlan JSON to vote on.")
    vote_p.add_argument("--symbols", type=Path, default=None,
                        help="Symbol fixtures JSON for offline mode. "
                             "Omit to use the live Altium bridge.")
    vote_p.add_argument("--host", default="127.0.0.1")
    vote_p.add_argument("--port", type=int, default=8765)
    vote_p.add_argument("--debug", action="store_true")

    args = parser.parse_args()

    if args.command is None or args.command == "serve":
        return serve_mcp()

    # Lazy import -- keeps the hot stdio path free of CLI-only deps.
    from . import cli

    if args.command == "scripts-path":
        return cli.cmd_scripts_path()
    if args.command == "install-scripts":
        return cli.cmd_install_scripts(dest=args.dest, force=args.force)
    if args.command == "dashboard":
        from .web.dashboard import main as dashboard_main
        return dashboard_main([
            "--host", args.host,
            "--port", str(args.port),
            *(["--debug"] if args.debug else []),
        ])
    if args.command == "vote":
        from .web.server import main as vote_main
        return vote_main([
            "--plan", str(args.plan),
            *(["--symbols", str(args.symbols)] if args.symbols else []),
            "--host", args.host,
            "--port", str(args.port),
            *(["--debug"] if args.debug else []),
        ])
    if args.command in ("health", "doctor"):
        from .diag.checks import format_report, overall_exit_code
        if args.command == "health":
            from .diag.health import run_health_checks
            checks = run_health_checks()
            title = "eda-agent health"
        else:
            from .diag.doctor import run_doctor_checks
            checks = run_doctor_checks(library_paths=args.library)
            title = "eda-agent doctor"

        emit_json = bool(getattr(args, "json", False))
        if emit_json:
            import json as _json
            payload = {
                "title": title,
                "checks": [
                    {
                        "name": c.name,
                        "status": c.status.value,
                        "message": c.message,
                        "fix": c.fix,
                        "severity": c.severity.value,
                    }
                    for c in checks
                ],
            }
            print(_json.dumps(payload, indent=2))
        else:
            print(format_report(checks, title=title))
        return overall_exit_code(checks)

    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

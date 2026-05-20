# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Local web dashboard for the EDA Agent MCP bridge.

Companion to the in-Altium status form. Run with:

    eda-agent dashboard

then open ``http://127.0.0.1:8766`` (or click the "Open Dashboard"
button on the Altium-side status form, which writes a sentinel the
MCP server's keep-alive thread picks up and launches the browser).

The dashboard tails ``workspace/activity.log`` and surfaces:

- Live status pill (in-flight call, elapsed time, pause state).
- Four KPI tiles (uptime, request count, busy time, idle countdown).
- A streaming feed of recent calls with severity tags, request IDs,
  durations, and inline error details that expand on click.
- A per-command performance table you can sort by N / avg / max.
- A free-text filter that scopes both feed and perf table.
- Health probes (script version, version match, IPC liveness).

Server-Sent Events stream from ``/events`` give the browser tab a
sub-second view of every command without polling. Static assets are
inlined into one HTML response so the dashboard works offline and
needs no build tooling.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from flask import Flask, Response, jsonify, send_from_directory, stream_with_context

from eda_agent.config import get_config

logger = logging.getLogger("eda_agent.web.dashboard")


# ---------------------------------------------------------------------------
# Bridge helpers: call MCP tools synchronously from Flask handlers.
# The dashboard runs in the same process as the MCP server, so get_bridge()
# returns the shared singleton. Responses are cached with a short TTL to
# avoid flooding Altium when multiple browser tabs are open.
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}
# Per-key fetch locks for single-flight caching. Without these, a burst of
# requests for the same key (the dashboard fires several at once) each see a
# cache miss and each launch their own bridge call -- a thundering herd of
# duplicate work. Single-flight collapses them to one fetch per key.
_keyfetch_locks: dict[str, threading.Lock] = {}

# NOTE: we deliberately do NOT serialise bridge calls with a process-wide
# lock. The IPC scheme is per-request-file (request_<id>.json /
# response_<id>.json), so concurrent callers never collide -- each polls
# only its own response. A global lock here was actively harmful: it held
# a second caller from even WRITING its request file until the first call
# finished, so Pascal couldn't pick up a request that did not yet exist
# (observed as multi-second "pickup" latency). Let every caller publish
# immediately; Pascal serialises the actual processing on its own side.


def _cache_peek(key: str, ttl_seconds: float) -> Any:
    """Return the cached value for key if fresh, else None.

    Unlike _cached this NEVER triggers a fetch -- it is for opportunistic
    reads where a cache miss should just be skipped, not waited on.
    """
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (now - hit[0]) < ttl_seconds:
            return hit[1]
    return None


def _cached(key: str, ttl_seconds: float, fn) -> Any:
    """Single-flight memoize: fn() by key for ttl_seconds.

    On a cache miss only ONE caller runs fn(); concurrent callers for the
    same key block on the key's fetch lock, then reuse the freshly-cached
    value. This collapses request bursts into one bridge round-trip.
    """
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (now - hit[0]) < ttl_seconds:
            return hit[1]
        lk = _keyfetch_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _keyfetch_locks[key] = lk

    # Serialise the fetch per key. Whoever gets here first does the work;
    # the rest wait, then fall through to the re-check below and find the
    # value already cached.
    with lk:
        now = time.time()
        with _cache_lock:
            hit = _cache.get(key)
            if hit and (now - hit[0]) < ttl_seconds:
                return hit[1]
        val = fn()
        with _cache_lock:
            _cache[key] = (time.time(), val)
        return val


def _bridge_call(command: str, params: Optional[dict] = None,
                 timeout: float = 12.0) -> Optional[dict]:
    """Send one MCP command via the shared bridge.

    Returns the response data dict on success, or None if Altium is
    unreachable or the call fails. Errors are logged but not raised --
    a dashboard tab missing data is preferable to a crashed endpoint.
    Not serialised: the per-request-file IPC scheme means concurrent
    callers never collide, and each publishes its request immediately
    so Pascal can pick it up the moment its polling loop is free.
    """
    try:
        from eda_agent.bridge import get_bridge
        bridge = get_bridge()
        if not bridge.is_altium_running():
            return None
        return bridge.send_command(command, params or {}, timeout=timeout)
    except Exception as e:
        logger.debug("bridge_call %s failed: %s", command, e)
        return None


# ---------------------------------------------------------------------------
# activity.log tail
# ---------------------------------------------------------------------------

# Pascal's activity.log line shapes:
#
#   Command line:
#     YYYY-MM-DD HH:MM:SS.mmm,duration_ms,command,tag,response_bytes,<payload...>
#   Session-start:
#     YYYY-MM-DD HH:MM:SS.mmm,0,_session_start,version=X,protocol=N
#   Session-end:
#     YYYY-MM-DD HH:MM:SS.mmm,0,_session_end,requests=N
_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}),"
    r"(?P<dur>\d+),"
    r"(?P<cmd>[^,]+),"
    r"(?P<tag>[^,]+),"
    r"(?P<bytes>\d+),"
    r"(?P<payload>.*)$"
)
_SESSION_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}),"
    r"0,(?P<kind>_session_start|_session_end),(?P<rest>.*)$"
)

_ERR_RE = re.compile(
    r'"error"\s*:\s*\{[^{}]*?"code"\s*:\s*"(?P<code>[^"]+)"[^{}]*?'
    r'"message"\s*:\s*"(?P<msg>(?:[^"\\]|\\.)*)"'
)

_ID_RE = re.compile(r'"id"\s*:\s*"(?P<id>[0-9a-f]{8,32})"')


@dataclass
class LogEntry:
    timestamp: str
    duration_ms: int
    command: str
    tag: str               # OK | WARN | SLOW | ERR  | EXCEPTION | EMPTY
    response_bytes: int
    request_id: str        # extracted from payload's "id" or "" if not found
    error_code: str        # extracted from payload's error.code, "" if none
    error_msg: str         # extracted from payload's error.message, "" if none
    payload_prefix: str    # full prefix as it appeared on the line

    def severity(self) -> str:
        # tag='OK' covers both success:true and success:false responses; the
        # error_code/message extracted from the payload is the authoritative
        # signal for "this call failed" regardless of the Pascal-side tag.
        if self.error_code or self.tag.strip() in ("ERR", "EXCEPTION", "EMPTY"):
            return "error"
        if self.duration_ms >= 500:
            return "slow"
        if self.duration_ms >= 100:
            return "warn"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.timestamp,
            "dur_ms": self.duration_ms,
            "cmd": self.command,
            "tag": self.tag.strip(),
            "bytes": self.response_bytes,
            "id": self.request_id,
            "err_code": self.error_code,
            "err_msg": self.error_msg,
            "severity": self.severity(),
        }


@dataclass
class SessionEvent:
    """Synthetic event emitted on _session_start / _session_end rows."""
    timestamp: str
    kind: str              # 'session_start' | 'session_end'
    version: str = ""
    requests: int = 0


def _parse_line(line: str) -> Optional[LogEntry | SessionEvent]:
    """Parse one activity.log line. Returns None for unparseable lines."""
    stripped = line.strip()
    if not stripped:
        return None

    # Session events have a different shape (no tag / response_bytes columns).
    sm = _SESSION_RE.match(stripped)
    if sm:
        rest = sm["rest"]
        if sm["kind"] == "_session_start":
            version = ""
            v = re.search(r"version=([^\s,]+)", rest)
            if v:
                version = v.group(1)
            return SessionEvent(
                timestamp=sm["ts"], kind="session_start", version=version,
            )
        # _session_end
        reqs = 0
        v = re.search(r"requests=(\d+)", rest)
        if v:
            reqs = int(v.group(1))
        return SessionEvent(
            timestamp=sm["ts"], kind="session_end", requests=reqs,
        )

    m = _LINE_RE.match(stripped)
    if not m:
        return None

    cmd = m["cmd"].strip()
    payload = m["payload"]

    id_m = _ID_RE.search(payload)
    err_m = _ERR_RE.search(payload)
    return LogEntry(
        timestamp=m["ts"],
        duration_ms=int(m["dur"]),
        command=cmd,
        tag=m["tag"].strip(),
        response_bytes=int(m["bytes"]),
        request_id=(id_m.group("id")[:8] if id_m else ""),
        error_code=(err_m.group("code") if err_m else ""),
        error_msg=(err_m.group("msg") if err_m else ""),
        payload_prefix=payload[:200],
    )


class ActivityTailer:
    """Background thread that tails activity.log and feeds a deque + SSE queue.

    Designed to survive log truncation and rotation: if the file shrinks
    below our last-seen offset, we seek to the start and replay. Each
    listener gets its own threading.Event + queue so SSE can fan out
    without blocking the tailer.
    """

    BUFFER_LINES = 2000

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.entries: deque[LogEntry] = deque(maxlen=self.BUFFER_LINES)
        self.session: Optional[SessionEvent] = None
        self._listeners: list[threading.Event] = []
        self._listener_queues: dict[int, deque[dict]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="dashboard-tailer", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def subscribe(self) -> tuple[threading.Event, deque[dict]]:
        """Register an SSE listener. Returns (wake-event, queue)."""
        wake = threading.Event()
        q: deque[dict] = deque(maxlen=200)
        key = id(wake)
        with self._lock:
            self._listeners.append(wake)
            self._listener_queues[key] = q
        return wake, q

    def unsubscribe(self, wake: threading.Event) -> None:
        with self._lock:
            try:
                self._listeners.remove(wake)
            except ValueError:
                pass
            self._listener_queues.pop(id(wake), None)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            session = self.session
            return {
                "session": {
                    "version": session.version if session else "",
                    "kind": session.kind if session else "",
                    "ts": session.timestamp if session else "",
                },
                "entries": [e.to_dict() for e in reversed(self.entries)],
            }

    def _broadcast(self, payload: dict) -> None:
        with self._lock:
            for wake in self._listeners:
                q = self._listener_queues.get(id(wake))
                if q is not None:
                    q.append(payload)
                wake.set()

    def _ingest(self, line: str) -> None:
        parsed = _parse_line(line)
        if parsed is None:
            return
        if isinstance(parsed, SessionEvent):
            with self._lock:
                self.session = parsed
            self._broadcast({
                "type": "session",
                "kind": parsed.kind,
                "version": parsed.version,
                "ts": parsed.timestamp,
            })
            return
        # LogEntry
        with self._lock:
            self.entries.append(parsed)
        self._broadcast({"type": "entry", "entry": parsed.to_dict()})

    def _run(self) -> None:
        offset = 0
        # Initial backfill: read everything that's there so the UI has
        # session context immediately on first paint.
        try:
            if self.log_path.exists():
                with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        self._ingest(line.rstrip("\n"))
                    offset = f.tell()
        except OSError as e:
            logger.debug("dashboard tailer initial read failed: %s", e)

        while not self._stop.is_set():
            try:
                if not self.log_path.exists():
                    time.sleep(0.5)
                    continue
                size = self.log_path.stat().st_size
                if size < offset:
                    # Truncated or rotated. Replay from scratch.
                    offset = 0
                if size == offset:
                    time.sleep(0.3)
                    continue
                with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    for line in f:
                        if not line.endswith("\n"):
                            # Partial line, keep position before it and retry.
                            break
                        self._ingest(line.rstrip("\n"))
                        offset = f.tell()
            except OSError as e:
                logger.debug("dashboard tailer read failed: %s", e)
                time.sleep(0.5)


# ---------------------------------------------------------------------------
# Stats aggregation
# ---------------------------------------------------------------------------

def _aggregate(entries: Iterable[LogEntry]) -> list[dict[str, Any]]:
    """Compute per-command N / total_ms / max_ms / avg_ms from a stream."""
    by_cmd: dict[str, dict[str, int]] = {}
    for e in entries:
        slot = by_cmd.setdefault(e.command, {"n": 0, "total": 0, "max": 0})
        slot["n"] += 1
        slot["total"] += e.duration_ms
        if e.duration_ms > slot["max"]:
            slot["max"] = e.duration_ms
    out: list[dict[str, Any]] = []
    for cmd, s in by_cmd.items():
        avg = (s["total"] // s["n"]) if s["n"] else 0
        out.append({
            "command": cmd, "n": s["n"],
            "avg_ms": avg, "max_ms": s["max"], "total_ms": s["total"],
        })
    out.sort(key=lambda r: r["max_ms"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Artifacts (recent files produced by tools)
# ---------------------------------------------------------------------------

# Extensions we want to surface in the dashboard. Each tool that produces a
# file uses one of these — SVG preview from design_preview_plan, PDF/STEP/DXF
# from the export_* tools, PNG screenshots from export_image, JSON snapshots
# from design_review_snapshot, etc.
_ARTIFACT_EXTS = {
    ".svg", ".pdf", ".png", ".jpg", ".jpeg", ".step", ".stp", ".dxf",
    ".json", ".jsonl", ".csv", ".html", ".txt",
}
_ARTIFACT_MAX_ROWS = 60


def _scan_artifacts(workspace_dir: Path) -> list[dict[str, Any]]:
    """Return recent artifact files near the workspace.

    Scans workspace_dir + its __Previews sibling (if present) plus the
    `<repo>/.symbol_cache/` directory where the design preview SVGs land.
    Filters to known interesting extensions, returns newest-first.
    """
    candidates: list[Path] = []
    roots = [workspace_dir, workspace_dir / "__Previews"]
    # Repo-level .symbol_cache where design previews go by default.
    try:
        repo_root = Path(__file__).resolve().parents[3]
        cache = repo_root / ".symbol_cache"
        if cache.exists():
            roots.append(cache)
    except (IndexError, OSError):
        pass

    for root in roots:
        try:
            if not root.exists():
                continue
            for p in root.iterdir():
                if not p.is_file():
                    continue
                if p.suffix.lower() not in _ARTIFACT_EXTS:
                    continue
                if p.name in (
                    "activity.log", "bridge_trace.log", "mcp_config.json",
                    "intent.txt", "open_dashboard.url",
                ):
                    continue
                # Skip noisy per-request IPC files.
                if p.name.startswith(("request_", "response_", "progress_")):
                    continue
                candidates.append(p)
        except OSError:
            continue

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    candidates = candidates[:_ARTIFACT_MAX_ROWS]

    out: list[dict[str, Any]] = []
    for p in candidates:
        try:
            st = p.stat()
            out.append({
                "path": str(p),
                "name": p.name,
                "dir":  str(p.parent),
                "size": st.st_size,
                "mtime": st.st_mtime,
                "ext": p.suffix.lower().lstrip("."),
            })
        except OSError:
            continue
    return out


def _safe_artifact_path(target: str, workspace_dir: Path) -> bool:
    """Whitelist gate: only let the dashboard open files we listed."""
    try:
        p = Path(target).resolve()
    except (OSError, ValueError):
        return False
    if not p.exists() or not p.is_file():
        return False
    if p.suffix.lower() not in _ARTIFACT_EXTS:
        return False
    # Must live under one of the scanned roots.
    try:
        repo_root = Path(__file__).resolve().parents[3]
    except (IndexError, OSError):
        repo_root = workspace_dir
    allowed_roots = [
        workspace_dir.resolve(),
        (workspace_dir / "__Previews").resolve() if (workspace_dir / "__Previews").exists() else None,
        (repo_root / ".symbol_cache").resolve() if (repo_root / ".symbol_cache").exists() else None,
    ]
    for root in allowed_roots:
        if root is None:
            continue
        try:
            p.relative_to(root)
            return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

_HTML_PATH = Path(__file__).resolve().parent / "dashboard_static" / "index.html"


def _watch_open_dashboard_sentinel(workspace_dir: Path, stop: threading.Event) -> None:
    """Watch workspace/open_dashboard.url and open the browser when it appears.

    The Pascal-side "Open Dashboard" button writes the URL to this file. The
    bridge keep-alive thread also watches the same sentinel, but that only
    runs after the first MCP call attaches the bridge. The dashboard server
    is typically already running long before MCP attaches, so this watcher
    is what actually makes the button respond promptly on first click.
    """
    sentinel = workspace_dir / "open_dashboard.url"
    while not stop.wait(0.5):
        if not sentinel.exists():
            continue
        try:
            url = sentinel.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        try:
            sentinel.unlink()
        except OSError:
            pass
        if not url.startswith("http://") and not url.startswith("https://"):
            continue
        try:
            import webbrowser
            webbrowser.open(url)
            logger.info("opened dashboard URL via sentinel: %s", url)
        except Exception as e:
            logger.debug("webbrowser.open failed: %s", e)


def create_app(workspace_dir: Optional[Path] = None) -> Flask:
    if workspace_dir is None:
        workspace_dir = get_config().workspace_dir
    log_path = workspace_dir / "activity.log"

    tailer = ActivityTailer(log_path)
    tailer.start()

    sentinel_stop = threading.Event()
    sentinel_thread = threading.Thread(
        target=_watch_open_dashboard_sentinel,
        args=(workspace_dir, sentinel_stop),
        name="dashboard-sentinel-watch",
        daemon=True,
    )
    sentinel_thread.start()

    app = Flask("eda-agent-dashboard")
    app.config["WORKSPACE_DIR"] = str(workspace_dir)
    app.config["TAILER"] = tailer

    @app.route("/")
    def index() -> Response:
        if not _HTML_PATH.exists():
            return Response(
                "<h1>dashboard_static/index.html missing</h1>",
                status=500, mimetype="text/html",
            )
        return Response(_HTML_PATH.read_text(encoding="utf-8"),
                        mimetype="text/html")

    @app.route("/api/snapshot")
    def snapshot() -> Response:
        return jsonify(tailer.snapshot())

    @app.route("/api/stats")
    def stats() -> Response:
        with tailer._lock:
            entries = list(tailer.entries)
        return jsonify({"commands": _aggregate(entries)})

    @app.route("/api/health")
    def health() -> Response:
        """Run the doctor preflight, return as JSON.

        Doctor checks talk to Altium (ping, version, save_all) so this
        endpoint can take a second or two. The dashboard calls it on
        demand, not on every render.
        """
        try:
            from eda_agent.diag.doctor import run_doctor_checks
            checks = run_doctor_checks(library_paths=[])
            return jsonify({
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
                "ok": all(c.status.value in ("pass", "skip") for c in checks),
            })
        except Exception as e:
            return jsonify({"ok": False, "error": str(e), "checks": []})

    @app.route("/api/artifacts")
    def artifacts() -> Response:
        """List recent files produced by tools (preview SVGs, exports).

        Pure filesystem scan against the workspace and its __Previews
        sibling. Returns newest-first. The dashboard turns each entry
        into a clickable row that opens the file via the OS handler
        (browsers refuse to open file:// links from http origins; the
        click goes back through /api/artifacts/open).
        """
        files = _scan_artifacts(workspace_dir)
        return jsonify({"artifacts": files})

    @app.route("/api/artifacts/open", methods=["POST"])
    def artifacts_open() -> Response:
        """Open an artifact via the OS default handler."""
        from flask import request as _req
        body = _req.get_json(silent=True) or {}
        target = body.get("path", "")
        if not _safe_artifact_path(target, workspace_dir):
            return jsonify({"ok": False, "error": "path not allowed"}), 400
        try:
            import os as _os
            _os.startfile(target)  # type: ignore[attr-defined]
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/intent")
    def intent() -> Response:
        """Read the current conversation intent the planner set."""
        intent_path = workspace_dir / "intent.txt"
        text = ""
        try:
            if intent_path.exists():
                text = intent_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            text = ""
        return jsonify({"intent": text})

    # -----------------------------------------------------------------
    # Design-centric proxy endpoints. Each one calls the bridge with a
    # short cache TTL so multiple browser tabs don't hammer Altium.
    # Errors return ``{"ok": False, "reason": "..."}`` so the UI can
    # render a meaningful empty state instead of a JS exception.
    # -----------------------------------------------------------------

    def _proxy(command: str, params: Optional[dict] = None,
               ttl: float = 8.0, timeout: float = 12.0,
               cache_key: Optional[str] = None) -> dict:
        key = cache_key or f"{command}:{json.dumps(params or {}, sort_keys=True)}"
        def call():
            try:
                data = _bridge_call(command, params, timeout=timeout)
            except Exception as e:
                return {"ok": False, "reason": str(e)}
            if data is None:
                return {"ok": False, "reason": "altium-not-running"}
            return {"ok": True, "data": data}
        return _cached(key, ttl, call)

    def _project_snapshot() -> dict:
        """One bundled IPC call -> focused / documents / stats / bom / nets /
        messages / path. The Pascal `project.dashboard_snapshot` handler
        gathers all of it server-side so the dashboard pays the poll + IO
        round-trip ONCE instead of 7x. Cached 15s; all /api/project/* and
        the review-summary endpoints read from this single entry.
        """
        def call():
            try:
                data = _bridge_call("project.dashboard_snapshot", {},
                                    timeout=45.0)
            except Exception as e:
                return {"ok": False, "reason": str(e)}
            if data is None:
                return {"ok": False, "reason": "altium-not-running"}
            return {"ok": True, "data": data}
        return _cached("project.dashboard_snapshot", 15.0, call)

    def _snapshot_section(section: str) -> dict:
        """Pull one section out of the bundled snapshot, shaped as the
        individual endpoints used to return: {"ok": bool, "data": ...}."""
        snap = _project_snapshot()
        if not snap.get("ok"):
            return snap
        bundle = snap.get("data") or {}
        return {"ok": True, "data": bundle.get(section)}

    @app.route("/api/project/info")
    def project_info() -> Response:
        """Focused project + documents + design stats -- one bundled call."""
        return jsonify({
            "focused":   _snapshot_section("focused"),
            "documents": _snapshot_section("documents"),
            "stats":     _snapshot_section("stats"),
            "path":      _snapshot_section("path"),
        })

    @app.route("/api/project/components")
    def project_components() -> Response:
        """Live BOM, served from the bundled snapshot."""
        return jsonify(_snapshot_section("bom"))

    @app.route("/api/project/nets")
    def project_nets() -> Response:
        """Net inventory, served from the bundled snapshot."""
        return jsonify(_snapshot_section("nets"))

    @app.route("/api/project/messages")
    def project_messages() -> Response:
        """Compiler / ERC messages, served from the bundled snapshot."""
        return jsonify(_snapshot_section("messages"))

    @app.route("/api/libraries")
    def libraries_inventory() -> Response:
        """Library inventory written by design.snapshot_inventory.

        Looks for ``workspace/inventory.json`` (a cached snapshot the agent
        writes when it calls ``design_snapshot_inventory``). When absent,
        returns an empty state hint so the UI can prompt the user to run
        the snapshot from the conversation side.
        """
        inv_path = workspace_dir / "inventory.json"
        if inv_path.exists():
            try:
                data = json.loads(inv_path.read_text(encoding="utf-8"))
                return jsonify({"ok": True, "data": data,
                                "mtime": inv_path.stat().st_mtime,
                                "source": str(inv_path)})
            except (OSError, json.JSONDecodeError) as e:
                return jsonify({"ok": False, "reason": f"inventory.json unreadable: {e}"})
        return jsonify({"ok": False, "reason": "no-inventory-cached",
                        "hint": "Run design_snapshot_inventory to populate."})

    @app.route("/api/plan")
    def design_plan() -> Response:
        """Current DesignPlan if one is cached in the workspace.

        Reads ``workspace/plan.json`` (written by tools that hand a plan
        to execute_plan). Falls back to any ``<workspace>/*.canvas.json``
        in the workspace dir, which design_execute_plan writes alongside
        the project. Empty state means "no plan in flight".
        """
        plan_path = workspace_dir / "plan.json"
        if plan_path.exists():
            try:
                data = json.loads(plan_path.read_text(encoding="utf-8"))
                return jsonify({"ok": True, "data": data,
                                "mtime": plan_path.stat().st_mtime,
                                "source": str(plan_path)})
            except (OSError, json.JSONDecodeError) as e:
                return jsonify({"ok": False, "reason": f"plan.json unreadable: {e}"})
        # Last-ditch: look for a canvas snapshot next to any open project.
        canvas_candidates = sorted(workspace_dir.glob("*.canvas.json"),
                                   key=lambda p: p.stat().st_mtime,
                                   reverse=True)
        if canvas_candidates:
            cp = canvas_candidates[0]
            try:
                data = json.loads(cp.read_text(encoding="utf-8"))
                return jsonify({"ok": True, "data": data,
                                "mtime": cp.stat().st_mtime,
                                "source": str(cp),
                                "kind": "canvas"})
            except (OSError, json.JSONDecodeError):
                pass
        return jsonify({"ok": False, "reason": "no-plan-cached",
                        "hint": "design_preview_plan / design_execute_plan write the cached snapshot."})

    # -----------------------------------------------------------------
    # Design review aggregator + drill-in detail.
    # -----------------------------------------------------------------

    # Lenient parameter-name lookups -- Altium projects spell these many ways.
    _DATASHEET_KEYS    = ("Datasheet", "DatasheetURL", "Datasheet URL", "datasheet")
    _MPN_KEYS          = ("ManufacturerPart", "Manufacturer Part Number",
                          "ManufacturerPartNumber", "MPN", "PartNumber",
                          "Part Number", "manufacturer_part_number")
    _MANUFACTURER_KEYS = ("Manufacturer", "Mfr", "manufacturer")
    _DESCRIPTION_KEYS  = ("Description", "Comment", "description")
    _VALUE_KEYS        = ("Value", "value")

    def _param(params: dict, keys: tuple) -> str:
        if not isinstance(params, dict):
            return ""
        for k in keys:
            v = params.get(k)
            if v not in (None, "", "*"):
                return str(v).strip()
        return ""

    def _looks_like_url(s: str) -> bool:
        s = (s or "").strip().lower()
        return s.startswith("http://") or s.startswith("https://") \
            or s.endswith(".pdf") or s.startswith("file:")

    def _build_review_summary() -> dict:
        """Fast review pass: BOM + nets only, no per-component parameter
        enrichment. Reads from the bundled project snapshot so it shares
        the single round-trip with the rest of the dashboard.
        """
        snap = _project_snapshot()
        if not snap.get("ok"):
            return {"ok": False, "reason": snap.get("reason", "unavailable")}
        bundle = snap.get("data") or {}
        bom = bundle.get("bom")
        bom_components = (bom.get("components") if isinstance(bom, dict) else None) or []

        issues: list[dict] = []
        components = []
        for raw in bom_components:
            if not isinstance(raw, dict):
                continue
            des = raw.get("designator", "")
            footprint = raw.get("footprint") or ""
            components.append({
                "designator": des,
                "comment":    raw.get("comment", ""),
                "footprint":  footprint,
                "lib_ref":    raw.get("lib_ref", ""),
                "pin_count":  len(raw.get("pins") or []),
                "has_footprint": bool(footprint),
            })
            if not footprint:
                issues.append({
                    "severity": "error", "category": "missing-footprint",
                    "designator": des, "message": f"{des} has no footprint",
                })

        # Net-level issues: orphan nets (only 1 pin attached). From the
        # same bundled snapshot -- no extra round-trip.
        nets_resp = bundle.get("nets")
        pin_rows = []
        if isinstance(nets_resp, dict):
            pin_rows = nets_resp.get("pins") or nets_resp.get("nets") or []
        net_pin_count: dict[str, int] = {}
        for p in pin_rows:
            if not isinstance(p, dict):
                continue
            name = p.get("net") or p.get("name")
            if not name:
                continue
            net_pin_count[name] = net_pin_count.get(name, 0) + 1
        for name, count in net_pin_count.items():
            if count == 1:
                issues.append({
                    "severity": "warn", "category": "orphan-net",
                    "net": name,
                    "message": f"net '{name}' has only one pin attached",
                })

        return {
            "ok": True,
            "components_total": len(components),
            "components": components,
            "issues": issues,
            "nets_total": len(net_pin_count),
        }

    def _build_review_coverage() -> dict:
        """Slow enrichment pass: get_component_info_batch with parameters.
        Computes datasheet / MPN / manufacturer coverage. Latency: 5-15s
        depending on project size. Cached 5 minutes -- this data does not
        change often during a review session.
        """
        snap = _project_snapshot()
        if not snap.get("ok"):
            return {"ok": False, "reason": snap.get("reason", "unavailable")}
        bom = (snap.get("data") or {}).get("bom")
        bom_components = (bom.get("components") if isinstance(bom, dict) else None) or []
        designators = [c.get("designator", "") for c in bom_components
                       if isinstance(c, dict) and c.get("designator")]
        if not designators:
            return {"ok": True, "coverage": {}, "issues": []}

        info = _bridge_call(
            "project.get_component_info_batch",
            {"designators": "~~".join(designators),
             "with_pin_nets": "false",
             "with_parameters": "true"},
            timeout=120,
        )
        info_list = []
        if isinstance(info, dict):
            info_list = info.get("components") or []
        info_by_designator = {}
        for c in info_list:
            if isinstance(c, dict) and c.get("designator"):
                info_by_designator[c["designator"]] = c

        cov_ds = cov_mpn = cov_mfr = cov_desc = cov_val = 0
        issues: list[dict] = []
        per_component: list[dict] = []
        for des in designators:
            detail = info_by_designator.get(des, {})
            params = detail.get("parameters") if isinstance(detail, dict) else {}
            datasheet = _param(params, _DATASHEET_KEYS)
            mpn       = _param(params, _MPN_KEYS)
            mfr       = _param(params, _MANUFACTURER_KEYS)
            descr     = _param(params, _DESCRIPTION_KEYS)
            value     = _param(params, _VALUE_KEYS)

            per_component.append({
                "designator":   des,
                "datasheet":    datasheet,
                "mpn":          mpn,
                "manufacturer": mfr,
                "description":  descr,
                "value":        value,
            })
            if datasheet:    cov_ds   += 1
            if mpn:          cov_mpn  += 1
            if mfr:          cov_mfr  += 1
            if descr:        cov_desc += 1
            if value:        cov_val  += 1

            if not datasheet:
                issues.append({
                    "severity": "warn", "category": "missing-datasheet",
                    "designator": des, "message": f"{des} has no Datasheet parameter",
                })
            elif not _looks_like_url(datasheet):
                issues.append({
                    "severity": "warn", "category": "datasheet-not-url",
                    "designator": des,
                    "message": f"{des} Datasheet does not look like a URL/PDF: '{datasheet[:60]}'",
                })
            if not mpn:
                issues.append({
                    "severity": "warn", "category": "missing-mpn",
                    "designator": des, "message": f"{des} has no MPN",
                })
            if not mfr:
                issues.append({
                    "severity": "info", "category": "missing-manufacturer",
                    "designator": des, "message": f"{des} has no Manufacturer",
                })

        total = len(designators)
        def pct(have: int) -> int:
            return int(round(100 * have / total)) if total else 0
        coverage = {
            "datasheet":    {"have": cov_ds,   "total": total, "pct": pct(cov_ds)},
            "mpn":          {"have": cov_mpn,  "total": total, "pct": pct(cov_mpn)},
            "manufacturer": {"have": cov_mfr,  "total": total, "pct": pct(cov_mfr)},
            "description":  {"have": cov_desc, "total": total, "pct": pct(cov_desc)},
            "value":        {"have": cov_val,  "total": total, "pct": pct(cov_val)},
        }
        return {
            "ok": True,
            "components_total": total,
            "coverage": coverage,
            "issues": issues,
            "per_component": per_component,
        }

    @app.route("/api/review/summary")
    def review_summary() -> Response:
        """Fast pass: BOM + nets + footprint/orphan issues. Cached 60s."""
        return jsonify(_cached("review_summary", 60.0, _build_review_summary))

    @app.route("/api/review/coverage")
    def review_coverage() -> Response:
        """Slow pass: per-component parameters -> datasheet/MPN/etc.
        coverage. Cached 5 minutes -- data is stable across a review session.
        """
        return jsonify(_cached("review_coverage", 300.0, _build_review_coverage))

    @app.route("/api/component/<designator>")
    def component_detail(designator: str) -> Response:
        """Single-component drill-in: parameters, pins, datasheet.

        Speed-critical: this fires on every BOM-row / issue-row click.
        We deliberately pass ``with_pin_nets=false`` so the batch handler
        SKIPS SmartCompile -- a recompile on a multi-sheet project costs
        5-15s and would be paid on every single click. The component's
        pins-with-nets come from the cached project snapshot instead
        (get_bom already compiled the project when the snapshot was
        built), so the drawer still shows each pin's net for free.
        """
        from flask import abort
        des = designator.strip()
        if not des:
            abort(400)
        info = _bridge_call(
            "project.get_component_info_batch",
            {"designators": des, "with_pin_nets": "false",
             "with_parameters": "true"},
            timeout=15,
        )
        if info is None:
            return jsonify({"ok": False, "reason": "altium-not-running"})
        comps = info.get("components") if isinstance(info, dict) else None
        if not comps:
            return jsonify({"ok": False, "reason": "not-found"})
        comp = comps[0]
        # Graft pins-with-nets from the snapshot BOM IF it is already
        # cached -- peek only, never trigger a fetch. A cold snapshot
        # would mean an 8-12s compile here; not worth it for a drawer.
        # When the snapshot is warm (the common case -- the user got to
        # this drawer from a tab that already loaded it) the pins show
        # their nets; when cold the drawer just shows pin number/name.
        try:
            snap = _cache_peek("project.dashboard_snapshot", 15.0)
            if snap and snap.get("ok"):
                bom = (snap.get("data") or {}).get("bom") or {}
                for c in (bom.get("components") or []):
                    if isinstance(c, dict) and c.get("designator") == des:
                        if c.get("pins"):
                            comp["pins"] = c["pins"]
                        break
        except Exception as e:
            logger.debug("snapshot pin-graft failed for %s: %s", des, e)
        return jsonify({"ok": True, "data": comp})

    # -----------------------------------------------------------------
    # Actions: cross-probe + highlight + clear. Each proxies one MCP
    # command. Never cached (the user is asking for an interactive jump).
    # -----------------------------------------------------------------

    @app.route("/api/action/cross_probe", methods=["POST"])
    def action_cross_probe() -> Response:
        from flask import request as _req
        body = _req.get_json(silent=True) or {}
        designator = (body.get("designator") or "").strip()
        target = (body.get("target") or "schematic").strip()
        if not designator:
            return jsonify({"ok": False, "reason": "designator required"}), 400
        if target not in ("schematic", "pcb"):
            target = "schematic"
        try:
            data = _bridge_call("project.cross_probe",
                                {"designator": designator, "target": target},
                                timeout=10)
        except Exception as e:
            return jsonify({"ok": False, "reason": str(e)})
        if data is None:
            return jsonify({"ok": False, "reason": "altium-not-running"})
        return jsonify({"ok": True, "data": data})

    @app.route("/api/action/highlight_net", methods=["POST"])
    def action_highlight_net() -> Response:
        from flask import request as _req
        body = _req.get_json(silent=True) or {}
        name = (body.get("net_name") or "").strip()
        clear = body.get("clear_existing", True)
        if not name:
            return jsonify({"ok": False, "reason": "net_name required"}), 400
        try:
            data = _bridge_call("generic.highlight_net",
                                {"net_name": name,
                                 "clear_existing": "true" if clear else "false"},
                                timeout=10)
        except Exception as e:
            return jsonify({"ok": False, "reason": str(e)})
        if data is None:
            return jsonify({"ok": False, "reason": "altium-not-running"})
        return jsonify({"ok": True, "data": data})

    @app.route("/api/action/clear_highlights", methods=["POST"])
    def action_clear_highlights() -> Response:
        try:
            data = _bridge_call("generic.clear_highlights", {}, timeout=5)
        except Exception as e:
            return jsonify({"ok": False, "reason": str(e)})
        if data is None:
            return jsonify({"ok": False, "reason": "altium-not-running"})
        return jsonify({"ok": True, "data": data})

    @app.route("/api/refresh/<topic>", methods=["POST"])
    def force_refresh(topic: str) -> Response:
        """Invalidate cached entries for one topic so the next GET re-fetches.

        The project / components / nets / messages tabs all read from the
        single bundled `project.dashboard_snapshot` cache entry, so every
        topic drops that. Review additionally drops its own derived caches.
        """
        extra = {
            "review": ("review_summary", "review_coverage",
                       "project.get_component_info_batch"),
        }
        if topic not in ("project", "components", "nets", "messages", "review"):
            return jsonify({"ok": False, "reason": "unknown topic"}), 400
        drop_prefixes = ("project.dashboard_snapshot",) + extra.get(topic, ())
        with _cache_lock:
            for k in list(_cache.keys()):
                if any(k.startswith(p) for p in drop_prefixes):
                    _cache.pop(k, None)
        return jsonify({"ok": True})

    @app.route("/api/entry/<rid>")
    def entry_detail(rid: str) -> Response:
        """Return the full payload prefix for one log entry.

        The activity.log truncates each response to 200 chars (Pascal-side
        ``Copy(ResponseContent, 1, 200)``). For richer inspection we also
        peek at bridge_trace.log around the request_id to confirm the
        full IPC story (POLL_SEEN / POLL_MATCH / extensions / timing).
        """
        entry = None
        with tailer._lock:
            for e in tailer.entries:
                if e.request_id == rid[:8]:
                    entry = e
                    break
        if entry is None:
            return jsonify({"ok": False, "error": "not found"}), 404

        trace_lines: list[str] = []
        try:
            trace_path = workspace_dir / "bridge_trace.log"
            if trace_path.exists():
                short = rid[:8]
                with open(trace_path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if short in line:
                            trace_lines.append(line.rstrip())
                            if len(trace_lines) >= 40:
                                break
        except OSError:
            pass

        return jsonify({
            "ok": True,
            "entry": entry.to_dict(),
            "payload_prefix": entry.payload_prefix,
            "trace": trace_lines,
        })

    @app.route("/events")
    def events() -> Response:
        wake, q = tailer.subscribe()

        def stream():
            # Send initial snapshot so a freshly-opened tab is populated.
            yield f"event: snapshot\ndata: {json.dumps(tailer.snapshot())}\n\n"
            try:
                while True:
                    wake.wait(timeout=15.0)
                    wake.clear()
                    drained: list[dict] = []
                    while q:
                        drained.append(q.popleft())
                    if drained:
                        yield f"data: {json.dumps(drained)}\n\n"
                    else:
                        # keep-alive comment
                        yield ": ping\n\n"
            finally:
                tailer.unsubscribe(wake)

        return Response(
            stream_with_context(stream()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    return app


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eda-agent dashboard",
        description="Local web dashboard for the EDA Agent MCP bridge.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--workspace", type=Path, default=None,
        help="Workspace directory (default: from get_config()).",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    app = create_app(workspace_dir=args.workspace)
    logger.info("dashboard serving on http://%s:%s/", args.host, args.port)
    # threaded=True so SSE doesn't lock out other requests.
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

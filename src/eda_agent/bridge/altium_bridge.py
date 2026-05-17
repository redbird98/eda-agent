# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba
"""Communication bridge between Python and Altium Designer via file-based IPC.

Per-request file scheme: Python writes ``request_<id>.json``, Altium scans the
workspace, processes it, and writes ``response_<id>.json``. Each side polls
only for files matching its own request ID, eliminating the stale-response
race that the older single-file scheme had when keep-alive pings overlapped
user tool calls.
"""

import json
import logging
import time
import uuid
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass, field

from ..config import get_config
from .process_manager import AltiumProcessManager
from .exceptions import (
    AltiumNotRunningError,
    AltiumTimeoutError,
    AltiumCommandError,
    AltiumProtocolError,
    ScriptNotLoadedError,
    raise_for_code,
)

logger = logging.getLogger("eda_agent.bridge")

# Wire protocol version. Must match scripts/altium/Main.pas:PROTOCOL_VERSION.
# Bump together; mismatch raises AltiumProtocolError on either side.
PROTOCOL_VERSION = 2

# Heartbeat-aware deadline extension cap. The Pascal dispatcher writes a
# progress_<id>.json marker while a handler runs; Python's poll loop extends
# the per-call deadline by another `timeout` window each time it observes
# fresh progress, up to this many extensions. With the default 10 s timeout
# this gives a 300 s ceiling per command -- plenty for heavy emit / compile
# passes while still catching runaway handlers.
_MAX_HEARTBEAT_EXTENSIONS = 30

# Thread pool for blocking I/O
_executor = ThreadPoolExecutor(max_workers=1)


def _trace_log(workspace_dir: Path, msg: str) -> None:
    """Append a line to workspace/bridge_trace.log. Never raises."""
    try:
        path = workspace_dir / "bridge_trace.log"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.time():.3f} {msg}\n")
    except Exception:
        pass


def _short_id() -> str:
    """Compact ID safe for filenames: 32 hex chars, no hyphens."""
    return uuid.uuid4().hex


@dataclass
class CommandRequest:
    """A command request to be sent to Altium."""

    command: str
    params: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=_short_id)

    def to_dict(self) -> dict:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "id": self.id,
            "command": self.command,
            "params": self.params,
        }


@dataclass
class CommandResponse:
    """A response from Altium."""

    id: str
    success: bool
    protocol_version: int = 0
    data: Any = None
    error: Optional[dict] = None

    @classmethod
    def from_dict(cls, data: dict) -> "CommandResponse":
        return cls(
            id=data.get("id", ""),
            success=data.get("success", False),
            protocol_version=data.get("protocol_version", 0),
            data=data.get("data"),
            error=data.get("error"),
        )


class AltiumBridge:
    """Handles communication with Altium Designer via file-based IPC.

    Flow:
        1. Python writes ``request_<id>.json`` (atomic via tmp + rename)
        2. Altium's polling script picks it up, deletes it, processes it
        3. Altium writes ``response_<id>.json`` (atomic via tmp + rename)
        4. Python polls for ``response_<id>.json`` and reads it

    With per-request files there is no shared file across callers, so
    concurrent calls (keep-alive ping + user tool) do not collide.
    """

    KEEPALIVE_INTERVAL = 30  # seconds
    DETACH_HINT_AFTER = 600  # seconds

    def __init__(self):
        self.config = get_config()
        self.process_manager = AltiumProcessManager()
        self._attached = False
        self._keepalive_thread: Optional[threading.Thread] = None
        self._keepalive_stop = threading.Event()
        self._attach_time: Optional[float] = None
        self._detach_hint_shown = False
        # Serialise pointer-file writes so two threads (keep-alive + user)
        # don't overwrite each other's request_pending.id between the body
        # write and the pointer write. The polling itself is per-response-ID
        # so the lock is only held for the brief request-publish step.
        self._publish_lock = threading.Lock()

    def ensure_workspace(self) -> None:
        self.config.ensure_workspace()
        self._sweep_orphan_responses()

    def _sweep_orphan_responses(self, max_age_seconds: float = 60.0) -> None:
        """Delete response_*.json files older than ``max_age_seconds``.

        Pascal's CleanupOrphanResponses can't enumerate the workspace
        (DelphiScript's FindFirst is broken), so the orphan-response
        sweep lives here. Python has reliable globbing and runs this on
        every ensure_workspace, typically once per attach + once per
        request. The age filter avoids racing with concurrent in-flight
        callers whose response is being polled right now.
        """
        cutoff = time.time() - max_age_seconds
        try:
            workspace = self.config.workspace_dir
            if not workspace.exists():
                return
            for path in workspace.glob("response_*.json"):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                except OSError:
                    pass
            # Stray .tmp files from older builds that did atomic rename
            for path in workspace.glob("response_*.json.tmp"):
                try:
                    path.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def is_altium_running(self) -> bool:
        return self.process_manager.is_altium_running()

    def get_altium_status(self) -> dict:
        process = self.process_manager.get_altium_info()
        if process:
            return {
                "running": True,
                "pid": process.pid,
                "exe_path": process.exe_path,
                "attached": self._attached,
            }
        return {
            "running": False,
            "pid": None,
            "exe_path": None,
            "attached": False,
        }

    def attach(self) -> bool:
        if not self.is_altium_running():
            raise AltiumNotRunningError()
        self.ensure_workspace()
        self._attached = True
        self._attach_time = time.monotonic()
        self._start_keepalive()
        logger.info("Attached to Altium Designer (file-based IPC, protocol v%d)", PROTOCOL_VERSION)
        return True

    def detach(self) -> None:
        self._stop_keepalive()
        self._attached = False
        self._attach_time = None
        self._detach_hint_shown = False

    def _ensure_keepalive(self) -> None:
        if self._attached and self._keepalive_thread and self._keepalive_thread.is_alive():
            return
        self._attached = True
        self._attach_time = time.monotonic()
        self._start_keepalive()

    def attach_duration(self) -> float:
        if self._attach_time is None:
            return 0.0
        return time.monotonic() - self._attach_time

    def _maybe_attach_detach_hint(self, command: str, data: Any) -> Any:
        if command in ("application.ping", "application.stop_server"):
            return data
        if self._detach_hint_shown:
            return data
        if self.attach_duration() < self.DETACH_HINT_AFTER:
            return data
        if not isinstance(data, dict):
            return data
        self._detach_hint_shown = True
        data = dict(data)
        data["_hint_detach"] = (
            "This MCP server has been holding Altium's scripting engine for "
            "over 10 minutes. When your Altium work is done, call "
            "detach_from_altium so Altium's own UI commands become responsive "
            "again. The user will have to re-launch StartMCPServer in Altium "
            "to run more MCP tools afterwards."
        )
        return data

    def _start_keepalive(self) -> None:
        self._stop_keepalive()
        self._keepalive_stop.clear()
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop, daemon=True, name="altium-keepalive"
        )
        self._keepalive_thread.start()
        logger.debug("Keep-alive thread started (interval=%ds)", self.KEEPALIVE_INTERVAL)

    def _stop_keepalive(self) -> None:
        if self._keepalive_thread and self._keepalive_thread.is_alive():
            self._keepalive_stop.set()
            self._keepalive_thread.join(timeout=5)
            logger.debug("Keep-alive thread stopped")
        self._keepalive_thread = None

    def _keepalive_loop(self) -> None:
        while not self._keepalive_stop.wait(self.KEEPALIVE_INTERVAL):
            if not self._attached:
                break
            try:
                self.send_command("application.ping", timeout=5.0)
            except Exception:
                logger.debug("Keep-alive ping failed, Altium may have stopped")
                break

    def _response_path(self, request_id: str) -> Path:
        return self.config.workspace_dir / f"response_{request_id}.json"

    def _request_path(self, request_id: str) -> Path:
        return self.config.workspace_dir / f"request_{request_id}.json"

    def _progress_path(self, request_id: str) -> Path:
        return self.config.workspace_dir / f"progress_{request_id}.json"

    def _publish_request(self, request: CommandRequest) -> None:
        """Publish a request to its own per-request file.

        Per-request request files (request_<id>.json) plus per-request
        response files (response_<id>.json) eliminate cross-caller races
        entirely; each caller has private filenames on both sides.
        Pascal enumerates request_*.json via FindFiles (the documented
        Altium DelphiScript helper). No lock needed: concurrent publishers
        write to different filenames.

        Atomic write: stage the body to ``.json.tmp`` (which Pascal's
        FindFiles glob ignores) then rename to the final filename. Without
        this, Pascal could pick up a half-written file and hit a sharing
        violation when its Reset() collides with Python's still-open
        write handle.
        """
        self.ensure_workspace()
        request_path = self._request_path(request.id)
        tmp_path = request_path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(request.to_dict(), f, indent=2)
        tmp_path.replace(request_path)
        logger.debug("Published request %s: %s", request.id, request.command)

    def _poll_response(self, request_id: str, timeout: float) -> CommandResponse:
        """Poll for response_<id>.json. Returns when it appears, raises on
        timeout.

        Heartbeat-aware. The Pascal dispatcher writes progress_<id>.json
        before invoking a handler and deletes it after the response is
        written. While progress_<id>.json exists, the polling loop is
        actively working on this request -- a legitimately slow handler is
        not a "polling loop dead" signal. We extend the deadline by another
        ``timeout`` window each time the heartbeat is still ticking, up to
        ``_MAX_HEARTBEAT_EXTENSIONS`` extensions before declaring the
        operation truly stuck. Default ceiling: 30 * 10 s = 5 minutes per
        command, which covers heavy emits / compiles without losing the
        early failure detection of the original 10 s timeout.

        Race window: Pascal writes the response THEN deletes the progress
        file, so at no instant are both absent. When the per-window
        deadline fires we re-check the response one more time after the
        progress check, in case the heartbeat was cleared between the two
        observations.
        """
        response_path = self._response_path(request_id)
        progress_path = self._progress_path(request_id)
        workspace_dir = self.config.workspace_dir
        poll_interval = self.config.poll_interval
        deadline = time.monotonic() + timeout
        extensions = 0
        poll_count = 0
        first_appearance: Optional[float] = None
        parse_errors = 0
        start = time.monotonic()

        _trace_log(
            workspace_dir,
            f"POLL_START id={request_id[:8]} timeout={timeout}s "
            f"interval={poll_interval}s",
        )

        while True:
            poll_count += 1
            if response_path.exists():
                if first_appearance is None:
                    first_appearance = time.monotonic() - start
                    _trace_log(
                        workspace_dir,
                        f"POLL_SEEN id={request_id[:8]} "
                        f"after={first_appearance*1000:.0f}ms polls={poll_count}",
                    )
                try:
                    with open(response_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except (json.JSONDecodeError, IOError, UnicodeDecodeError) as e:
                    parse_errors += 1
                    if parse_errors <= 3 or parse_errors % 50 == 0:
                        _trace_log(
                            workspace_dir,
                            f"POLL_PARSE_ERR id={request_id[:8]} "
                            f"err={type(e).__name__} count={parse_errors}",
                        )
                    # Fall through to the deadline check so a permanently
                    # corrupt file cannot wedge us in an infinite parse loop.
                else:
                    try:
                        response_path.unlink()
                    except OSError:
                        pass

                    elapsed = (time.monotonic() - start) * 1000
                    _trace_log(
                        workspace_dir,
                        f"POLL_MATCH id={request_id[:8]} elapsed={elapsed:.0f}ms "
                        f"polls={poll_count} parse_errs={parse_errors} "
                        f"extensions={extensions}",
                    )
                    return CommandResponse.from_dict(data)

            if time.monotonic() >= deadline:
                # Window expired. Heartbeat still ticking? extend.
                if progress_path.exists() and extensions < _MAX_HEARTBEAT_EXTENSIONS:
                    extensions += 1
                    elapsed = (time.monotonic() - start) * 1000
                    _trace_log(
                        workspace_dir,
                        f"POLL_EXTEND id={request_id[:8]} "
                        f"extensions={extensions}/{_MAX_HEARTBEAT_EXTENSIONS} "
                        f"elapsed={elapsed:.0f}ms",
                    )
                    deadline = time.monotonic() + timeout
                    continue
                # Race window: progress was cleared between deletion and
                # our check. Take one more response peek before giving up,
                # but ONLY if we have not already been parsing the file --
                # otherwise a permanently-corrupt response would loop here
                # forever. ``first_appearance`` being non-None means we've
                # already opened the file at least once and the top-of-
                # loop parse check is doing its job; no need to re-enter.
                if first_appearance is None and response_path.exists():
                    continue
                break

            time.sleep(poll_interval)

        elapsed = (time.monotonic() - start) * 1000
        _trace_log(
            workspace_dir,
            f"POLL_TIMEOUT id={request_id[:8]} elapsed={elapsed:.0f}ms "
            f"polls={poll_count} parse_errs={parse_errors} "
            f"extensions={extensions} "
            f"first_seen_ms={first_appearance*1000 if first_appearance else -1}",
        )
        if extensions >= _MAX_HEARTBEAT_EXTENSIONS:
            raise AltiumTimeoutError(
                f"Handler exceeded {_MAX_HEARTBEAT_EXTENSIONS} heartbeat "
                f"extensions ({_MAX_HEARTBEAT_EXTENSIONS * timeout:.0f}s "
                f"total); Altium is responding to keepalives but the command "
                f"never returned. The handler is likely stuck in an infinite "
                f"loop."
            )
        raise AltiumTimeoutError(
            f"No response within {timeout}s and no progress heartbeat. The "
            f"Altium polling loop is probably not running -- relaunch "
            f"StartMCPServer."
        )

    def _execute_command(self, command: str, params: dict[str, Any], timeout: float) -> Any:
        """Execute a command synchronously (blocking)."""
        request = CommandRequest(command=command, params=params)
        workspace_dir = self.config.workspace_dir

        _trace_log(workspace_dir, f"SEND cmd={command} id={request.id[:8]}")
        self._publish_request(request)
        logger.info("Sent command: %s (id=%s)", command, request.id[:8])

        response_path = self._response_path(request.id)
        try:
            response = self._poll_response(request.id, timeout)
        finally:
            # Always sweep our own response file on the way out,_poll_response
            # already deletes it on success, but a timeout or a late-arriving
            # response would otherwise be orphaned forever.
            try:
                if response_path.exists():
                    response_path.unlink()
            except OSError:
                pass

        if response.protocol_version and response.protocol_version != PROTOCOL_VERSION:
            raise AltiumProtocolError(
                client_version=PROTOCOL_VERSION,
                server_version=response.protocol_version,
            )

        if response.success:
            logger.info("Command %s succeeded", command)
            return self._maybe_attach_detach_hint(command, response.data)

        error = response.error or {}
        code = error.get("code", "UNKNOWN_ERROR")
        message = error.get("message", "Unknown error")
        details = error.get("details")
        logger.warning("Command %s failed: %s - %s", command, code, message)
        raise_for_code(code, message, details)

    def send_command(
        self,
        command: str,
        params: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        if not self.is_altium_running():
            raise AltiumNotRunningError()
        if timeout is None:
            timeout = self.config.poll_timeout
        self._ensure_keepalive()
        return self._execute_command(command, params or {}, timeout)

    async def send_command_async(
        self,
        command: str,
        params: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        if not self.is_altium_running():
            raise AltiumNotRunningError()
        if timeout is None:
            timeout = self.config.poll_timeout
        self._ensure_keepalive()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _executor,
            self._execute_command,
            command,
            params or {},
            timeout,
        )

    def ping(self) -> bool:
        try:
            self.send_command("application.ping", timeout=3.0)
            return True
        except (AltiumTimeoutError, AltiumCommandError, Exception):
            return False

    def ping_with_version(self) -> Optional[dict[str, Any]]:
        try:
            result = self.send_command("application.ping", timeout=3.0)
        except (AltiumTimeoutError, AltiumCommandError, Exception):
            return None
        if isinstance(result, dict):
            return result
        if result == "pong":
            return {"pong": True, "script_version": ""}
        return None


_bridge: Optional[AltiumBridge] = None


def get_bridge() -> AltiumBridge:
    global _bridge
    if _bridge is None:
        _bridge = AltiumBridge()
    return _bridge


def reset_bridge() -> None:
    global _bridge
    _bridge = None

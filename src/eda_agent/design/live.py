# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The live layout view: the board as the engines change it, and why.

An agent placing and routing a board makes its decisions out of sight:
a placement plan or a route job is invisible until it is applied, and
the design-session journal is text. This module is the shared state the
dashboard's Layout tab draws from, so the user watches the layout change
while the work happens instead of waiting for a reveal.

Three files under ``<workspace>/live``:

* ``board.json``: the latest snapshot of a ``LayoutBoard`` in mils, with
  a version that only rises, the note it was published with, and the
  difference from the snapshot before it (items added, moved, removed).
  Written whole to a temp file and renamed over the old one, so a reader
  never sees half a board.
* ``decisions.jsonl``: one decision per line, appended. A decision is
  logged BEFORE the action it describes, so the user can see what is
  about to happen and stop it.
* ``progress.json``: the last progress line of a running job, throttled.

Pure Python, no bridge: the publisher is the engine's own process, and
the dashboard (in process or standalone) only reads the files.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..atomicfile import discard, replace_with_retry

if TYPE_CHECKING:
    from ..layout.model import LayoutBoard

logger = logging.getLogger("eda_agent.design.live")

BOARD_FILE = "board.json"
DECISIONS_FILE = "decisions.jsonl"
PROGRESS_FILE = "progress.json"

#: At most this many ratsnest lines go into a snapshot. A board with
#: nothing routed yet has one per missing connection, and past a few
#: thousand the lines are a grey wash that says nothing.
MAX_RATS = 5000

#: Unrouted net names listed in a snapshot's status, worst first.
MAX_LISTED_NETS = 50

_lock = threading.Lock()
# Per live directory: the last document published there and the board it
# came from. The document gives the next version and the diff without
# reading the file back; the board lets a snapshot be republished.
_memory: dict[str, dict[str, Any]] = {}

#: Boards published recently, by version, so a tool that writes a job's
#: result to the EDA can republish exactly the board that job published
#: rather than whatever was published last.
KEEP_VERSIONS = 20
_history: dict[str, "OrderedDict[int, tuple]"] = {}
# Per live directory: the last decision's sequence number, read from the
# file once and counted on from there.
_seq: dict[str, int] = {}


def live_dir(root: str | Path | None = None) -> Path:
    """The live directory: ``root`` when given, else ``<workspace>/live``."""
    if root is not None:
        return Path(root)
    from ..config import get_config
    return get_config().workspace_dir / "live"


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def _r(v: float) -> float:
    return round(float(v), 2)


def _pts(pts) -> list[list[float]]:
    return [[_r(p[0]), _r(p[1])] for p in pts]


class _Keys:
    """Stable keys, with ``#n`` added when one repeats (two identical
    tracks, nine pads called SCREW on one part)."""

    def __init__(self):
        self.seen: dict[str, int] = {}

    def __call__(self, key: str) -> str:
        n = self.seen.get(key, 0)
        self.seen[key] = n + 1
        return key if n == 0 else f"{key}#{n}"


def _pad_owner(pad) -> str:
    return pad.comp or pad.key


def board_status(board: "LayoutBoard") -> tuple[dict[str, Any], list[list]]:
    """How far the board is routed, from the exact connectivity in
    ``layout.drc``, and the ratsnest of what is still missing.

    A net with two or more pads is routed when copper joins all its pads,
    partly routed when copper joins pads of at least two parts but not
    all of them, and not started otherwise. Two pads of one part that
    touch (an exposed pad drawn in pieces) do not make a net partly
    routed: nothing was routed to get them there.
    """
    from ..layout.drc import connectivity

    groups = connectivity(board)
    routed = partly = 0
    unrouted: dict[str, str] = {}
    missing: dict[str, int] = {}
    rats: list[list] = []
    for net, pads in board.pads_by_net().items():
        if len(pads) < 2:
            continue
        parts = groups.get(net, [])
        if len(parts) <= 1:
            routed += 1
            continue
        missing[net] = len(parts) - 1
        if any(len({_pad_owner(board.pads[i]) for i in g}) > 1 for g in parts):
            partly += 1
            unrouted[net] = "partly"
        else:
            unrouted[net] = "open"
        if len(rats) < MAX_RATS:
            rats.extend(_rats(board, net, parts))
    total = routed + len(unrouted)
    worst = sorted(missing, key=lambda n: (-missing[n], n))[:MAX_LISTED_NETS]
    status = {
        "nets": total,
        "routed": routed,
        "partly": partly,
        "not_started": total - routed - partly,
        "missing_connections": sum(missing.values()),
        "unrouted": unrouted,
        "worst": worst,
    }
    return status, rats[:MAX_RATS]


def _rats(board: "LayoutBoard", net: str, parts: list[set[int]]) -> list[list]:
    """The shortest lines joining a net's copper groups: Prim's tree over
    its pads, where pads copper already joins cost nothing."""
    import numpy as np

    idx = [i for g in parts for i in g]
    group = np.array([k for k, g in enumerate(parts) for _ in g])
    xy = np.array([(board.pads[i].x, board.pads[i].y) for i in idx], dtype=float)
    n = len(idx)
    best = np.full(n, np.inf)
    src = np.zeros(n, dtype=int)
    done = np.zeros(n, dtype=bool)
    out = []

    def take(g: int) -> None:
        members = np.nonzero(group == g)[0]
        done[members] = True
        for m in members:
            d = np.hypot(xy[:, 0] - xy[m, 0], xy[:, 1] - xy[m, 1])
            closer = d < best
            best[closer] = d[closer]
            src[closer] = m

    take(0)
    for _ in range(len(parts) - 1):
        cand = np.where(done, np.inf, best)
        j = int(np.argmin(cand))
        if not np.isfinite(cand[j]):
            break
        i = int(src[j])
        out.append([_r(xy[i, 0]), _r(xy[i, 1]), _r(xy[j, 0]), _r(xy[j, 1]), net])
        take(int(group[j]))
    return out


def snapshot(board: "LayoutBoard", extra: dict | None = None) -> dict[str, Any]:
    """The board as the Layout tab draws it, in mils.

    ``extra`` carries what the board does not: ``blocks`` (a list of
    ``{name, rect: [x0, y0, x1, y1], members}``), ``lanes`` (planned paths,
    ``{name, net, layer, width, path: [[x, y], ...]}``) and ``fixed`` (parts
    an engine holds in place besides the locked ones).
    """
    extra = extra or {}
    copper = board.copper_layers()
    copper_set = set(copper)
    fixed = set(extra.get("fixed") or ())
    blocks = []
    block_of: dict[str, str] = {}
    keys = _Keys()
    for b in extra.get("blocks") or ():
        if not isinstance(b, dict):
            continue
        name = str(b.get("name", ""))
        members = [str(m) for m in b.get("members") or ()]
        rect = b.get("rect")
        entry = {"k": keys(f"b:{name}"), "name": name, "members": members}
        if isinstance(rect, (list, tuple)) and len(rect) == 4:
            entry["rect"] = [_r(v) for v in rect]
        blocks.append(entry)
        for m in members:
            block_of.setdefault(m, name)

    pads_of: dict[str, list] = {}
    pads = []
    holes = []
    for p in board.pads:
        pads_of.setdefault(p.comp, []).append(p)
        cu = next((c for c in p.copper if c.layer == copper[0]), None) if copper else None
        cu = cu or (p.copper[0] if p.copper else None)
        x, y = p.x, p.y
        if cu is not None and (cu.offset_x or cu.offset_y):
            a = math.radians(p.rotation)
            x += cu.offset_x * math.cos(a) - cu.offset_y * math.sin(a)
            y += cu.offset_x * math.sin(a) + cu.offset_y * math.cos(a)
        layers = p.layers()
        side = "multi" if p.hole > 0 or len(layers) > 1 else (layers[0] if layers else "")
        name = f"p:{p.comp}.{p.name}" if p.comp else f"p:~{p.name}@{_r(p.x)},{_r(p.y)}"
        entry = {"k": keys(name), "comp": p.comp, "name": p.name,
                 "x": _r(x), "y": _r(y), "rot": _r(p.rotation), "side": side,
                 "net": p.net}
        if cu is not None:
            entry.update(w=_r(cu.w), h=_r(cu.h), shape=cu.shape)
            if cu.corner_pct:
                entry["cr"] = _r(cu.corner_pct)
        pads.append(entry)
        if p.hole > 0:
            hole = {"x": _r(p.x), "y": _r(p.y), "d": _r(p.hole), "plated": p.plated}
            if p.hole_type == "slot" and p.hole_width > p.hole:
                hole.update(slot=_r(p.hole_width), rot=_r(p.rotation + p.hole_rotation))
            holes.append(hole)

    comps = []
    for c in board.components:
        outline = list(c.courtyard)
        if len(outline) < 3:
            own = pads_of.get(c.ref, [])
            if own:
                half = [max((cu.w for cu in p.copper), default=0.0) / 2 for p in own]
                x0 = min(p.x - h for p, h in zip(own, half))
                x1 = max(p.x + h for p, h in zip(own, half))
                y0 = min(p.y - h for p, h in zip(own, half))
                y1 = max(p.y + h for p, h in zip(own, half))
            else:
                x0, y0, x1, y1 = c.x - 10, c.y - 10, c.x + 10, c.y + 10
            outline = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        xs = [q[0] for q in outline]
        ys = [q[1] for q in outline]
        comps.append({
            "k": keys(f"c:{c.ref}"), "ref": c.ref, "x": _r(c.x), "y": _r(c.y),
            "rot": _r(c.rotation), "side": c.side,
            "fixed": bool(c.locked or c.ref in fixed), "locked": bool(c.locked),
            "block": block_of.get(c.ref, ""), "footprint": c.footprint,
            "outline": _pts(outline),
            "bbox": [_r(min(xs)), _r(min(ys)), _r(max(xs)), _r(max(ys))],
        })

    tracks = []
    keepouts = []
    for t in board.tracks:
        if t.keepout:
            keepouts.append({"k": keys(f"kt:{t.layer}:{_r(t.x1)},{_r(t.y1)},{_r(t.x2)},{_r(t.y2)}"),
                             "type": "track", "layer": t.layer, "x1": _r(t.x1), "y1": _r(t.y1),
                             "x2": _r(t.x2), "y2": _r(t.y2), "w": _r(t.width)})
            continue
        if t.layer not in copper_set:
            continue
        a, b = (t.x1, t.y1), (t.x2, t.y2)
        if b < a:
            a, b = b, a
        tracks.append({"k": keys(f"t:{t.layer}:{t.net}:{_r(a[0])},{_r(a[1])},"
                                 f"{_r(b[0])},{_r(b[1])}:{_r(t.width)}"),
                       "layer": t.layer, "x1": _r(t.x1), "y1": _r(t.y1),
                       "x2": _r(t.x2), "y2": _r(t.y2), "w": _r(t.width), "net": t.net})
    arcs = []
    for a in board.arcs:
        geo = {"layer": a.layer, "cx": _r(a.cx), "cy": _r(a.cy), "r": _r(a.radius),
               "a1": _r(a.a1), "a2": _r(a.a2), "w": _r(a.width)}
        if a.keepout:
            keepouts.append({"k": keys(f"ka:{a.layer}:{geo['cx']},{geo['cy']},{geo['r']}"),
                             "type": "arc", **geo})
            continue
        if a.layer not in copper_set:
            continue
        arcs.append({"k": keys(f"a:{a.layer}:{a.net}:{geo['cx']},{geo['cy']},{geo['r']},"
                               f"{geo['a1']},{geo['a2']}:{geo['w']}"),
                     "net": a.net, **geo})
    vias = [{"k": keys(f"v:{_r(v.x)},{_r(v.y)}:{v.net}:{_r(v.diameter)}"),
             "x": _r(v.x), "y": _r(v.y), "d": _r(v.diameter), "hole": _r(v.hole),
             "net": v.net} for v in board.vias]
    pours = []
    for r in board.regions:
        if r.kind == "keepout" or r.keepout:
            keepouts.append({"k": keys(f"kr:{r.layer}:{r.source}"), "type": "region",
                             "layer": r.layer, "outline": _pts(r.outline)})
            continue
        if r.kind not in ("pour", "pour_boundary", "plane", "copper") or r.layer not in copper_set:
            continue
        pours.append({"k": keys(f"r:{r.kind}:{r.layer}:{r.net}:{r.source or r.comp}"),
                      "kind": r.kind, "layer": r.layer, "net": r.net,
                      "outline": _pts(r.outline), "holes": [_pts(h) for h in r.holes]})

    lanes = []
    for i, ln in enumerate(extra.get("lanes") or ()):
        if not isinstance(ln, dict):
            continue
        path = [q for q in ln.get("path") or () if isinstance(q, (list, tuple)) and len(q) >= 2]
        name = str(ln.get("name") or ln.get("net") or i)
        lanes.append({"k": keys(f"l:{name}"), "name": name, "net": str(ln.get("net", "")),
                      "layer": str(ln.get("layer", "")), "w": _r(ln.get("width") or 0),
                      "path": _pts(path)})

    pts = list(board.outline) or [q for c in comps for q in c["outline"]] or [(0, 0)]
    xs = [q[0] for q in pts]
    ys = [q[1] for q in pts]
    status, rats = board_status(board)
    return {
        "board": board.name, "source": board.source, "units": "mil",
        "bbox": [_r(min(xs)), _r(min(ys)), _r(max(xs)), _r(max(ys))],
        "outline": _pts(board.outline),
        "cutouts": [_pts(c) for c in board.cutouts],
        "layers": [{"name": l.name, "kind": l.kind, "order": l.order}
                   for l in sorted(board.layers, key=lambda l: l.order)],
        "components": comps, "pads": pads, "holes": holes,
        "tracks": tracks, "arcs": arcs, "vias": vias, "pours": pours,
        "keepouts": keepouts, "blocks": blocks, "lanes": lanes, "rats": rats,
        "status": status,
        "counts": {"components": len(comps), "pads": len(pads), "tracks": len(tracks),
                   "arcs": len(arcs), "vias": len(vias), "pours": len(pours)},
    }


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

#: The lists a diff walks, and the fields whose change counts as a move.
#: Copper that changes shape gets a new key, so for tracks, arcs and vias
#: a change is a removal and an addition; a part or a block keeps its
#: name and moves.
_DIFFED: dict[str, tuple[str, ...]] = {
    "components": ("x", "y", "rot", "side"),
    "pads": ("x", "y", "rot"),
    "tracks": (),
    "arcs": (),
    "vias": (),
    "pours": ("outline", "holes"),
    "blocks": ("rect",),
    "lanes": ("path",),
}


def diff_snapshots(prev: dict | None, cur: dict | None) -> dict[str, Any]:
    """Items added, moved and removed between two snapshots, by key.

    ``added`` and ``moved`` are keys into ``cur``; ``removed`` holds the
    removed items themselves, each with its list name under ``list``, so a
    page can draw them as ghosts after they are gone from ``cur``. With no
    previous snapshot nothing is tagged: a first board is not a change.
    """
    out: dict[str, Any] = {"added": [], "moved": [], "removed": []}
    if not prev or not cur:
        return out
    for name, fields in _DIFFED.items():
        before = {it["k"]: it for it in prev.get(name) or () if "k" in it}
        after = {it["k"]: it for it in cur.get(name) or () if "k" in it}
        for k, it in after.items():
            old = before.get(k)
            if old is None:
                out["added"].append(k)
            elif any(old.get(f) != it.get(f) for f in fields):
                out["moved"].append(k)
        for k, it in before.items():
            if k not in after:
                out["removed"].append({**it, "list": name})
    return out


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _now() -> tuple[float, str]:
    t = time.time()
    return t, datetime.fromtimestamp(t).isoformat(timespec="seconds")


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    try:
        replace_with_retry(tmp, path)
    except OSError:
        discard(tmp)
        raise


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def publish(board: "LayoutBoard", note: str = "", kind: str = "",
            extra: dict | None = None, *, root: str | Path | None = None) -> dict[str, Any]:
    """Write a snapshot of ``board`` as the next version of ``board.json``.

    ``note`` says what the snapshot is (it also goes into the decision log,
    tagged with the version), ``kind`` who published it (``place``,
    ``route``, ``manual``), ``extra`` what the board does not carry (see
    ``snapshot``). Returns the version, the status and the time it took.
    """
    t0 = time.perf_counter()
    d = live_dir(root)
    snap = snapshot(board, extra)
    with _lock:
        mem = _memory.get(str(d))
        prev = mem["doc"] if mem else _read_json(d / BOARD_FILE)
        if not isinstance(prev, dict):
            prev = None
        version = int(prev.get("version", 0)) + 1 if prev else 1
        prev_snap = prev.get("snapshot") if prev else None
        # Another board is a fresh start, not a change to every item.
        if isinstance(prev_snap, dict) and prev_snap.get("board") != snap["board"]:
            prev_snap = None
        t, ts = _now()
        doc = {"version": version, "time": t, "ts": ts, "note": note, "kind": kind,
               "snapshot": snap, "diff": diff_snapshots(prev_snap, snap)}
        _write_json(d / BOARD_FILE, doc)
        _memory[str(d)] = {"doc": doc, "board": board}
        hist = _history.setdefault(str(d), OrderedDict())
        hist[version] = (board, extra)
        while len(hist) > KEEP_VERSIONS:
            hist.popitem(last=False)
    if note:
        _append_decision(d, kind or "snapshot", note, version)
    st = snap["status"]
    return {"version": version, "board": snap["board"], "path": str(d / BOARD_FILE),
            "status": {k: st[k] for k in ("nets", "routed", "partly", "not_started")},
            "changes": {k: len(v) for k, v in doc["diff"].items()},
            "seconds": round(time.perf_counter() - t0, 3)}


def publish_safe(board: "LayoutBoard", note: str = "", kind: str = "",
                 extra: dict | None = None, *, root: str | Path | None = None) -> dict | None:
    """``publish`` for an engine job: a failure is logged and ignored.

    The view is a window on the work, never a reason for it to fail.
    """
    try:
        return publish(board, note, kind, extra, root=root)
    except Exception as exc:  # noqa: BLE001 - the job must go on
        logger.warning("live view publish failed (%s): %s", kind or "snapshot", exc)
        return None


def board_of(version: int, root: str | Path | None = None) -> tuple | None:
    """``(board, extra)`` as published as ``version``, while still kept."""
    try:
        v = int(version)
    except (TypeError, ValueError):
        return None
    return (_history.get(str(live_dir(root))) or {}).get(v)


def publish_applied(version, note: str, *, root: str | Path | None = None) -> dict | None:
    """Republish the board a job published as ``version``, now written to
    the EDA. None when that board is no longer kept (another server
    process, or more than KEEP_VERSIONS publishes since); a failure is
    logged and ignored like any engine publish."""
    kept = board_of(version, root) if version else None
    if kept is None:
        return None
    board, extra = kept
    return publish_safe(board, note, "applied", extra, root=root)


def last_board(root: str | Path | None = None) -> "LayoutBoard | None":
    """The board this process last published to ``root``, if any."""
    mem = _memory.get(str(live_dir(root)))
    return mem["board"] if mem else None


def note(section: str, text: str, *, root: str | Path | None = None,
         version: int | None = None) -> dict[str, Any]:
    """Append one decision to ``decisions.jsonl`` and return it.

    Call it BEFORE the action it describes. ``section`` groups decisions
    (``placement``, ``routing``, a block's name); ``version`` ties a
    snapshot's own note to the snapshot.
    """
    return _append_decision(live_dir(root), section, text, version)


def _append_decision(d: Path, section: str, text: str,
                     version: int | None = None) -> dict[str, Any]:
    with _lock:
        seq = _seq.get(str(d))
        if seq is None:
            last = decisions(0, root=d, limit=1, newest=True)
            seq = int(last[0]["seq"]) if last else 0
        seq += 1
        t, ts = _now()
        entry: dict[str, Any] = {"seq": seq, "time": t, "ts": ts,
                                 "section": str(section), "text": str(text)}
        if version is not None:
            entry["version"] = version
        d.mkdir(parents=True, exist_ok=True)
        path = d / DECISIONS_FILE
        # After a crash mid-append the file ends in half a line; appended
        # straight on, the next entry joined it and both were lost.
        lead = "\n" if _ends_mid_line(path) else ""
        with path.open("a", encoding="utf-8") as f:
            f.write(lead + json.dumps(entry) + "\n")
        _seq[str(d)] = seq
    return entry


def _ends_mid_line(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                return False
            f.seek(-1, os.SEEK_END)
            return f.read(1) != b"\n"
    except OSError:
        return False


def decisions(since: int = 0, *, root: str | Path | None = None,
              limit: int = 500, newest: bool = False) -> list[dict[str, Any]]:
    """Decisions with a sequence number above ``since``, oldest first.

    At most ``limit``: the oldest ones, or the newest when ``newest``.
    A torn last line (a crash mid-append) is skipped.
    """
    path = live_dir(root) / DECISIONS_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and int(entry.get("seq", 0)) > since:
            out.append(entry)
    return out[-limit:] if newest else out[:limit]


def progress(text: str, kind: str = "", *, root: str | Path | None = None) -> None:
    """Record a running job's latest progress line."""
    t, ts = _now()
    _write_json(live_dir(root) / PROGRESS_FILE,
                {"time": t, "ts": ts, "kind": kind, "text": str(text)})


def progress_logger(kind: str, every: float = 2.0, *,
                    root: str | Path | None = None) -> Callable[[str], None]:
    """A ``log`` callback for an engine: writes progress at most once per
    ``every`` seconds, drops the lines between, and never raises."""
    last = [-math.inf]

    def log(text: str) -> None:
        logger.debug("%s: %s", kind, text)
        now = time.monotonic()
        if now - last[0] < every:
            return
        last[0] = now
        try:
            progress(text, kind, root=root)
        except Exception as exc:  # noqa: BLE001 - progress is best effort
            logger.debug("live progress write failed: %s", exc)

    return log


# ---------------------------------------------------------------------------
# Reading (the dashboard)
# ---------------------------------------------------------------------------

def read_board(root: str | Path | None = None) -> dict | None:
    """The latest published document, or None when there is none."""
    doc = _read_json(live_dir(root) / BOARD_FILE)
    return doc if isinstance(doc, dict) else None


def read_progress(root: str | Path | None = None) -> dict | None:
    doc = _read_json(live_dir(root) / PROGRESS_FILE)
    return doc if isinstance(doc, dict) else None

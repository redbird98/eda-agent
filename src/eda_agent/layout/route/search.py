# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Shortest paths on a region of the routing grid.

A search region is a set of cells (a mask inside a bounding box) on every
routing layer, plus one via node per cell. Each layer cell joins its
eight neighbours in the region; a via node joins the cell above it on
every layer, so any change of layer through one via costs one via. Costs
are per cell, entered: the step length times the cost of the cell
stepped onto.

Only the region's cells become graph nodes. scipy's Dijkstra pays for
every node it is given before it settles the first (about 12 ms for half
a million), so a long diagonal connection searched over its whole
bounding box, on six layers, paid for a million nodes it had no use for.
A band or corridor round the route holds a tenth of them.

The graph is built as a fixed-degree CSR matrix with numpy (no Python
loop per node) and searched from the new terminal towards the tree
already routed, stopping at a distance limit. A cell that may not be
used carries a weight far above any limit, so it is never entered; a move
out of the region is an edge back to the same cell with that weight,
which keeps every row the same length.

scipy has no A*, but Dijkstra on reduced weights w - h(u) + h(v) IS A*
when h is a consistent estimate of the cost still to go. Here h is the
straight-line distance to the nearest target cell, from a Euclidean
distance transform: every move costs at least its length, so h never
overestimates and the reduced weights stay non-negative. scipy's
Dijkstra does not stop at a target, it settles everything under its
limit, so on reduced costs the limit is the detour allowed beyond a
straight line.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

BLOCKED = 1e12

#: The eight in-plane moves: (dx, dy, length in cells).
MOVES = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
         (1, 1, math.sqrt(2)), (1, -1, math.sqrt(2)),
         (-1, 1, math.sqrt(2)), (-1, -1, math.sqrt(2)))


class Window:
    """A box of the grid, and the cells in it that the search may use."""

    def __init__(self, i0: int, j0: int, w: int, h: int, L: int,
                 mask: np.ndarray | None = None):
        self.i0, self.j0, self.w, self.h, self.L = i0, j0, w, h, L
        self.mask = np.ones((h, w), dtype=bool) if mask is None else mask
        self.cy, self.cx = np.nonzero(self.mask)
        self.M = len(self.cy)
        self.rank = np.full((h, w), -1, dtype=np.int64)
        self.rank[self.cy, self.cx] = np.arange(self.M)

    def __repr__(self):
        return f"Window(i0={self.i0}, j0={self.j0}, w={self.w}, h={self.h}, L={self.L}, cells={self.M})"

    @property
    def n_layer(self) -> int:
        return self.L * self.M

    @property
    def size(self) -> int:
        return self.n_layer + self.M

    @property
    def slices(self):
        return (slice(self.j0, self.j0 + self.h), slice(self.i0, self.i0 + self.w))

    def contains(self, y, x) -> np.ndarray:
        y, x = np.asarray(y), np.asarray(x)
        inbox = (y >= self.j0) & (y < self.j0 + self.h) & (x >= self.i0) & (x < self.i0 + self.w)
        out = np.zeros(inbox.shape, dtype=bool)
        out[inbox] = self.mask[y[inbox] - self.j0, x[inbox] - self.i0]
        return out

    def node(self, l, y, x):
        """Graph node of grid cell (l, y, x); the cell must be in the region."""
        r = self.rank[np.asarray(y) - self.j0, np.asarray(x) - self.i0]
        return np.asarray(l) * self.M + r

    def hub(self, y, x):
        return self.n_layer + self.rank[np.asarray(y) - self.j0, np.asarray(x) - self.i0]

    def decode(self, n: int) -> tuple[int, int, int]:
        """(layer, y, x) in grid indices; layer -1 for a via node."""
        if n >= self.n_layer:
            r = n - self.n_layer
            return -1, self.j0 + int(self.cy[r]), self.i0 + int(self.cx[r])
        l, r = divmod(n, self.M)
        return l, self.j0 + int(self.cy[r]), self.i0 + int(self.cx[r])


def build_graph(win: Window, cell_cost: np.ndarray, via_cost: np.ndarray,
                move_factor: np.ndarray, pitch: float,
                potential: np.ndarray | None = None) -> csr_matrix:
    """``cell_cost`` (L, M) and ``via_cost`` (M,) per region cell: inf
    where barred.

    ``move_factor`` (L, 8): the per-layer cost multiplier of each move,
    which is how a layer prefers one direction. ``potential`` (M,), in
    the same units as the costs, turns the search into A* (see above);
    it must never exceed the true cost to go.
    """
    L, M, h, w = win.L, win.M, win.h, win.w
    cy, cx = win.cy, win.cx
    cc = np.minimum(cell_cost, BLOCKED)
    vc = np.minimum(via_cost, BLOCKED)
    idx = np.empty((L, M, 9), dtype=np.int32)
    wt = np.empty((L, M, 9), dtype=np.float64)
    selfid = np.arange(M, dtype=np.int64)
    layer_base = (np.arange(L, dtype=np.int64) * M)[:, None]
    for k, (dx, dy, length) in enumerate(MOVES):
        ny, nx = cy + dy, cx + dx
        ok = (ny >= 0) & (ny < h) & (nx >= 0) & (nx < w)
        r = np.full(M, -1, dtype=np.int64)
        r[ok] = win.rank[ny[ok], nx[ok]]
        ok &= r >= 0
        nb = np.where(ok, r, selfid)
        idx[:, :, k] = layer_base + nb[None, :]
        wk = cc[:, nb] * ((length * pitch) * move_factor[:, k][:, None])
        if potential is not None:
            wk += (potential[nb] - potential)[None, :]
            np.maximum(wk, 0.0, out=wk)
        wt[:, :, k] = np.where(ok[None, :], wk, BLOCKED)
    blocked_cell = cc >= BLOCKED
    idx[:, :, 8] = win.n_layer + selfid[None, :]
    wt[:, :, 8] = vc[None, :] / 2.0 + blocked_cell * BLOCKED
    hidx = (layer_base + selfid[None, :]).T                 # (M, L)
    hwt = vc[:, None] / 2.0 + blocked_cell.T * BLOCKED
    data = np.concatenate([wt.reshape(-1), hwt.reshape(-1)])
    indices = np.concatenate([idx.reshape(-1), hidx.reshape(-1).astype(np.int32)])
    nL = L * M
    indptr = np.concatenate([np.arange(0, nL * 9, 9, dtype=np.int32),
                             nL * 9 + np.arange(0, M * L + 1, L, dtype=np.int32)])
    return csr_matrix((data, indices, indptr), shape=(win.size, win.size), copy=False)


def shortest_path(graph: csr_matrix, sources: np.ndarray, targets: np.ndarray,
                  limit: float) -> list[int] | None:
    """Cheapest path from any source to any target, as graph node ids."""
    if len(sources) == 0 or len(targets) == 0:
        return None
    dist, pred, _ = dijkstra(graph, directed=True, indices=np.unique(sources),
                             return_predecessors=True, min_only=True, limit=limit)
    td = dist[targets]
    k = int(np.argmin(td))
    if not np.isfinite(td[k]) or td[k] >= BLOCKED:
        return None
    n = int(targets[k])
    path = [n]
    while pred[n] >= 0:
        n = int(pred[n])
        path.append(n)
    path.reverse()
    return path

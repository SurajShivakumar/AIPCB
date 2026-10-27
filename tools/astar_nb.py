"""Compiled A* search for maze_route.py (numba). Same rules as the pure-Python search it replaces:
8-way moves on each routing layer, a layer change (through via) where via_free allows it, a penalty per
change of heading so paths come out as long straight runs, and a weighted heuristic.

search(...) returns the path as an (n, 3) int array of (layer, i, j) cells, or an empty array if there is no
path within max_expand node expansions.
"""

from __future__ import annotations

import numpy as np
from numba import njit

DI = np.array([1, -1, 0, 0, 1, 1, -1, -1], dtype=np.int64)
DJ = np.array([0, 0, 1, -1, 1, -1, 1, -1], dtype=np.int64)
W = np.array([1.0, 1.0, 1.0, 1.0, 1.4142, 1.4142, 1.4142, 1.4142])


HEAP_CAP = 12_000_000     # fixed heap: 12M entries x 12 bytes = ~150 MB, whatever the board does


@njit(cache=True)
def _push(hk, hv, n, key, val):
    i = n
    hk[i] = key; hv[i] = val
    while i > 0:
        p = (i - 1) >> 1
        if hk[p] <= hk[i]:
            break
        hk[p], hk[i] = hk[i], hk[p]
        hv[p], hv[i] = hv[i], hv[p]
        i = p
    return n + 1


@njit(cache=True)
def _pop(hk, hv, n):
    key, val = hk[0], hv[0]
    n -= 1
    hk[0] = hk[n]; hv[0] = hv[n]
    i = 0
    while True:
        l = 2 * i + 1
        if l >= n:
            break
        c = l
        if l + 1 < n and hk[l + 1] < hk[l]:
            c = l + 1
        if hk[i] <= hk[c]:
            break
        hk[c], hk[i] = hk[i], hk[c]
        hv[c], hv[i] = hv[i], hv[c]
        i = c
    return key, val, n


@njit(cache=True)
def _search(free, via_free, starts, goal, nx, ny, gi, gj, via_cost, turn_cost, h_weight, max_expand, di, dj, w,
            extra):
    nl = free.shape[0]
    ncell = nx * ny
    nstate = nl * ncell * 9                        # (layer, cell, heading 0..7 or 8 = none)
    dist = np.full(nstate, np.inf, dtype=np.float32)
    prev = np.full(nstate, -1, dtype=np.int32)
    hk = np.empty(HEAP_CAP, dtype=np.float64)
    hv = np.empty(HEAP_CAP, dtype=np.int32)
    n = 0
    for k in range(starts.shape[0]):
        l, i, j = starts[k, 0], starts[k, 1], starts[k, 2]
        st = ((l * ncell) + j * nx + i) * 9 + 8
        dist[st] = 0.0
        n = _push(hk, hv, n, h_weight * np.sqrt((i - gi) ** 2 + (j - gj) ** 2), st)
    found = -1
    expanded = 0
    while n > 0:
        _, st, n = _pop(hk, hv, n)
        d = dist[st]
        expanded += 1
        if expanded > max_expand:
            break
        hd = st % 9
        cellid = st // 9
        l = cellid // ncell
        c = cellid % ncell
        j = c // nx
        i = c % nx
        if goal[l, c]:
            found = st
            break
        for k in range(8):
            ii = i + di[k]
            jj = j + dj[k]
            if ii < 0 or ii >= nx or jj < 0 or jj >= ny:
                continue
            cc = jj * nx + ii
            if not free[l, cc]:
                continue
            nd = d + w[k] + extra[l, cc]
            if hd != 8 and hd != k:
                nd += turn_cost
            ns = ((l * ncell) + cc) * 9 + k
            if nd < dist[ns]:
                if n >= HEAP_CAP:
                    return np.zeros((0, 3), dtype=np.int64)     # out of budget: treat as no path
                dist[ns] = nd
                prev[ns] = st
                n = _push(hk, hv, n, nd + h_weight * np.sqrt((ii - gi) ** 2 + (jj - gj) ** 2), ns)
        if nl > 1 and via_free[c]:
            for lj in range(nl):
                if lj == l or not free[lj, c]:
                    continue
                nd = d + via_cost + extra[lj, c]
                ns = ((lj * ncell) + c) * 9 + 8
                if nd < dist[ns]:
                    if n >= HEAP_CAP:
                        return np.zeros((0, 3), dtype=np.int64)
                    dist[ns] = nd
                    prev[ns] = st
                    n = _push(hk, hv, n, nd + h_weight * np.sqrt((i - gi) ** 2 + (j - gj) ** 2), ns)
    if found < 0:
        return np.zeros((0, 3), dtype=np.int64)
    n = 0
    s = found
    while s >= 0:
        n += 1
        s = prev[s]
    out = np.zeros((n, 3), dtype=np.int64)
    s = found
    k = n - 1
    while s >= 0:
        cellid = s // 9
        out[k, 0] = cellid // ncell
        c = cellid % ncell
        out[k, 1] = c % nx
        out[k, 2] = c // nx
        k -= 1
        s = prev[s]
    return out


def search(free_masks, via_free_mask, starts, goals, nx, ny, gi, gj, via_cost, turn_cost, h_weight, max_expand,
           extra_cost=None):
    """free_masks: list of (ny, nx) bool per layer; via_free_mask: (ny, nx) bool;
    starts / goals: iterables of (layer, i, j); extra_cost: optional list of (ny, nx) per-cell costs per layer
    (rip-up: crossing another net's copper is allowed but expensive)."""
    free = np.ascontiguousarray(np.stack([m.reshape(-1) for m in free_masks]).astype(np.uint8))
    via_free = np.ascontiguousarray(via_free_mask.reshape(-1).astype(np.uint8))
    goal = np.zeros_like(free)
    for l, i, j in goals:
        goal[l, j * nx + i] = 1
    st = np.array(list(starts), dtype=np.int64).reshape(-1, 3)
    if extra_cost is None:
        extra = np.zeros(free.shape, dtype=np.float32)
    else:
        extra = np.ascontiguousarray(np.stack([c.reshape(-1) for c in extra_cost]).astype(np.float32))
    path = _search(free, via_free, st, goal, nx, ny, gi, gj, float(via_cost), float(turn_cost), float(h_weight),
                   int(max_expand), DI, DJ, W, extra)
    return [tuple(int(v) for v in row) for row in path]

"""Route specific unrouted connections with an A* maze router (for what Freerouting leaves).

Run with KiCad's Python:
    vendor/KiCAD-MCP-Server/venv/Scripts/python.exe tools/maze_route.py <board> NET:END:END [...]
      [--width 0] [--clearance 0] [--grid 0.1] [--layers F.Cu,B.Cu]

END is REF.PAD, or x,y (mm): an existing same-net via / track end there, else a new escape via.
--width/--clearance 0 (the default) take each net's own net-class values from the .kicad_pro.
Obstacles are every pad / track / via of other nets, inflated by clearance + half the track width.
Zones are ignored (they are refilled around the new copper). Through vias are checked against all
copper layers. Rule areas that forbid tracks (or vias) are honoured, so the router never breaks the
board's own keep-outs. Vias cost extra so the router prefers staying on one layer. The path is
simplified to straight 0/45/90-degree segments before writing. Jobs run in order; each sees the
copper the earlier ones added.
"""

from __future__ import annotations

import sys as _sys, os as _os; _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _quiet  # noqa: E402,F401  - no console windows for child processes on Windows

import heapq
import math
import sys

import numpy as np
import pcbnew
import wx

wx.DisableAsserts()

try:                                   # compiled A* if numba is installed (setup.ps1 installs it)
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
    import astar_nb
except Exception:                      # noqa: BLE001 - fall back to the pure-Python search
    astar_nb = None

ARGS = {"--width": 0.0, "--clearance": 0.0, "--grid": 0.1}
PLANE_NETS = ("GND", "/VM", "/BATN")
RIP_COST = 4.0      # per grid cell of other-net copper a rip-up path crosses (high values stall the search)
RIP_H_WEIGHT = 2.0  # rip-up search heads harder for the goal: it only has to find *a* way, the real route comes after
LAYER_IDS = {"F.Cu": pcbnew.F_Cu, "In1.Cu": pcbnew.In1_Cu, "In2.Cu": pcbnew.In2_Cu, "B.Cu": pcbnew.B_Cu}
COPPER = (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu)
VIA_D, VIA_DRILL, VIA_COST = 0.6, 0.3, 40.0
EDGE_KEEP = 0.5
LANE = 0.8           # escape lane kept clear straight out from every fine-pitch pin (other nets may not cross it)
FINE = 0.35          # a pad whose short side is below this is a fine-pitch pin
TURN_COST = 3.0      # per change of direction: long straight runs instead of A* staircases
MAX_EXPAND = 3_000_000
MAX_EXPAND_NB = 8_000_000    # compiled search: big budget, bounded memory (astar_nb.HEAP_CAP)
H_WEIGHT = 1.3       # weighted A*: heads for the goal; paths stay near-shortest, search shrinks a lot
HOLE_KEEP = 3.0
HOLE_PITCH = 0.85    # via centre spacing: 0.3 mm drills + 0.5 mm hole-to-hole + margin      # mounting holes: screw head / washer


def net_class_rules(board_path):
    """{netname -> (track_width, clearance)} resolver from the project's net classes (mm).
    KiCad 9's Python hands back NETCLASS as an opaque pointer, so read the .kicad_pro instead."""
    import fnmatch
    import json
    from pathlib import Path
    pro = Path(board_path).with_suffix(".kicad_pro")
    d = json.loads(pro.read_text(encoding="utf-8")) if pro.exists() else {}
    ns = d.get("net_settings", {})
    classes = {c["name"]: (c.get("track_width", 0.2), c.get("clearance", 0.2)) for c in ns.get("classes", [])}
    default = classes.get("Default", (0.2, 0.2))
    pats = [(p["pattern"], p["netclass"]) for p in ns.get("netclass_patterns") or []]

    def rules(net):
        for pat, cls in pats:
            if fnmatch.fnmatchcase(net, pat) and cls in classes:
                return classes[cls]
        return default
    return rules


class Grid:
    def __init__(self, x0, y0, nx, ny, g):
        self.x0, self.y0, self.nx, self.ny, self.g = x0, y0, nx, ny, g
        self.xs = x0 + np.arange(nx) * g
        self.ys = y0 + np.arange(ny) * g

    def cell(self, x, y):
        return int(round((x - self.x0) / self.g)), int(round((y - self.y0) / self.g))

    def window(self, x0, y0, x1, y1):
        i0 = max(0, int(math.floor((x0 - self.x0) / self.g)))
        j0 = max(0, int(math.floor((y0 - self.y0) / self.g)))
        i1 = min(self.nx, int(math.ceil((x1 - self.x0) / self.g)) + 1)
        j1 = min(self.ny, int(math.ceil((y1 - self.y0) / self.g)) + 1)
        return i0, j0, i1, j1

    def rect(self, m, x0, y0, x1, y1):
        # only cells whose centre lies inside: rounding outward would close the gaps between
        # 0.5 mm-pitch pins at a 0.05 mm grid
        e = 1e-9
        i0 = max(0, int(math.ceil((x0 - self.x0) / self.g - e)))
        j0 = max(0, int(math.ceil((y0 - self.y0) / self.g - e)))
        i1 = min(self.nx, int(math.floor((x1 - self.x0) / self.g + e)) + 1)
        j1 = min(self.ny, int(math.floor((y1 - self.y0) / self.g + e)) + 1)
        if i0 < i1 and j0 < j1:
            m[j0:j1, i0:i1] = True

    def capsule(self, m, ax, ay, bx, by, r):
        i0, j0, i1, j1 = self.window(min(ax, bx) - r, min(ay, by) - r, max(ax, bx) + r, max(ay, by) + r)
        if i0 >= i1 or j0 >= j1:
            return
        X = self.xs[i0:i1][None, :]; Y = self.ys[j0:j1][:, None]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = np.zeros_like(X + Y) if L2 == 0 else np.clip(((X - ax) * dx + (Y - ay) * dy) / L2, 0, 1)
        d2 = (X - (ax + t * dx)) ** 2 + (Y - (ay + t * dy)) ** 2
        m[j0:j1, i0:i1] |= d2 <= r * r

    def polygon(self, rings):
        """Even-odd fill of a set of rings (outline + holes)."""
        X = self.xs[None, :]; Y = self.ys[:, None]
        inside = np.zeros((self.ny, self.nx), dtype=bool)
        for pts in rings:
            n = len(pts)
            for k in range(n):
                (x1, y1), (x2, y2) = pts[k], pts[(k + 1) % n]
                if y1 == y2:
                    continue
                cond = (y1 > Y) != (y2 > Y)
                xint = x1 + (Y - y1) * (x2 - x1) / (y2 - y1)
                inside ^= cond & (X < xint)
        return inside


def _clear_line(a, b, free, nx):
    """Every grid cell on the straight a->b run (0/45/90 degrees only) is free."""
    di, dj = b[1] - a[1], b[2] - a[2]
    n = max(abs(di), abs(dj))
    if n == 0:
        return True
    if not (di == 0 or dj == 0 or abs(di) == abs(dj)):
        return False
    si, sj = di // n, dj // n
    return all(free[(a[2] + sj * k) * nx + a[1] + si * k] for k in range(n + 1))


def smooth(nodes, free_l, nx):
    """Turn the A* cell path into few, long 0/45/90-degree segments: from each corner, jump to the farthest
    later node on the same layer that a clear straight run reaches (A* ties otherwise give staircases)."""
    out = [nodes[0]]
    k = 0
    while k < len(nodes) - 1:
        if nodes[k + 1][0] != nodes[k][0]:          # layer change (via): keep it
            k += 1; out.append(nodes[k]); continue
        end = k + 1
        while end + 1 < len(nodes) and nodes[end + 1][0] == nodes[k][0]:
            end += 1
        best = k + 1
        for m in range(end, k + 1, -1):
            if _clear_line(nodes[k], nodes[m], free_l[nodes[k][0]], nx):
                best = m; break
        k = best
        out.append(nodes[k])
    return out


def dilate(m, r):
    if r <= 0:
        return m.copy()
    out = m.copy()
    for s in range(1, r + 1):
        out[:, s:] |= m[:, :-s]; out[:, :-s] |= m[:, s:]
    m2 = out.copy()
    for s in range(1, r + 1):
        out[s:, :] |= m2[:-s, :]; out[:-s, :] |= m2[s:, :]
    return out


def main() -> None:
    argv = sys.argv[1:]
    layers = (pcbnew.F_Cu, pcbnew.B_Cu)
    if "--layers" in argv:
        i = argv.index("--layers")
        layers = tuple(LAYER_IDS[n] for n in argv[i + 1].split(","))
        del argv[i:i + 2]
    ripup = "--ripup" in argv
    argv = [a for a in argv if a != "--ripup"]
    for k in list(ARGS):
        if k in argv:
            i = argv.index(k); ARGS[k] = float(argv[i + 1]); del argv[i:i + 2]
    path, jobs = argv[0], argv[1:]
    g = ARGS["--grid"]
    b = pcbnew.LoadBoard(path)
    class_rules = net_class_rules(path)
    # KiCad 9's Python wrapper hands back stale proxies from GetFootprints()/GetTracks() after a Remove(), so work
    # from one footprint snapshot and a track list we keep up to date ourselves
    FPS = list(b.GetFootprints())
    TRK = list(b.GetTracks())
    RIPPED = []      # rip-up victims: out of TRK at once, out of the board only just before saving
    mm = pcbnew.ToMM
    bb = b.GetBoardEdgesBoundingBox()
    G = Grid(mm(bb.GetX()), mm(bb.GetY()), int(mm(bb.GetWidth()) / g) + 1, int(mm(bb.GetHeight()) / g) + 1, g)
    W, H = G.nx * g, G.ny * g

    # ---- rule areas, rasterised once
    no_track = {L: np.zeros((G.ny, G.nx), dtype=bool) for L in COPPER}
    no_via = np.zeros((G.ny, G.nx), dtype=bool)
    areas = [z for z in b.Zones() if z.GetIsRuleArea()]
    for fp in FPS:
        areas += [z for z in fp.Zones() if z.GetIsRuleArea()]
    for z in areas:
        tl = [L for L in COPPER if z.IsOnLayer(L)] if z.GetDoNotAllowTracks() else []
        if not tl and not z.GetDoNotAllowVias():
            continue
        ol = z.Outline()
        rings = []
        for oi in range(ol.OutlineCount()):
            ch = ol.Outline(oi)
            rings.append([(mm(ch.CPoint(k).x), mm(ch.CPoint(k).y)) for k in range(ch.PointCount())])
            for h in range(ol.HoleCount(oi)):
                hh = ol.Hole(oi, h)
                rings.append([(mm(hh.CPoint(k).x), mm(hh.CPoint(k).y)) for k in range(hh.PointCount())])
        inside = G.polygon(rings)
        for L in tl:
            no_track[L] |= inside
        if z.GetDoNotAllowVias():
            no_via |= inside
    keepout_cache = {}

    # Phase pours are off limits to foreign signals: a net may only enter a phase pour region if it already has
    # a pad or via inside it (that phase's own gate, BEMF and phase-sense nets). (esc_checks flags the same.)
    phase_regions = []          # (layer, mask, allowed netcodes)
    for z in b.Zones():
        if z.GetIsRuleArea() or not z.GetNetname().startswith("/Phase_"):
            continue
        ol = z.Outline()
        rings = [[(mm(ol.Outline(0).CPoint(k).x), mm(ol.Outline(0).CPoint(k).y))
                  for k in range(ol.Outline(0).PointCount())]]
        mask = G.polygon(rings)
        allowed = {z.GetNetCode()}
        for fp in FPS:
            for p in fp.Pads():
                if ol.Contains(p.GetPosition()):
                    allowed.add(p.GetNetCode())
        for t in TRK:
            if t.GetClass() == "PCB_VIA" and ol.Contains(t.GetPosition()):
                allowed.add(t.GetNetCode())
        for L in COPPER:
            if z.IsOnLayer(L):
                phase_regions.append((L, mask, allowed))
    phase_cache = {}

    def foreign_phase(L, netcode, r):
        """Cells of phase pours on layer L that `netcode` may not enter, grown by r cells."""
        key = (L, netcode, r)
        if key not in phase_cache:
            m_ = None
            for L_, mask, allowed in phase_regions:
                if L_ == L and netcode not in allowed:
                    m_ = mask.copy() if m_ is None else (m_ | mask)
            phase_cache[key] = dilate(m_, r) if m_ is not None else None
        return phase_cache[key]
    no_via_grown = dilate(no_via, int(math.ceil((VIA_D / 2) / g)))   # the whole via disc stays out, not just its centre
    # no via inside (or overlapping) any SMD pad: via-in-pad wicks solder away from the joint
    for fp in FPS:
        for p in fp.Pads():
            if p.GetAttribute() == pcbnew.PAD_ATTRIB_SMD:
                r = p.GetBoundingBox()
                G.rect(no_via_grown, mm(r.GetLeft()) - VIA_D / 2, mm(r.GetTop()) - VIA_D / 2,
                       mm(r.GetRight()) + VIA_D / 2, mm(r.GetBottom()) + VIA_D / 2)

    # escape lanes in front of fine-pitch pins (QFN/QFP): (netcode, pad, lane rectangle)
    lanes = []
    for fp in FPS:
        fine = [p for p in fp.Pads() if min(mm(p.GetSize().x), mm(p.GetSize().y)) < FINE]
        if len(fine) < 8:
            continue
        c = fp.GetPosition(); cx, cy = mm(c.x), mm(c.y)
        for p in fine:
            r = p.GetBoundingBox()
            x0, y0, x1, y1 = mm(r.GetLeft()), mm(r.GetTop()), mm(r.GetRight()), mm(r.GetBottom())
            px, py = (x0 + x1) / 2, (y0 + y1) / 2
            if (x1 - x0) > (y1 - y0):        # long axis horizontal: pin on the left / right edge
                lane = (x1, y0, x1 + LANE, y1) if px > cx else (x0 - LANE, y0, x0, y1)
            else:
                lane = (x0, y1, x1, y1 + LANE) if py > cy else (x0, y0 - LANE, x1, y0)
            lanes.append((p.GetNetCode(), p, lane))

    def keepout(L, r):
        if (L, r) not in keepout_cache:
            keepout_cache[(L, r)] = dilate(no_track[L], r) if no_track[L].any() else None
        return keepout_cache[(L, r)]

    def build_grid(netcode, extra, lays, with_keepouts, soft=False):
        """Blocked cells per layer for a net: other nets' copper inflated by `extra`."""
        blocked = [np.zeros((G.ny, G.nx), dtype=bool) for _ in lays]
        for fp in FPS:
            for p in fp.Pads():
                if p.GetNetCode() == netcode and netcode != 0:
                    continue
                r = p.GetBoundingBox()
                for li, L in enumerate(lays):
                    if p.IsOnLayer(L):
                        G.rect(blocked[li], mm(r.GetLeft()) - extra, mm(r.GetTop()) - extra,
                               mm(r.GetRight()) + extra, mm(r.GetBottom()) + extra)
            if fp.GetReference().startswith("H"):
                c = fp.GetPosition()
                for li in range(len(lays)):
                    G.capsule(blocked[li], mm(c.x), mm(c.y), mm(c.x), mm(c.y), HOLE_KEEP + extra)
        for nc_, p, (x0, y0, x1, y1) in lanes:
            if nc_ == netcode and netcode != 0:
                continue
            for li, L in enumerate(lays):
                if p.IsOnLayer(L):
                    G.rect(blocked[li], x0 - extra, y0 - extra, x1 + extra, y1 + extra)
        tv = [np.zeros((G.ny, G.nx), dtype=bool) for _ in lays]     # other nets' tracks + vias only
        for t in TRK:
            if t.GetNetCode() == netcode or (soft and not t.IsLocked()):
                continue
            if t.GetClass() == "PCB_VIA":
                p = t.GetPosition(); rad = mm(t.GetWidth(pcbnew.F_Cu)) / 2 + extra
                for li in range(len(lays)):
                    G.capsule(tv[li], mm(p.x), mm(p.y), mm(p.x), mm(p.y), rad)
            elif t.GetLayer() in lays:
                s, e = t.GetStart(), t.GetEnd()
                G.capsule(tv[lays.index(t.GetLayer())], mm(s.x), mm(s.y), mm(e.x), mm(e.y),
                          mm(t.GetWidth()) / 2 + extra)
        for li in range(len(lays)):
            blocked[li] |= tv[li]
        build_grid.tv = tv          # opening an end pad must keep these blocked (see open_end)
        r = int(math.ceil(extra / g))
        for li, L in enumerate(lays):
            if with_keepouts:
                k = keepout(L, r)
                if k is not None:
                    blocked[li] |= k
                fp_ = foreign_phase(L, netcode, r)
                if fp_ is not None:
                    blocked[li] |= fp_
            m = blocked[li]
            G.rect(m, G.x0 - 1, G.y0 - 1, G.x0 + EDGE_KEEP + extra, G.y0 + H + 1)
            G.rect(m, G.x0 + W - g - EDGE_KEEP - extra, G.y0 - 1, G.x0 + W + 1, G.y0 + H + 1)
            G.rect(m, G.x0 - 1, G.y0 - 1, G.x0 + W + 1, G.y0 + EDGE_KEEP + extra)
            G.rect(m, G.x0 - 1, G.y0 + H - g - EDGE_KEEP - extra, G.x0 + W + 1, G.y0 + H + 1)
        return blocked

    def pad_of(ref, num):
        fp = b.FindFootprintByReference(ref)
        return next(p for p in fp.Pads() if p.GetNumber() == num)

    class End:
        """A route end that isn't a pad: an existing via or track end (or a new escape via)."""
        def __init__(self, x, y, lays):
            self.pos, self.lays = pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)), lays
        def GetPosition(self):
            return self.pos
        def GetClass(self):
            return "END"
        def IsOnLayer(self, L):
            return L in self.lays
        def GetBoundingBox(self):
            r = pcbnew.BOX2I(self.pos, pcbnew.VECTOR2I(0, 0)); r.Inflate(pcbnew.FromMM(0.15)); return r

    def usable(p, net):
        """A pad whose centre sits in a keep-out on every routing layer it has (e.g. a FET pad under the top-layer
        keep-out): use the nearest same-net via instead - it reaches every layer."""
        if p.GetClass() not in ("PAD",):
            return p
        q = p.GetPosition()
        ci, cj = G.cell(mm(q.x), mm(q.y))
        if not (0 <= ci < G.nx and 0 <= cj < G.ny):
            return p
        if any(p.IsOnLayer(L) and not no_track[L][cj, ci] for L in layers):
            return p
        best, bd = p, None
        for t in TRK:
            if t.GetClass() == "PCB_VIA" and t.GetNetCode() == net.GetNetCode():
                d = (t.GetPosition() - q).EuclideanNorm()
                if bd is None or d < bd:
                    best, bd = End(mm(t.GetPosition().x), mm(t.GetPosition().y), COPPER), d
        return best

    def endpoint(spec, net):
        if "," not in spec:
            return pad_of(*spec.split("."))
        x, y = (float(v) for v in spec.split(","))
        p = pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))
        near = pcbnew.FromMM(0.05)
        same = [t for t in TRK if t.GetNetCode() == net.GetNetCode()]
        for t in same:      # a via there wins: it reaches every layer
            if t.GetClass() == "PCB_VIA" and (t.GetPosition() - p).EuclideanNorm() <= near:
                return End(x, y, COPPER)
        for t in same:
            if t.GetClass() == "PCB_TRACK" and t.GetLayer() in layers and                     min((t.GetStart() - p).EuclideanNorm(), (t.GetEnd() - p).EuclideanNorm()) <= near:
                return End(x, y, (t.GetLayer(),))
        # nothing of this net exists there: no unchecked escape via, start from the nearest pad / via / track end
        best, bd = None, None
        for t in same:
            for q, lays in ((t.GetPosition(), COPPER),) if t.GetClass() == "PCB_VIA" else                     (((t.GetStart(), (t.GetLayer(),)), (t.GetEnd(), (t.GetLayer(),))) if t.GetLayer() in layers else ()):
                dd = (q - p).EuclideanNorm()
                if bd is None or dd < bd:
                    best, bd = End(mm(q.x), mm(q.y), lays), dd
        for fp in FPS:
            for pd in fp.Pads():
                if pd.GetNetCode() == net.GetNetCode() and any(pd.IsOnLayer(L) for L in layers):
                    dd = (pd.GetPosition() - p).EuclideanNorm()
                    if bd is None or dd < bd:
                        best, bd = pd, dd
        return best if best is not None else End(x, y, ())

    NX, NY = G.nx, G.ny

    def rip_blockers(net, width, clr, sa, goal_cells, pa, pc, gi, gj):
        """Find a path that ignores other nets' unlocked routing; remove the nets that path runs into."""
        nc = net.GetNetCode()
        tbs = build_grid(nc, clr + width / 2, layers, True, soft=True)
        tbs_tv = build_grid.tv
        vbs = build_grid(nc, clr + VIA_D / 2, COPPER, False, soft=True)
        holes_s = np.zeros((G.ny, G.nx), dtype=bool)
        for t in TRK:
            if t.GetClass() == "PCB_VIA" and (t.IsLocked() or t.GetNetCode() == nc):
                q = t.GetPosition()
                G.capsule(holes_s, mm(q.x), mm(q.y), mm(q.x), mm(q.y), HOLE_PITCH)
        for p in (pa, pc):
            r = p.GetBoundingBox()
            i0, j0, i1, j1 = G.window(mm(r.GetLeft()), mm(r.GetTop()), mm(r.GetRight()), mm(r.GetBottom()))
            for li, L in enumerate(layers):
                if p.GetClass() == "PCB_VIA" or p.IsOnLayer(L):
                    ko = keepout(L, int(math.ceil((clr + width / 2) / g)))
                    tbs[li][j0:j1, i0:i1] = tbs_tv[li][j0:j1, i0:i1] | (False if ko is None else ko[j0:j1, i0:i1])
        # crossing another net's (unlocked) copper is allowed but costs RIP_COST per cell, so the path that disturbs
        # the least routing wins instead of ploughing through whatever is in the straight line
        hard = [tbs[li] for li in range(len(layers))]
        full = build_grid(nc, clr + width / 2, layers, True)
        cost = [np.where(full[li] & ~hard[li], np.float32(RIP_COST), np.float32(0)) for li in range(len(layers))]
        path = astar_nb.search([~m for m in tbs], ~(np.logical_or.reduce(vbs) | no_via_grown | holes_s), sa,
                               goal_cells, NX, NY, gi, gj, VIA_COST, TURN_COST, RIP_H_WEIGHT, MAX_EXPAND_NB, cost)
        if __import__("os").environ.get("MAZE_DEBUG_RIP"):
            print("  rip-up soft path:", len(path), "cells", flush=True)
        if not path:
            return set()
        # where the soft path's copper would sit, grown by clearance + a victim's half width (<= 0.125 mm)
        occ = [np.zeros((G.ny, G.nx), dtype=bool) for _ in layers]
        via_occ = np.zeros((G.ny, G.nx), dtype=bool)
        for k, (li, i, j) in enumerate(path):
            occ[li][j, i] = True
            if k and path[k - 1][0] != li:
                via_occ[j, i] = True
        r_tr = int(math.ceil((width / 2 + clr + 0.125) / g))
        r_via = int(math.ceil((VIA_D / 2 + clr + 0.125) / g))
        grown_vias = dilate(via_occ, r_via)
        occ = [dilate(o, r_tr) | grown_vias for o in occ]
        any_occ = np.logical_or.reduce(occ)
        victims = set()
        rv = int(math.ceil(0.3 / g))
        for t in TRK:
            n = t.GetNetname()
            if t.IsLocked() or t.GetNetCode() == nc or n in PLANE_NETS or n in victims:
                continue
            if t.GetClass() == "PCB_VIA":
                q = t.GetPosition()
                ci, cj = G.cell(mm(q.x), mm(q.y))
                if any_occ[max(0, cj - rv):cj + rv + 1, max(0, ci - rv):ci + rv + 1].any():
                    victims.add(n)
            elif t.GetLayer() in layers:
                o = occ[layers.index(t.GetLayer())]
                s0, e0 = t.GetStart(), t.GetEnd()
                sx, sy, ex, ey = mm(s0.x), mm(s0.y), mm(e0.x), mm(e0.y)
                n_s = max(1, int(math.hypot(ex - sx, ey - sy) / (g / 2)))
                for k in range(n_s + 1):
                    ci, cj = G.cell(sx + (ex - sx) * k / n_s, sy + (ey - sy) * k / n_s)
                    if 0 <= ci < NX and 0 <= cj < NY and o[cj, ci]:
                        victims.add(n)
                        break
        if __import__("os").environ.get("MAZE_DEBUG_RIP"):
            print("  rip-up victims:", sorted(victims), flush=True)
        if victims:
            for t in list(TRK):
                if t.GetNetname() in victims and not t.IsLocked():
                    TRK.remove(t); RIPPED.append(t)      # deleted from the board at the end (see TRK)
        return victims

    results = []

    def emit(text):
        results.append(text)
        print(f"[{len(results)}/{len(jobs)}] {text}", flush=True)   # live progress, one line per connection

    for job in jobs:
        netname, a, c = job.split(":")
        net = b.FindNet(netname)
        cw, cc = class_rules(netname)
        width = ARGS["--width"] or cw
        clr = ARGS["--clearance"] or cc
        pa, pc = endpoint(a, net), endpoint(c, net)
        pa, pc = usable(pa, net), usable(pc, net)
        tb = build_grid(net.GetNetCode(), clr + width / 2, layers, True)
        tb_tv = build_grid.tv
        vb = build_grid(net.GetNetCode(), clr + VIA_D / 2, COPPER, False)
        holes = np.zeros((G.ny, G.nx), dtype=bool)        # drill-to-drill spacing applies to every net
        for t in TRK:
            if t.GetClass() == "PCB_VIA":
                q = t.GetPosition()
                G.capsule(holes, mm(q.x), mm(q.y), mm(q.x), mm(q.y), HOLE_PITCH)
        no_via_here = no_via_grown | holes
        via_free = (~(np.logical_or.reduce(vb) | no_via_here)).tobytes()

        def layers_of(p):
            if p.GetClass() == "PCB_VIA":
                return list(range(len(layers)))
            return [li for li, L in enumerate(layers) if p.IsOnLayer(L)]

        sa = [(li, *G.cell(mm(pa.GetPosition().x), mm(pa.GetPosition().y))) for li in layers_of(pa)]
        goal_cells = {(li, *G.cell(mm(pc.GetPosition().x), mm(pc.GetPosition().y))) for li in layers_of(pc)}
        if not sa or not goal_cells:
            emit(f"{job}: NO PATH (end not on a routing layer)"); continue
        gi, gj = next(iter(goal_cells))[1:]
        for p in (pa, pc):   # open the end pads so the search can leave/enter them (but never a keep-out)
            r = p.GetBoundingBox()
            i0, j0, i1, j1 = G.window(mm(r.GetLeft()), mm(r.GetTop()), mm(r.GetRight()), mm(r.GetBottom()))
            for li in layers_of(p):
                ko = keepout(layers[li], int(math.ceil((clr + width / 2) / g)))
                tb[li][j0:j1, i0:i1] = tb_tv[li][j0:j1, i0:i1] | (False if ko is None else ko[j0:j1, i0:i1])
        # an end whose centre is inside a keep-out on some layer can't be used on that layer
        sa = [n for n in sa if not no_track[layers[n[0]]][n[2], n[1]]]
        goal_cells = {n for n in goal_cells if not no_track[layers[n[0]]][n[2], n[1]]}
        if not sa or not goal_cells:
            emit(f"{job}: NO PATH (end only reachable inside a keep-out)"); continue
        free_l = [(~m).tobytes() for m in tb]
        if __import__("os").environ.get("MAZE_PROBE"):     # free? per layer at given points (mm)
            for q in __import__("os").environ["MAZE_PROBE"].split(";"):
                qx, qy = (float(v) for v in q.split(","))
                qi, qj = G.cell(qx, qy)
                print("probe", q, [bool(free_l[li][qj * NX + qi]) for li in range(len(layers))])
        if __import__("os").environ.get("MAZE_PNG"):       # full free/blocked map per layer as PNG
            from PIL import Image
            for li in range(len(layers)):
                im = Image.frombytes("L", (NX, NY), bytes(255 if c else 40 for c in free_l[li])).convert("RGB")
                for (lj, i, j) in list(sa) + list(goal_cells):
                    for di in range(-4, 5):
                        for dj in range(-4, 5):
                            if 0 <= i + di < NX and 0 <= j + dj < NY:
                                im.putpixel((i + di, j + dj), (255, 0, 0) if (lj, i, j) in sa else (0, 160, 255))
                im.save(f"{__import__('os').environ['MAZE_PNG']}_{li}.png")
        if __import__("os").environ.get("MAZE_DEBUG"):     # ascii map of the start area per layer
            si, sj = sa[0][1], sa[0][2]
            for li in range(len(layers)):
                print("layer", li, "start", sa[0])
                for j in range(sj - 12, sj + 13):
                    print("".join("S" if (i, j) == (si, sj) else ("." if 0 <= i < NX and 0 <= j < NY and free_l[li][j * NX + i] else "#")
                                  for i in range(si - 30, si + 31)))

        if astar_nb is not None:   # compiled search (numba): same rules, ~50x faster
            path_nodes = astar_nb.search([~m for m in tb], ~(np.logical_or.reduce(vb) | no_via_here), sa, goal_cells,
                                         NX, NY, gi, gj, VIA_COST, TURN_COST, H_WEIGHT, MAX_EXPAND_NB)
            ripped_note = ""
            if not path_nodes and ripup:
                # Rip-up and reroute: search again treating other nets' unlocked routing as passable. If that finds
                # a way, remove exactly the nets in the way and route this connection for real; the removed nets
                # are left for finish_route's next pass to put back elsewhere.
                victims = rip_blockers(net, width, clr, sa, goal_cells, pa, pc, gi, gj)
                if victims:
                    ripped_note = f" (ripped up {', '.join(sorted(victims))})"
                    tb = build_grid(net.GetNetCode(), clr + width / 2, layers, True)
                    tb_tv = build_grid.tv
                    vb = build_grid(net.GetNetCode(), clr + VIA_D / 2, COPPER, False)
                    holes = np.zeros((G.ny, G.nx), dtype=bool)
                    for t in TRK:
                        if t.GetClass() == "PCB_VIA":
                            q = t.GetPosition()
                            G.capsule(holes, mm(q.x), mm(q.y), mm(q.x), mm(q.y), HOLE_PITCH)
                    no_via_here = no_via_grown | holes
                    for p in (pa, pc):
                        r = p.GetBoundingBox()
                        i0, j0, i1, j1 = G.window(mm(r.GetLeft()), mm(r.GetTop()), mm(r.GetRight()),
                                                  mm(r.GetBottom()))
                        for li in layers_of(p):
                            ko = keepout(layers[li], int(math.ceil((clr + width / 2) / g)))
                            tb[li][j0:j1, i0:i1] = tb_tv[li][j0:j1, i0:i1] |                                 (False if ko is None else ko[j0:j1, i0:i1])
                    free_l = [(~m).tobytes() for m in tb]
                    path_nodes = astar_nb.search([~m for m in tb], ~(np.logical_or.reduce(vb) | no_via_here), sa,
                                                 goal_cells, NX, NY, gi, gj, VIA_COST, TURN_COST, H_WEIGHT,
                                                 MAX_EXPAND_NB)
            if not path_nodes:
                emit(f"{job}: NO PATH{ripped_note}"); continue
        else:
            moves = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                     (1, 1, 1.4142), (1, -1, 1.4142), (-1, 1, 1.4142), (-1, -1, 1.4142)]
            nl = len(layers)
            # state = (layer, i, j, direction of the move that got here; 8 = none yet / after a via)
            dist, prev, heap = {}, {}, []
            for s in sa:
                st = (*s, 8)
                dist[st] = 0.0; heapq.heappush(heap, (H_WEIGHT * math.hypot(s[1] - gi, s[2] - gj), 0.0, st))
            found, expanded = None, 0
            while heap:
                _, d, node = heapq.heappop(heap)
                expanded += 1
                if expanded > MAX_EXPAND:      # a sealed-off end: give up rather than flood every cell x 8 headings
                    break
                if node[:3] in goal_cells:
                    found = node; break
                if d > dist.get(node, 1e18):
                    continue
                li, i, j, dr = node
                fl = free_l[li]
                nbrs = []
                for k, (di, dj, w) in enumerate(moves):
                    ii, jj = i + di, j + dj
                    if 0 <= ii < NX and 0 <= jj < NY and fl[jj * NX + ii]:
                        nbrs.append(((li, ii, jj, k), w + (TURN_COST if dr not in (8, k) else 0.0)))
                if nl > 1 and via_free[j * NX + i]:
                    nbrs += [((lj, i, j, 8), VIA_COST) for lj in range(nl) if lj != li and free_l[lj][j * NX + i]]
                for nb, w in nbrs:
                    nd = d + w
                    if nd < dist.get(nb, 1e18):
                        dist[nb] = nd; prev[nb] = node
                        heapq.heappush(heap, (nd + H_WEIGHT * math.hypot(nb[1] - gi, nb[2] - gj), nd, nb))
            if not found:
                emit(f"{job}: NO PATH"); continue
            path_nodes = [found]
            while path_nodes[-1] in prev:
                path_nodes.append(prev[path_nodes[-1]])
            path_nodes.reverse()
            path_nodes = [n[:3] for n in path_nodes]
        pts = smooth(path_nodes, free_l, NX)
        segs = vias = 0

        def xy(n):
            return pcbnew.VECTOR2I(pcbnew.FromMM(G.x0 + n[1] * g), pcbnew.FromMM(G.y0 + n[2] * g))
        for k in range(len(pts) - 1):
            p0, p1 = pts[k], pts[k + 1]
            if p0[0] != p1[0]:
                v = pcbnew.PCB_VIA(b); v.SetPosition(xy(p0)); v.SetWidth(pcbnew.FromMM(VIA_D))
                v.SetDrill(pcbnew.FromMM(VIA_DRILL)); v.SetNet(net); v.SetViaType(pcbnew.VIATYPE_THROUGH)
                b.Add(v); TRK.append(v); vias += 1
                continue
            t = pcbnew.PCB_TRACK(b)
            t.SetStart(pa.GetPosition() if k == 0 else xy(p0))
            t.SetEnd(pc.GetPosition() if k == len(pts) - 2 else xy(p1))
            t.SetWidth(pcbnew.FromMM(width)); t.SetLayer(layers[p0[0]]); t.SetNet(net); b.Add(t); TRK.append(t); segs += 1
        emit(f"{job}: {segs} segments, {vias} vias" + (ripped_note if astar_nb is not None else ""))
    zones = list(b.Zones())
    for t in RIPPED:
        b.Remove(t)
    pcbnew.ZONE_FILLER(b).Fill(zones)
    b.Save(path)
    ok = sum(1 for r in results if "NO PATH" not in r)
    print(f"routed {ok}/{len(results)} connections", flush=True)


if __name__ == "__main__":
    main()

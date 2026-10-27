"""Remove tracks so they can be rerouted, then prune any dangling stubs that leaves behind.

Run with KiCad's Python:
    vendor/KiCAD-MCP-Server/venv/Scripts/python.exe tools/prune_tracks.py <board> <rules.json>

rules.json:
  {"nets": ["NET", ...],                              # remove every track of these nets
   "near": [{"net": "/Phase_C", "x": 103.4, "y": 96.3, "r": 2.6, "layer": "B.Cu"}]}  # remove segments passing near a point
Dangling segments (an end touching no pad / via / same-net track / same-net zone) are removed repeatedly.
"""

from __future__ import annotations

import sys as _sys, os as _os; _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _quiet  # noqa: E402,F401  - no console windows for child processes on Windows

import json
import sys

import pcbnew
import wx

wx.DisableAsserts()


def seg_dist(t, x, y) -> float:
    ax, ay = pcbnew.ToMM(t.GetStart().x), pcbnew.ToMM(t.GetStart().y)
    bx, by = pcbnew.ToMM(t.GetEnd().x), pcbnew.ToMM(t.GetEnd().y)
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    u = 0 if L == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L))
    return ((ax + u * dx - x) ** 2 + (ay + u * dy - y) ** 2) ** 0.5


def main() -> None:
    path, rules = sys.argv[1], json.load(open(sys.argv[2]))
    b = pcbnew.LoadBoard(path)
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    tracks = list(b.GetTracks())          # one snapshot; KiCad 9's wrapper dislikes re-listing after removals
    pads = [p for fp in b.GetFootprints() for p in fp.Pads()]
    zones = list(b.Zones())
    alive = set(range(len(tracks)))
    removed = []

    def drop(i, why):
        alive.discard(i)
        removed.append((tracks[i].GetNetname(), why))

    for i, t in enumerate(tracks):
        if t.GetNetname() in rules.get("nets", []):
            drop(i, "net")
        for n in rules.get("near", []):
            if t.GetClass() == "PCB_TRACK" and t.GetNetname() == n["net"] and b.GetLayerName(t.GetLayer()) == n["layer"] \
                    and seg_dist(t, n["x"], n["y"]) < n["r"]:
                drop(i, "near")

    def connected(i, pt) -> bool:
        t = tracks[i]
        layer, net = t.GetLayer(), t.GetNetCode()
        for p in pads:
            if p.GetNetCode() == net and p.IsOnLayer(layer) and p.HitTest(pt):
                return True
        for j in alive:
            if j == i:
                continue
            o = tracks[j]
            if o.GetNetCode() != net:
                continue
            if o.GetClass() == "PCB_VIA":
                if (o.GetPosition() - pt).EuclideanNorm() <= o.GetWidth(pcbnew.F_Cu) // 2:
                    return True
            elif o.GetLayer() == layer and (o.GetStart() == pt or o.GetEnd() == pt or o.HitTest(pt, 0)):
                return True
        for z in zones:
            if z.GetNetCode() == net and z.IsOnLayer(layer) and z.HitTestFilledArea(layer, pt):
                return True
        return False

    copper = (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu)

    def via_layers(i) -> int:
        """How many copper layers something of the via's own net actually touches it on."""
        v = tracks[i]
        pos, net, r = v.GetPosition(), v.GetNetCode(), v.GetWidth(pcbnew.F_Cu) // 2
        hit = set()
        for j in alive:
            o = tracks[j]
            if j != i and o.GetNetCode() == net and o.GetClass() == "PCB_TRACK" and \
                    ((o.GetStart() - pos).EuclideanNorm() <= r or (o.GetEnd() - pos).EuclideanNorm() <= r):
                hit.add(o.GetLayer())
        for p in pads:
            if p.GetNetCode() == net and p.HitTest(pos):
                hit.update(L for L in copper if p.IsOnLayer(L))
        for z in zones:
            if z.GetNetCode() == net:
                hit.update(L for L in copper if z.IsOnLayer(L) and z.HitTestFilledArea(L, pos))
        return len(hit)

    changed = True
    while changed:
        changed = False
        for i in list(alive):
            t = tracks[i]
            if t.GetClass() == "PCB_VIA":
                # a signal via that joins nothing to nothing (e.g. a fan-out the router no longer needed)
                if t.GetNetname() not in ("GND", "/VM", "/BATN") and via_layers(i) <= 1:
                    drop(i, "one-layer via")
                    changed = True
                continue
            if not (connected(i, t.GetStart()) and connected(i, t.GetEnd())):
                drop(i, "dangling")
                changed = True

    for i, t in enumerate(tracks):
        if i not in alive:
            b.Remove(t)
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    b.Save(path)
    from collections import Counter
    print("removed:", dict(Counter(f"{n} ({w})" for n, w in removed)))


if __name__ == "__main__":
    main()

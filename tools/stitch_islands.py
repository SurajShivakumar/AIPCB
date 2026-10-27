"""Tie isolated copper islands of a zone net to the rest of the net with stitching vias,
and delete dangling vias. Run with KiCad's Python:
    vendor/KiCAD-MCP-Server/venv/Scripts/python.exe tools/stitch_islands.py <board> [NET] [--min-area 2]
"""

from __future__ import annotations

import sys as _sys, os as _os; _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _quiet  # noqa: E402,F401  - no console windows for child processes on Windows

import math
import sys

import pcbnew
import wx

wx.DisableAsserts()
VIA_D, VIA_DRILL, CLR = 0.6, 0.3, 0.25


def main() -> None:
    path = sys.argv[1]
    netname = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else "GND"
    min_area = float(sys.argv[sys.argv.index("--min-area") + 1]) if "--min-area" in sys.argv else 2.0
    b = pcbnew.LoadBoard(path)
    mm, FM = pcbnew.ToMM, pcbnew.FromMM
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    net = b.FindNet(netname)
    tracks = list(b.GetTracks())
    pads = [p for fp in b.GetFootprints() for p in fp.Pads()]
    no_via_areas = [z.Outline() for z in b.Zones() if z.GetIsRuleArea() and z.GetDoNotAllowVias()]
    for fp in b.GetFootprints():
        no_via_areas += [z.Outline() for z in fp.Zones() if z.GetIsRuleArea() and z.GetDoNotAllowVias()]

    # 1. dangling vias: a via touching no track end and no pad, on any net
    removed = 0
    for v in [t for t in tracks if t.GetClass() == "PCB_VIA"]:
        pos = v.GetPosition()
        r = v.GetWidth(pcbnew.F_Cu) // 2
        touches = any(t is not v and t.GetClass() == "PCB_TRACK" and t.GetNetCode() == v.GetNetCode()
                      and ((t.GetStart() - pos).EuclideanNorm() <= r or (t.GetEnd() - pos).EuclideanNorm() <= r)
                      for t in tracks)
        in_pad = any(p.GetNetCode() == v.GetNetCode() and p.HitTest(pos) for p in pads)
        in_zone = sum(1 for z in b.Zones() if z.GetNetCode() == v.GetNetCode()
                      and any(z.IsOnLayer(L) and z.HitTestFilledArea(L, pos) for L in (pcbnew.F_Cu, pcbnew.B_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu)))
        if not touches and not in_pad and in_zone < 2:
            b.Remove(v); removed += 1
    tracks = list(b.GetTracks())

    def clear_at(x, y) -> bool:
        p = pcbnew.VECTOR2I(FM(x), FM(y))
        need = FM(VIA_D / 2 + CLR)
        for q in pads:
            if q.GetNetCode() == net.GetNetCode() and q.GetAttribute() != pcbnew.PAD_ATTRIB_SMD:
                continue
            bb = q.GetBoundingBox(); bb.Inflate(need)
            if bb.Contains(p):
                return False
        for t in tracks:
            if t.GetClass() == "PCB_VIA":
                if (t.GetPosition() - p).EuclideanNorm() < need + t.GetWidth(pcbnew.F_Cu) // 2:
                    return False
            elif t.GetNetCode() != net.GetNetCode() and t.HitTest(p, need):
                return False
        for fp in b.GetFootprints():   # mounting holes and other NPTH
            if fp.GetReference().startswith("H"):
                c = fp.GetPosition()
                if math.hypot(mm(c.x) - x, mm(c.y) - y) < 3.6:
                    return False
        r = VIA_D / 2
        for ol in no_via_areas:        # rule areas that forbid vias (e.g. under the TVS): keep the whole disc out
            for dx, dy in ((0, 0), (r, 0), (-r, 0), (0, r), (0, -r)):
                if ol.Contains(pcbnew.VECTOR2I(FM(x + dx), FM(y + dy))):
                    return False
        return True

    added = 0
    jobs = []
    for z in b.Zones():
        if z.GetNetCode() != net.GetNetCode():
            continue
        for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
            if not z.IsOnLayer(layer):
                continue
            polys = z.GetFilledPolysList(layer)
            for i in range(polys.OutlineCount()):
                ol = polys.Outline(i)
                bb = ol.BBox()
                if mm(bb.GetWidth()) * mm(bb.GetHeight()) < min_area:
                    continue
                single = pcbnew.SHAPE_POLY_SET(); single.AddOutline(ol)
                has_via = any(t.GetClass() == "PCB_VIA" and t.GetNetCode() == net.GetNetCode()
                              and single.Contains(t.GetPosition()) for t in tracks)
                has_pth = any(p.GetNetCode() == net.GetNetCode() and p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH
                              and single.Contains(p.GetPosition()) for p in pads)
                if has_via or has_pth:
                    continue
                # search a grid inside the island for a spot with room for a via
                placed = 0
                x0, y0, x1, y1 = mm(bb.GetLeft()), mm(bb.GetTop()), mm(bb.GetRight()), mm(bb.GetBottom())
                step = 0.1
                y = y0 + 0.5
                while y < y1 - 0.5 and placed < 2:
                    x = x0 + 0.5
                    while x < x1 - 0.5 and placed < 2:
                        pt = pcbnew.VECTOR2I(FM(x), FM(y))
                        # the via's centre must be on the island (it then joins it); clear_at keeps it off other nets
                        if single.Contains(pt) and clear_at(x, y):
                            v = pcbnew.PCB_VIA(b); v.SetPosition(pt); v.SetWidth(FM(VIA_D)); v.SetDrill(FM(VIA_DRILL))
                            v.SetNet(net); v.SetViaType(pcbnew.VIATYPE_THROUGH); b.Add(v); tracks.append(v)
                            placed += 1; added += 1
                            x += 2.0
                        x += step
                    y += step
                if not placed:
                    # no room for a via: route a track from a pad on the island to the nearest via of the net
                    pad = next((p for p in pads if p.GetNetCode() == net.GetNetCode() and p.IsOnLayer(layer)
                                and single.Contains(p.GetPosition())), None)
                    others = [t for t in tracks if t.GetClass() == "PCB_VIA" and t.GetNetCode() == net.GetNetCode()
                              and not single.Contains(t.GetPosition())]
                    if pad is not None and others:
                        v = min(others, key=lambda t: (t.GetPosition() - pad.GetPosition()).EuclideanNorm())
                        fp = pad.GetParentFootprint()
                        jobs.append(f"{netname}:{fp.GetReference()}.{pad.GetNumber()}:"
                                    f"{mm(v.GetPosition().x):.4f},{mm(v.GetPosition().y):.4f}")
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    b.Save(path)
    print(f"removed {removed} dangling vias; added {added} stitching vias on {netname}")
    if jobs:
        # islands too cramped for a via: the maze router (with rip-up) ties their pads to the plane
        import subprocess
        from pathlib import Path as _P
        maze = _P(__file__).resolve().parent / "maze_route.py"
        out = subprocess.run([sys.executable, "-u", str(maze), path, *dict.fromkeys(jobs), "--layers",
                              "F.Cu,In2.Cu,B.Cu", "--grid", "0.05", *([] if "--no-ripup" in sys.argv else ["--ripup"])],
                             capture_output=True, text=True)
        for line in out.stdout.splitlines():
            if line.startswith("[") or line.startswith("routed "):
                print("  tie island:", line)


if __name__ == "__main__":
    main()

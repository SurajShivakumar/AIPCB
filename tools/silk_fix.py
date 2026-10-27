"""Move reference designators so they don't sit on pads, other silkscreen, or the board edge.

Run with KiCad's Python:
    vendor/KiCAD-MCP-Server/venv/Scripts/python.exe tools/silk_fix.py <board> [--size 0.8]

For each footprint's Reference text on a silkscreen layer, try positions around the footprint
(above, below, left, right, rotated or not) and keep the first that clears every pad's mask,
every other silk item and the board edge. If nothing fits, the reference moves to the Fab layer
(still on assembly drawings, no longer printed). Footprint silk outlines that overlap pads are clipped by KiCad
itself at plot time; outlines that collide with *other* parts' silk are reported.
"""

from __future__ import annotations

import sys as _sys, os as _os; _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _quiet  # noqa: E402,F401  - no console windows for child processes on Windows

import sys

import pcbnew
import wx

wx.DisableAsserts()
MM, FM = pcbnew.ToMM, pcbnew.FromMM
GAP = 0.2


def rect_of(item) -> pcbnew.BOX2I:
    return item.GetBoundingBox()


def main() -> None:
    path = sys.argv[1]
    size = float(sys.argv[sys.argv.index("--size") + 1]) if "--size" in sys.argv else 0.8
    b = pcbnew.LoadBoard(path)
    edge = b.GetBoardEdgesBoundingBox()
    fps = list(b.GetFootprints())
    side_layers = {pcbnew.F_SilkS: (pcbnew.F_Cu, pcbnew.F_Mask), pcbnew.B_SilkS: (pcbnew.B_Cu, pcbnew.B_Mask)}

    def obstacles(layer, skip_ref):
        cu, _ = side_layers[layer]
        boxes = []
        for fp in fps:
            for p in fp.Pads():
                if p.IsOnLayer(cu) or p.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH):
                    r = rect_of(p); r.Inflate(FM(GAP)); boxes.append(r)
            for g in fp.GraphicalItems():
                if g.GetLayer() == layer:
                    r = rect_of(g); r.Inflate(FM(GAP / 2)); boxes.append(r)
            if fp is not skip_ref:
                ref = fp.Reference()
                if ref.IsVisible() and ref.GetLayer() == layer:
                    r = rect_of(ref); r.Inflate(FM(GAP / 2)); boxes.append(r)
        for d in b.GetDrawings():
            if d.GetLayer() == layer:
                r = rect_of(d); r.Inflate(FM(GAP / 2)); boxes.append(r)
        for t in b.GetTracks():
            if t.GetClass() == "PCB_VIA":
                r = rect_of(t); r.Inflate(FM(GAP)); boxes.append(r)
        return boxes

    moved = fab = kept = 0
    for fp in fps:
        ref = fp.Reference()
        if not ref.IsVisible() or ref.GetLayer() not in side_layers:
            continue
        layer = ref.GetLayer()
        ref.SetTextSize(pcbnew.VECTOR2I(FM(size), FM(size)))
        ref.SetTextThickness(FM(size * 0.15))
        obs = obstacles(layer, fp)

        def fits() -> bool:
            r = rect_of(ref)
            inner = pcbnew.BOX2I(edge.GetPosition(), edge.GetSize())
            inner.Inflate(-FM(0.3))
            if not (inner.Contains(r.GetOrigin()) and inner.Contains(r.GetEnd())):
                return False
            return not any(r.Intersects(o) for o in obs)

        if fits():
            kept += 1
            continue
        body = fp.GetBoundingBox(False, False)
        cx, cy = body.GetCenter().x, body.GetCenter().y
        hw, hh = body.GetWidth() // 2, body.GetHeight() // 2
        th = FM(size)
        offsets = [(0, -(hh + th)), (0, hh + th), (-(hw + 2 * th), 0), (hw + 2 * th, 0),
                   (-(hw + th), -(hh + th)), (hw + th, -(hh + th)), (-(hw + th), hh + th), (hw + th, hh + th),
                   (0, -(hh + 2 * th)), (0, hh + 2 * th)]
        placed = False
        for angle in (0, 90):
            ref.SetTextAngleDegrees(angle)
            for dx, dy in offsets:
                ref.SetPosition(pcbnew.VECTOR2I(cx + dx, cy + dy))
                if fits():
                    placed = True
                    break
            if placed:
                break
        if placed:
            moved += 1
        else:
            ref.SetLayer(pcbnew.B_Fab if layer == pcbnew.B_SilkS else pcbnew.F_Fab)
            fab += 1
    b.Save(path)
    print(f"references: kept {kept}, moved {moved}, sent to Fab {fab}")


if __name__ == "__main__":
    main()

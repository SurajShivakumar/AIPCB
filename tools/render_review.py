"""Render a board for review: each copper layer on its own (fills, tracks, vias, pads, keep-outs) plus the 3D
top and bottom views. Run with KiCad's Python:
    vendor/KiCAD-MCP-Server/venv/Scripts/python.exe tools/render_review.py <board.kicad_pcb> [--out DIR]
        [--no-3d | --3d-only]

DIR defaults to <project>/review. Files: layer_F_Cu.png, layer_In1_Cu.png, layer_In2_Cu.png, layer_B_Cu.png,
3d_top.png, 3d_bottom.png.
"""

from __future__ import annotations

import sys as _sys, os as _os; _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _quiet  # noqa: E402,F401  - no console windows for child processes on Windows

import subprocess
import sys
from pathlib import Path

import pcbnew
import wx
from PIL import Image, ImageDraw

wx.DisableAsserts()
from _env import KPY, KICAD_CLI, KICAD_SHARE  # noqa: E402  - KiCad paths (Windows default / Docker override)
CLI = KICAD_CLI
SCALE = 16            # px per mm
PALETTE = [(230, 80, 80), (80, 160, 230), (90, 200, 90), (220, 180, 50), (190, 90, 210), (60, 200, 200),
           (240, 130, 40), (150, 150, 150)]


def main() -> None:
    args = sys.argv[1:]
    board_path = Path(args[0]).resolve()
    out = Path(args[args.index("--out") + 1]) if "--out" in args else board_path.parent / "review"
    out.mkdir(parents=True, exist_ok=True)
    only_3d = "--3d-only" in args
    # the Docker image has no 3D model library: the 3D views are rendered on the host afterwards
    want_3d = "--no-3d" not in args and not __import__("os").environ.get("AUTOPCB_DOCKER")
    if only_3d:
        render_3d(board_path, out)
        return
    b = pcbnew.LoadBoard(str(board_path))
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    bb = b.GetBoardEdgesBoundingBox()
    x0, y0 = pcbnew.ToMM(bb.GetX()), pcbnew.ToMM(bb.GetY())
    w, h = pcbnew.ToMM(bb.GetWidth()), pcbnew.ToMM(bb.GetHeight())
    colours = {}

    def col(net):
        if net == "GND":
            return (70, 70, 110)
        if net not in colours:
            colours[net] = PALETTE[len(colours) % len(PALETTE)]
        return colours[net]

    def P(v):
        return ((pcbnew.ToMM(v.x) - x0 + 1) * SCALE, (pcbnew.ToMM(v.y) - y0 + 1) * SCALE)

    for lid in (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu):
        img = Image.new("RGB", (int((w + 2) * SCALE), int((h + 2) * SCALE)), (15, 15, 15))
        d = ImageDraw.Draw(img, "RGBA")
        for z in b.Zones():
            if z.GetIsRuleArea():
                if z.IsOnLayer(lid):
                    ol = z.Outline()
                    for i in range(ol.OutlineCount()):
                        ch = ol.Outline(i)
                        d.polygon([P(ch.CPoint(k)) for k in range(ch.PointCount())], outline=(255, 255, 0),
                                  fill=(255, 255, 0, 20))
                continue
            if not z.IsOnLayer(lid):
                continue
            fp = z.GetFilledPolysList(lid)
            c = col(z.GetNetname())
            for i in range(fp.OutlineCount()):
                ch = fp.Outline(i)
                d.polygon([P(ch.CPoint(k)) for k in range(ch.PointCount())], fill=c + (110,))
                for hh in range(fp.HoleCount(i)):
                    hp = fp.Hole(i, hh)
                    d.polygon([P(hp.CPoint(k)) for k in range(hp.PointCount())], fill=(15, 15, 15, 255))
        for t in b.GetTracks():
            c = col(t.GetNetname())
            if t.GetClass() == "PCB_VIA":
                p = P(t.GetPosition()); r = pcbnew.ToMM(t.GetWidth(pcbnew.F_Cu)) / 2 * SCALE
                d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=(220, 220, 220), outline=c)
            elif t.GetLayer() == lid:
                d.line([P(t.GetStart()), P(t.GetEnd())], fill=c + (255,),
                       width=max(1, int(pcbnew.ToMM(t.GetWidth()) * SCALE)))
        for f in b.GetFootprints():
            for pad in f.Pads():
                if pad.IsOnLayer(lid):
                    r = pad.GetBoundingBox()
                    d.rectangle([P(r.GetOrigin()), P(r.GetEnd())], fill=col(pad.GetNetname()) + (230,),
                                outline=(255, 255, 255, 120))
            side_ok = (f.GetLayer() == pcbnew.F_Cu and lid == pcbnew.F_Cu) or (f.GetLayer() == pcbnew.B_Cu and lid == pcbnew.B_Cu)
            if side_ok:
                d.text(P(f.GetPosition()), f.GetReference(), fill=(255, 255, 255))
        d.text((6, 4), b.GetLayerName(lid), fill=(255, 255, 255))
        img.save(out / f"layer_{b.GetLayerName(lid).replace('.', '_')}.png")
    if want_3d:
        render_3d(board_path, out)
    print("review images:", ", ".join(sorted(p.name for p in out.glob("*.png"))))


def render_3d(board_path: Path, out: Path) -> None:
    for side in ("top", "bottom"):
        subprocess.run([CLI, "pcb", "render", "--side", side, "--width", "1200", "--height", "1450", "--quality",
                        "basic", "-o", str(out / f"3d_{side}.png"), str(board_path)], capture_output=True)
    print("3D views:", ", ".join(sorted(p.name for p in out.glob("3d_*.png"))))


if __name__ == "__main__":
    main()

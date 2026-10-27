"""Update a board from its schematic (KiCad's F8 'Update PCB from Schematic', scripted).

Run with KiCad's Python:
    vendor/KiCAD-MCP-Server/venv/Scripts/python.exe tools/sync_board.py <project_dir> [placements.json]

  * adds footprints for new symbols (at positions from placements.json: {"REF": [x, y, rot, "F"|"B"]},
    relative to the board's top-left corner)
  * reloads footprints whose library copy changed (keeps position/side/rotation/nets)
  * updates values, footprint fields and pad nets; renames nets on existing tracks, vias and zones
  * locks all pre-existing tracks/vias so a later autoroute only adds the new connections
"""

from __future__ import annotations

import sys as _sys, os as _os; _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _quiet  # noqa: E402,F401  - no console windows for child processes on Windows

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

import pcbnew
import wx

wx.DisableAsserts()

from _env import KPY, KICAD_CLI, KICAD_SHARE  # noqa: E402  - KiCad paths (Windows default / Docker override)
KICAD_FP = KICAD_SHARE / "footprints"
CLI = KICAD_CLI
MM = pcbnew.FromMM


def lib_path(project: Path, lib: str) -> str:
    local = project / "libs" / f"{lib}.pretty"
    return str(local if local.exists() else KICAD_FP / f"{lib}.pretty")


def load_fp(project: Path, fpid: str):
    lib, name = fpid.split(":", 1)
    fp = pcbnew.FootprintLoad(lib_path(project, lib), name)
    if fp is None:
        raise SystemExit(f"cannot load footprint {fpid}")
    fp.SetFPID(pcbnew.LIB_ID(lib, name))
    return fp


def main() -> None:
    project = Path(sys.argv[1]).resolve()
    placements = json.loads(Path(sys.argv[2]).read_text()) if len(sys.argv) > 2 else {}
    reload_refs = set(json.loads(Path(sys.argv[3]).read_text())) if len(sys.argv) > 3 else set()
    pro = next(project.glob("*.kicad_pro"))
    name = pro.stem
    xml_out = project / ".copilot" / "sync_netlist.xml"
    xml_out.parent.mkdir(exist_ok=True)
    subprocess.run([CLI, "sch", "export", "netlist", "--format", "kicadxml", "-o", str(xml_out),
                    str(project / f"{name}.kicad_sch")], check=True, capture_output=True)
    root = ET.parse(xml_out).getroot()
    board_path = project / f"{name}.kicad_pcb"
    board = pcbnew.LoadBoard(str(board_path))
    bb = board.GetBoardEdgesBoundingBox()
    ox, oy = pcbnew.ToMM(bb.GetX()), pcbnew.ToMM(bb.GetY())

    pin_net = {}
    for n in root.iterfind("nets/net"):
        for node in n.iterfind("node"):
            pin_net[(node.get("ref"), node.get("pin"))] = n.get("name")

    # lock everything that's already routed
    for t in board.GetTracks():
        t.SetLocked(True)

    # old net -> new net, decided by the pads (KiCad does the same on F8)
    votes: dict[str, Counter] = defaultdict(Counter)
    existing = {fp.GetReference(): fp for fp in board.GetFootprints()}
    for ref, fp in existing.items():
        for pad in fp.Pads():
            new = pin_net.get((ref, pad.GetNumber()))
            if new and pad.GetNetname():
                votes[pad.GetNetname()][new] += 1
    rename = {old: c.most_common(1)[0][0] for old, c in votes.items() if c.most_common(1)[0][0] != old}

    netinfo = {}
    def net(name_: str):
        if name_ not in netinfo:
            ni = board.FindNet(name_)
            if ni is None:
                ni = pcbnew.NETINFO_ITEM(board, name_)
                board.Add(ni)
            netinfo[name_] = ni
        return netinfo[name_]

    report = {"added": [], "reloaded": [], "values": [], "renamed_nets": rename}
    for comp in root.iterfind("components/comp"):
        ref = comp.get("ref")
        fpid = comp.findtext("footprint", "")
        value = comp.findtext("value", "")
        path = pcbnew.KIID_PATH(comp.find("sheetpath").get("tstamps") + comp.findtext("tstamps"))
        fp = existing.get(ref)
        if fp is None:
            if ref not in placements:
                raise SystemExit(f"new part {ref} needs a placement")
            fp = load_fp(project, fpid)
            x, y, rot, side = placements[ref]
            fp.SetPosition(pcbnew.VECTOR2I(MM(ox + x), MM(oy + y)))
            board.Add(fp)
            if side == "B":
                fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
            fp.SetOrientationDegrees(rot)
            report["added"].append(ref)
        elif fp.GetFPIDAsString() != fpid or ref in reload_refs:
            new = load_fp(project, fpid)
            if ref in placements:   # footprint changed size/shape: put it where we were told
                x, y, rot, side = placements[ref]
                new.SetPosition(pcbnew.VECTOR2I(MM(ox + x), MM(oy + y)))
                board.Add(new)
                if side == "B":
                    new.Flip(new.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
                new.SetOrientationDegrees(rot)
                report.setdefault("old_pad_positions", {})[ref] = [
                    [pcbnew.ToMM(p.GetPosition().x), pcbnew.ToMM(p.GetPosition().y), p.GetNetname()] for p in fp.Pads()]
            else:
                new.SetPosition(fp.GetPosition())
                board.Add(new)
                if fp.IsFlipped():
                    new.Flip(new.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
                new.SetOrientation(fp.GetOrientation())
                new.Reference().SetPosition(fp.Reference().GetPosition())
                new.Reference().SetLayer(fp.Reference().GetLayer())
            board.Remove(fp)
            fp = new
            report["reloaded"].append(ref)
        fp.SetReference(ref)
        if fp.GetValue() != value:
            report["values"].append(f"{ref}: {fp.GetValue()} -> {value}")
            fp.SetValue(value)
        fp.SetPath(path)
        for f in comp.iterfind("fields/field"):
            if f.get("name") in ("LCSC", "MPN", "Manufacturer"):
                fp.SetField(f.get("name"), f.text or "")
        for fld in fp.GetFields():   # sourcing data never goes on the silkscreen
            if fld.GetName() in ("LCSC", "MPN", "Manufacturer"):
                fld.SetVisible(False)
        for pad in fp.Pads():
            nn = pin_net.get((ref, pad.GetNumber()))
            if nn:
                pad.SetNet(net(nn))

    for item in list(board.GetTracks()) + list(board.Zones()):
        if item.GetNetname() in rename:
            item.SetNet(net(rename[item.GetNetname()]))

    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.Save(str(board_path))
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()

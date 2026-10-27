"""Power-electronics design checks for the ESC board: the things DRC/ERC can't see.

Run with KiCad's Python:
    vendor/KiCAD-MCP-Server/venv/Scripts/python.exe tools/esc_checks.py <board.kicad_pcb> [--out DIR]

Writes DIR/design_checks.md and DIR/design_checks.json (DIR defaults to <project>/review) and prints a summary.
Every check is measured on the board. FAIL = fix before ordering, WARN = look at it, INFO = context.

  switching loop     each half-bridge has a ceramic VM-GND cap close to both its HS drain and LS source
  charge pump        DRV8320 VCP / flying caps sit next to their pins
  gate drive         gate path length and vias, driver pin -> series R -> FET gate
  phase crossings    no signal runs inside another phase's pour (any layer)
  via in pad         vias inside SMD pads (need plugging/tenting at the fab)
  ground return      LS-source -> BAT- return path vs the crystal / MCU
  mounting holes     plain NPTH, not tied to a net
  3D models          every part with a body has a model file that resolves
"""

from __future__ import annotations

import sys as _sys, os as _os; _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _quiet  # noqa: E402,F401  - no console windows for child processes on Windows

import json
import math
import os
import re
import sys
from pathlib import Path

from _env import KPY, KICAD_CLI, KICAD_SHARE  # noqa: E402  - KiCad paths (Windows default / Docker override)
import pcbnew
import wx

wx.DisableAsserts()

PLANES = ("GND", "/VM", "/BATN")
LOOP_MAX = 4.0          # mm: ceramic VM pad -> HS drain pads + ceramic GND pad -> LS source pads, edge to edge
CP_MAX = 3.0            # mm: charge-pump cap to its driver pin
GATE_WARN = 30.0        # mm of gate track (driver pin -> FET gate)
RETURN_KEEP = 4.0       # mm: crystal / MCU centre to the straight LS-source -> BAT- return line


def mm(v):
    return pcbnew.ToMM(v)


def main() -> None:
    args = sys.argv[1:]
    board_path = Path(args[0]).resolve()
    out = Path(args[args.index("--out") + 1]) if "--out" in args else board_path.parent / "review"
    out.mkdir(parents=True, exist_ok=True)
    b = pcbnew.LoadBoard(str(board_path))
    fps = {f.GetReference(): f for f in b.GetFootprints()}
    tracks = list(b.GetTracks())
    results = []

    def add(check, status, msg, refs=()):
        results.append({"check": check, "status": status, "message": msg, "refs": list(refs)})

    def pads(ref, net=None):
        f = fps.get(ref)
        return [p for p in f.Pads() if net is None or p.GetNetname() == net] if f else []

    def centre(ps):
        return (sum(mm(p.GetPosition().x) for p in ps) / len(ps), sum(mm(p.GetPosition().y) for p in ps) / len(ps))

    def dist(a, b_):
        return math.hypot(a[0] - b_[0], a[1] - b_[1])

    def pad_gap(p, q):
        """Edge-to-edge distance between two pads' bounding boxes (0 if they overlap)."""
        a_, b_ = p.GetBoundingBox(), q.GetBoundingBox()
        dx = max(0, max(mm(a_.GetLeft()), mm(b_.GetLeft())) - min(mm(a_.GetRight()), mm(b_.GetRight())))
        dy = max(0, max(mm(a_.GetTop()), mm(b_.GetTop())) - min(mm(a_.GetBottom()), mm(b_.GetBottom())))
        return math.hypot(dx, dy)

    # ---------------------------------------------------------------- half-bridges
    fets = [r for r, f in fps.items() if f.GetFPIDAsString().endswith("DNK8_TEX")]
    bridges = {}
    for r in fets:
        nets = {p.GetNetname() for p in pads(r)}
        phase = next((n for n in nets if n.startswith("/Phase_")), None)
        if phase:
            side = "hs" if "/VM" in nets else "ls" if "GND" in nets else None
            bridges.setdefault(phase, {})[side] = r
    ceramics = [r for r, f in fps.items() if r.startswith("C") and "CP_Elec" not in f.GetFPIDAsString()
                and {p.GetNetname() for p in f.Pads()} == {"/VM", "GND"}]
    for phase, br in sorted(bridges.items()):
        hs, ls = br.get("hs"), br.get("ls")
        if not (hs and ls):
            add("switching loop", "FAIL", f"{phase}: could not identify both FETs", [hs or "", ls or ""])
            continue
        drains, sources = pads(hs, "/VM"), pads(ls, "GND")
        best = None
        for c in ceramics:
            vm_pad = next(p for p in pads(c) if p.GetNetname() == "/VM")
            gnd_pad = next(p for p in pads(c) if p.GetNetname() == "GND")
            g1 = min(pad_gap(vm_pad, d) for d in drains)
            g2 = min(pad_gap(gnd_pad, q) for q in sources)
            if best is None or g1 + g2 < best[0]:
                best = (g1 + g2, c, g1, g2)
        if best is None:
            add("switching loop", "FAIL", f"{phase}: no ceramic VM-GND capacitor on the board", [hs, ls])
        elif best[0] > LOOP_MAX:
            add("switching loop", "FAIL", f"{phase}: nearest ceramic {best[1]} is {best[2]:.1f} mm from the HS drain "
                f"and {best[3]:.1f} mm from the LS source, pad to pad (limit {LOOP_MAX} mm together): "
                "the hot loop closes far away", [hs, ls, best[1]])
        else:
            add("switching loop", "PASS", f"{phase}: {best[1]} sits {best[2]:.1f} mm from {hs}'s drain and "
                f"{best[3]:.1f} mm from {ls}'s source (pad to pad)", [hs, ls, best[1]])

    # ---------------------------------------------------------------- charge pump
    drv = next((r for r, f in fps.items() if "RTV0032E" in f.GetFPIDAsString()), None)
    if drv:
        drv_pins = {p.GetNetname(): p for p in pads(drv)}
        for net in ("Net-(U5-VCP)", "Net-(U5-CPH)", "Net-(U5-CPL)", "Net-(U5-DVDD)"):
            pin = drv_pins.get(net)
            cap = next((r for r in fps if r.startswith("C") and any(p.GetNetname() == net for p in pads(r))), None)
            if not (pin and cap):
                continue
            cp = next(p for p in pads(cap) if p.GetNetname() == net)
            d = dist((mm(pin.GetPosition().x), mm(pin.GetPosition().y)), (mm(cp.GetPosition().x), mm(cp.GetPosition().y)))
            add("charge pump", "PASS" if d <= CP_MAX else "WARN",
                f"{cap} on {net}: {d:.1f} mm from {drv} pin (limit {CP_MAX} mm)", [cap, drv])

    # ---------------------------------------------------------------- gate drive
    def net_len(net):
        L = sum(mm(t.GetLength()) for t in tracks if t.GetNetname() == net and t.GetClass() == "PCB_TRACK")
        v = sum(1 for t in tracks if t.GetNetname() == net and t.GetClass() == "PCB_VIA")
        return L, v
    if drv:
        for p in pads(drv):
            n = p.GetNetname()
            if not re.search(r"(^|-)G[HL][ABC]\)?$|^G[HL][ABC]$", n):
                continue
            # driver pin -> series resistor -> FET gate net
            series = next((r for r in fps if r.startswith("R") and any(q.GetNetname() == n for q in pads(r))), None)
            gate_net = next((q.GetNetname() for q in pads(series) if q.GetNetname() != n), None) if series else None
            L1, v1 = net_len(n)
            L2, v2 = net_len(gate_net) if gate_net else (0, 0)
            total, vias = L1 + L2, v1 + v2
            add("gate drive", "PASS" if total <= GATE_WARN else "WARN",
                f"{n} -> {series} -> {gate_net}: {total:.1f} mm of track, {vias} vias"
                + ("" if total <= GATE_WARN else f" (over {GATE_WARN} mm: keep it short, return path alongside)"),
                [drv, series or ""])

    # ---------------------------------------------------------------- phase crossings
    phase_zones = [z for z in b.Zones() if not z.GetIsRuleArea() and z.GetNetname().startswith("/Phase_")]
    gate_nets_of = {}
    for phase, br in bridges.items():
        own = {phase, "BEMF_" + phase[-1]}
        for fet in br.values():
            for q in pads(fet):
                own.add(q.GetNetname())
            for r in fps:
                if r.startswith("R") and any(q.GetNetname() in own and q.GetNetname() not in PLANES for q in pads(r)):
                    own.update(q.GetNetname() for q in pads(r))
        own.update(n for n in ("GH" + phase[-1], "GL" + phase[-1], f"Net-(U5-GH{phase[-1]})", f"Net-(U5-GL{phase[-1]})"))
        gate_nets_of[phase] = own - set(PLANES)
    crossings = {}
    for t in tracks:
        if t.GetClass() != "PCB_TRACK" or t.GetNetname() in PLANES:
            continue
        for z in phase_zones:
            if not z.IsOnLayer(t.GetLayer()) or t.GetNetname() in gate_nets_of.get(z.GetNetname(), set()):
                continue
            mid = pcbnew.VECTOR2I((t.GetStart().x + t.GetEnd().x) // 2, (t.GetStart().y + t.GetEnd().y) // 2)
            if z.Outline().Contains(mid):
                key = (t.GetNetname(), z.GetNetname(), b.GetLayerName(t.GetLayer()))
                crossings[key] = crossings.get(key, 0) + mm(t.GetLength())
    for (net, phase, layer), L in sorted(crossings.items()):
        add("phase crossings", "WARN", f"{net} runs {L:.1f} mm inside the {phase} pour on {layer}", [])
    if not crossings:
        add("phase crossings", "PASS", "no foreign signal runs inside a phase pour on any layer")

    # ---------------------------------------------------------------- via in pad
    smd = [p for f in fps.values() for p in f.Pads() if p.GetAttribute() == pcbnew.PAD_ATTRIB_SMD]
    vip = []
    for v in (t for t in tracks if t.GetClass() == "PCB_VIA"):
        for p in smd:
            if p.HitTest(v.GetPosition()):
                vip.append(f"{p.GetParentFootprint().GetReference()}.{p.GetNumber()}")
                break
    epad_vias = [f"{r}" for r, f in fps.items() for p in f.Pads()
                 if p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH and any(q.GetAttribute() == pcbnew.PAD_ATTRIB_SMD
                                                                       and q.GetNumber() == p.GetNumber() for q in f.Pads())]
    if vip:
        add("via in pad", "WARN", f"{len(vip)} vias sit inside SMD pads ({', '.join(sorted(set(vip))[:12])}) - "
            "order them plugged/capped or move them off the pad", sorted(set(vip)))
    else:
        add("via in pad", "PASS", "no routed or stitching via sits inside an SMD pad")
    if epad_vias:
        add("via in pad", "INFO", f"exposed-pad thermal vias in {', '.join(sorted(set(epad_vias)))}: "
            "0.3 mm drills, specify 'via plugging / epoxy filled' at JLCPCB or accept slight solder wicking",
            sorted(set(epad_vias)))

    # ---------------------------------------------------------------- ground return
    batm = next((r for r in fps if r.lower().startswith("bat-")), None)
    if batm and bridges:
        bp = centre(pads(batm))
        for phase, br in sorted(bridges.items()):
            src = centre(pads(br["ls"], "GND"))
            for sensitive in ("Y1", "U4"):
                if sensitive not in fps:
                    continue
                c = (mm(fps[sensitive].GetPosition().x), mm(fps[sensitive].GetPosition().y))
                ax, ay, bx, by = src[0], src[1], bp[0], bp[1]
                t_ = max(0, min(1, ((c[0] - ax) * (bx - ax) + (c[1] - ay) * (by - ay)) / ((bx - ax) ** 2 + (by - ay) ** 2)))
                d = dist(c, (ax + t_ * (bx - ax), ay + t_ * (by - ay)))
                if d < RETURN_KEEP:
                    add("ground return", "WARN", f"{phase} low-side return to {batm} passes {d:.1f} mm from "
                        f"{sensitive}: high current in the GND plane right under it", [br["ls"], batm, sensitive])
        if not any(r["check"] == "ground return" for r in results):
            add("ground return", "PASS", f"no low-side return path to {batm} passes within {RETURN_KEEP} mm of the "
                "crystal or MCU")

    # ---------------------------------------------------------------- current sense
    shunt = next((r for r, f in fps.items() if "Shunt" in f.GetFPIDAsString()), None)
    if shunt:
        f = fps[shunt]
        ties = f.IsNetTie()
        sense = [p for p in f.Pads() if p.GetNumber() in ("2", "3")]
        gnd_pad = next((p for p in f.Pads() if p.GetNetname() == "GND"), None)
        routed = all(any(t.GetNetname() == p.GetNetname() and t.GetClass() == "PCB_TRACK" and
                         (t.GetStart() == p.GetPosition() or t.GetEnd() == p.GetPosition() or p.HitTest(t.GetStart())
                          or p.HitTest(t.GetEnd())) for t in tracks) for p in sense)
        add("current sense", "PASS" if ties and len(sense) == 2 and routed else "FAIL",
            f"{shunt}: Kelvin sense {'routed from its own sense pads 2/3' if routed else 'NOT routed from pads 2/3'}"
            f"{'' if ties else ' (footprint is not a net tie)'}", [shunt])
        if gnd_pad is not None:
            gp = gnd_pad.GetPosition()
            n = sum(1 for t in tracks if t.GetClass() == "PCB_VIA" and t.GetNetname() == "GND"
                    and (t.GetPosition() - gp).EuclideanNorm() < pcbnew.FromMM(8))
            add("current sense", "PASS" if n >= 24 else "WARN",
                f"{n} GND vias within 8 mm of {shunt}'s GND pad carry the return current to it "
                f"({'ok' if n >= 24 else 'want 24+ for ~40 A'})", [shunt])

    # ---------------------------------------------------------------- mounting holes, 3D models
    for r, f in sorted(fps.items()):
        if r.startswith("H"):
            nets = {p.GetNetname() for p in f.Pads() if p.GetNetname()}
            plated = any(p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH for p in f.Pads())
            if nets or plated:
                add("mounting holes", "WARN", f"{r} is plated / on net {nets}: metal standoffs would tie it to the frame", [r])
    if not any(r["check"] == "mounting holes" for r in results):
        add("mounting holes", "PASS", "mounting holes are plain NPTH, not on any net")
    env = {"KIPRJMOD": str(board_path.parent), "KICAD9_3DMODEL_DIR": str(KICAD_SHARE / "3dmodels")}
    missing = []
    for r, f in sorted(fps.items()):
        bodyless = r.startswith("H") or "SolderWirePad" in f.GetFPIDAsString()
        ms = [m.m_Filename for m in f.Models()]
        ok = any(Path(re.sub(r"\$\{(\w+)\}", lambda v: env.get(v.group(1), v.group(0)), m)).exists() for m in ms)
        if not bodyless and not ok:
            missing.append(r)
    if os.environ.get("AUTOPCB_DOCKER") and not (KICAD_SHARE / "3dmodels").exists():
        add("3D models", "INFO", "not checked in the container (no 3D library): the host re-runs this check", [])
    else:
        add("3D models", "PASS" if not missing else "WARN",
            "every part with a body has a 3D model that loads" if not missing else f"no loadable 3D model: {', '.join(missing)}",
            missing)

    # ---------------------------------------------------------------- write
    order = {"FAIL": 0, "WARN": 1, "INFO": 2, "PASS": 3}
    results.sort(key=lambda r: (order[r["status"]], r["check"]))
    counts = {k: sum(1 for r in results if r["status"] == k) for k in order}
    (out / "design_checks.json").write_text(json.dumps({"counts": counts, "results": results}, indent=1), encoding="utf-8")
    md = [f"# Design checks: {board_path.name}", "",
          f"**{counts['FAIL']} fail, {counts['WARN']} warn, {counts['INFO']} info, {counts['PASS']} pass.** "
          "Measured on the board by `tools/esc_checks.py`. DRC/ERC are reported separately.", "",
          "| Status | Check | Finding |", "|---|---|---|"]
    md += [f"| {r['status']} | {r['check']} | {r['message']} |" for r in results]
    (out / "design_checks.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    for r in results:
        if r["status"] != "PASS":
            print(f"  {r['status']:4} {r['check']}: {r['message']}")
    print(f"design checks: {counts['FAIL']} fail, {counts['WARN']} warn, {counts['INFO']} info, {counts['PASS']} pass "
          f"-> {out / 'design_checks.md'}")


if __name__ == "__main__":
    main()

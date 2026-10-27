"""Build the ESC V2 board (4-layer, double-sided) from the V2 schematic netlist.

Run with KiCad's Python (needs pcbnew):
    vendor/KiCAD-MCP-Server/venv/Scripts/python.exe tools/build_esc_v2.py workspace/esc_v2

Stackup  F.Cu  power stage (FETs, phase pours, VM/GND islands), bulk caps, pads
         In1   GND plane
         In2   VM plane (+3.3V island under the MCU)
         B.Cu  logic (DRV8320, MCU, LDO, passives), phase pours under the FETs, GND fill
High-current nets (VM, GND, phases) are zones + via arrays; signals are routed afterwards.
"""

from __future__ import annotations

import sys as _sys, os as _os; _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _quiet  # noqa: E402,F401  - no console windows for child processes on Windows

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pcbnew
import wx

# KiCad's debug asserts pop up modal dialogs on the user's desktop; never show them from scripts.
wx.DisableAsserts()

from _env import KPY, KICAD_CLI, KICAD_SHARE  # noqa: E402  - KiCad paths (Windows default / Docker override)
KICAD_FP = KICAD_SHARE / "footprints"
OX, OY = 100.0, 100.0           # board origin (top-left) in board coordinates
W, H = 48.0, 58.0               # board size, mm
MM = pcbnew.FromMM

# phase column centres (x) -- A left, B middle, C right. The DRV8320 sits on the bottom, so its pinout is
# mirrored: A pins face left, then B and C left-to-right along the top edge. Ordering the half-bridges the same
# way makes every gate / phase-sense line run straight to its FETs with no crossings. (Phase order on a BLDC
# ESC is arbitrary: swapping two motor wires, or the firmware's direction setting, gives the same motor.)
CX = {"A": 11.0, "B": 24.0, "C": 37.0}
# Which side of each column the high-side FET sits on (+1 = left, -1 = right). Column B is mirrored so its gate /
# phase-sense pins on the DRV8320's top edge (GLB, SHB, GHB left to right) face LS, phase, HS in the same order:
# all three half-bridges then route to the driver without a single crossing.
SIDE = {"A": 1, "B": -1, "C": 1}


def hs_x(p):
    return CX[p] - 3.5 * SIDE[p]


def ls_x(p):
    return CX[p] + 3.5 * SIDE[p]


def mx(p, dx):
    """x of a feature drawn for a left-HS column at offset dx from the column centre, mirrored for column B."""
    return CX[p] + SIDE[p] * dx
FET_Y = 21.0

# (ref, x, y, rotation, side)  side: F = top, B = bottom
PLACEMENT = [
    # mounting
    ("H1", 4, 4, 0, "F"), ("H2", 44, 4, 0, "F"), ("H3", 4, 54, 0, "F"), ("H4", 44, 54, 0, "F"),
    # motor pads and half-bridges (HS rotated 180 so source/phase faces the motor pad; LS drain faces it)
    ("MotC1", CX["C"], 8.5, 0, "F"), ("MotB1", CX["B"], 8.5, 0, "F"), ("MotA1", CX["A"], 8.5, 0, "F"),
    # DNK8 at 0 deg: source/gate pins on the left, drain on the right. LS at 90 -> drain (phase) up,
    # source (GND) down. HS at 270 -> source (phase) up, drain (VM) down.
    ("U7", hs_x("C"), FET_Y, 270, "F"), ("U8", ls_x("C"), FET_Y, 90, "F"),
    ("U3", hs_x("B"), FET_Y, 270, "F"), ("U6", ls_x("B"), FET_Y, 90, "F"),
    ("U1", hs_x("A"), FET_Y, 270, "F"), ("U2", ls_x("A"), FET_Y, 90, "F"),
    # input power, bulk, protection
    # C11 (10 mm can) sits in the right-hand VM area, clear of the DRV8320/MCU on the other side;
    # D2 lies flat along the bottom edge, below the MCU footprint.
    # each half-bridge gets its own ceramic right across the HS drain (VM) and LS source (GND): the switching
    # loop closes in a few mm instead of through the bulk cap or the battery pads
    ("C23", CX["A"], 26.8, 0 if SIDE["A"] > 0 else 180, "F"), ("C24", CX["C"], 26.8, 0 if SIDE["C"] > 0 else 180, "F"),
    ("C12", CX["B"], 26.8, 0 if SIDE["B"] > 0 else 180, "F"), ("C11", 35.2, 35.0, 180, "F"), ("D2", 24.0, 55.6, 0, "F"),   # C12 VM pad toward the HS drain
    # battery pads at the sides, level with the FET rows: the low-side return runs straight from the FETs to BAT-
    # (left) and the supply comes in at BAT+ (right) beside the bulk cap - neither crosses the logic area
    ("Bat-1", 3.3, 35.0, 0, "F"), ("Bat+1", 45.0, 35.0, 0, "F"),
    # bus current sense: the 0.5 mOhm Kelvin shunt R31 sits right beside BAT- (its BATN force pad next to the
    # pad, its GND force pad on the GND block); U10 INA181A3 + filters on the bottom, under its sense pads
    ("R31", 10.0, 35.0, 0, "F"),
    ("U10", 10.0, 30.9, 0, "B"), ("R33", 7.0, 31.2, 90, "B"), ("R32", 13.0, 31.2, 90, "B"),
    ("C25", 10.0, 33.9, 0, "B"), ("C26", 13.2, 34.2, 0, "B"),
    ("R34", 5.0, 34.0, 90, "B"), ("C27", 5.0, 37.4, 90, "B"),
    # wire pads on the left edge, programming header
    ("SIG1", 9.0, 52.5, 0, "F"), ("GND1", 12.2, 52.5, 0, "F"), ("3V3", 15.4, 52.5, 0, "F"),   # bottom-left
    # 5 V in from the PDB lands on the right edge, beside the LDO (a few mm of 5 V track instead of ~35 mm)
    ("5V1", 45.6, 44.5, 0, "F"),
    ("J1", 12.0, 44.0, 0, "F"),
    # ---- bottom: gate resistors + pull-downs are placed next to each FET's gate pad by place_gate_parts()
    # gate driver + its passives
    ("U5", 24.0, 35.2, 0, "B"),
    # charge pump right at its pins: C14 (VCP-VM) beside VCP, C16 (flying cap) at the CPH/CPL corner
    # charge pump at the lower-left corner of U5, below its phase-A pins (GHA/SHA/GLA must escape left freely)
    ("C14", 18.9, 37.6, 270, "B"), ("C13", 16.8, 36.8, 90, "B"), ("C16", 21.6, 39.5, 0, "B"),
    # R30 (VDS level) and R11 (nFAULT pull-up) sit right of U5 by the pins they serve, keeping the strip
    # between U5 and the MCU free for the six PWM lines
    ("C18", 29.0, 38.0, 90, "B"), ("R30", 30.8, 34.8, 90, "B"), ("R11", 30.8, 31.4, 90, "B"),
    # MCU + decoupling
    ("U4", 24.0, 48.0, 0, "B"),   # 1.5 mm lower: a wider strip under the DRV8320
    ("C2", 16.5, 43.6, 90, "B"), ("C3", 16.5, 46.8, 90, "B"), ("C20", 16.5, 50.0, 90, "B"),
    ("C4", 33.6, 43.6, 90, "B"), ("C5", 33.6, 46.8, 90, "B"), ("C1", 33.6, 50.0, 90, "B"),   # corridor for PWM lines
    ("C8", 34.0, 39.0, 0, "B"),
    # clock, reset, boot, LED, SWD pulls
    ("Y1", 11.5, 47.3, 90, "B"), ("C17", 11.5, 43.3, 0, "B"), ("C19", 11.5, 51.3, 0, "B"),
    ("R3", 8.5, 38.0, 90, "B"), ("C15", 11.0, 38.0, 90, "B"), ("R10", 13.5, 38.0, 90, "B"),
    ("D1", 8.0, 45.8, 90, "B"), ("R18", 8.0, 49.8, 90, "B"),
    ("R9", 36.0, 52.0, 90, "B"), ("R6", 38.0, 52.0, 90, "B"),
    # signal input
    ("R19", 5.0, 46.2, 90, "B"), ("R29", 5.0, 49.6, 90, "B"),   # by the SIG pad (bottom-left), clear of BAT-
    # LDO
    # LDO turned so its 5 V pin faces the 5V pad and its 3.3 V tab faces the MCU
    ("U9", 39.5, 44.6, 180, "B"), ("C21", 45.0, 49.4, 0, "B"), ("C22", 41.0, 50.4, 0, "B"),
    # VDDA filter + BEMF dividers: two horizontal rows under the MCU, between the bottom mounting holes
    # one row along the bottom edge, between the mounting holes: VDDA filter, then the BEMF low sides
    ("FB1", 8.4, 56.0, 0, "B"), ("C6", 11.5, 56.0, 0, "B"), ("C7", 14.6, 56.0, 0, "B"),
    ("C9", 17.7, 56.0, 0, "B"), ("C10", 20.8, 56.0, 0, "B"),
    # BEMF dividers: the 20k high sides sit under each low-side FET, inside the bottom phase pour (their phase end
    # joins the pour with no track, so the switching node never leaves the power stage); only the divided-down
    # BEMF signal runs to the MCU. The 2.2k / 10k low sides stay in the rows under the MCU.
    # vertical, phase pad up in the pour and BEMF pad down, so the BEMF track leaves without cutting the pour
    ("R20", ls_x("A"), 18.2, 270, "B"), ("R21", ls_x("B"), 18.2, 270, "B"), ("R22", ls_x("C"), 18.2, 270, "B"),
    ("R24", 23.9, 56.0, 0, "B"), ("R27", 27.0, 56.0, 0, "B"), ("R25", 30.1, 56.0, 0, "B"),
    ("R28", 33.2, 56.0, 0, "B"), ("R23", 36.3, 56.0, 0, "B"), ("R26", 39.4, 56.0, 0, "B"),
]

# Gate parts: (fet, series R, pull-down R). Placed on the bottom just beside the FET's gate pad.
GATE_PARTS = [("U7", "R14", "R15"), ("U3", "R7", "R8"), ("U1", "R2", "R1"),
              ("U8", "R16", "R17"), ("U6", "R12", "R13"), ("U2", "R4", "R5")]


def place_gate_parts(board) -> None:
    """Series R and pull-down R as a horizontal pair on the bottom, stacked so their gate pads (pad 2) line up.
    Gate pads (pad 2) face the FET's gate pin; the series R's driver pad and the pull-down's phase/GND pad
    (pad 1) sit on the far side, so the driver route never has to pass the gate via or the gate link.
      HS: pull-down on top (next to the phase via field), series below it, under the HS FET.
      LS: series on top (next to the gate via), pull-down below, beside the low-side GND via field."""
    u5x = next(x for r, x, *_ in PLACEMENT if r == "U5")
    for fet, rs, rpd in GATE_PARTS:
        f = board.FindFootprintByReference(fet)
        gate = next(p for p in f.Pads() if p.GetNumber() == "4")
        gx, gy = pcbnew.ToMM(gate.GetPosition().x) - OX, pcbnew.ToMM(gate.GetPosition().y) - OY
        hs = gy < FET_Y  # high-side gates sit at the top (phase) edge of the FET
        if hs:
            x = pcbnew.ToMM(f.GetPosition().x) - OX
            # gate pads (pad 2) toward the FET gate pin, driver / phase pads (pad 1) on the far side
            rot = 180 if gx < x else 0    # on the bottom side, 0 deg puts pad 1 on the left
            spots = {rpd: (x, 20.2), rs: (x, 21.8)}
        else:
            # LS: both standing, side by side right under the gate via: gate pads up (to the via), driver / GND
            # pads down (toward U5); the series R on the side nearer U5
            t = 1 if u5x > gx else -1
            rot = 90                      # on the bottom side, 90 deg puts pad 1 below pad 2
            cx_ = gx - (2.2 if fet == "U6" else 0.0)   # U6's gate is right above U5's top pins: keep them clear
            spots = {rs: (cx_ + 0.85 * t, 30.0), rpd: (cx_ - 0.85 * t, 30.0)}
        for ref, (x_, y) in spots.items():
            fp = board.FindFootprintByReference(ref)
            fp.SetPosition(pt(x_, y))
            if not fp.IsFlipped():
                fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
            fp.SetOrientationDegrees(rot)

POWER_NETS = ("GND", "/VM")
POUR_ONLY = ("GND", "/VM", "/BATN")   # nets carried by pours, never fanned out or routed as signals
FANOUT_NETS = ("GND", "/VM")


# ------------------------------------------------------------------ silkscreen
SILK_REFS = ("U", "J")            # designators kept on silk; everything else goes to the Fab layer
SILK_LABELS = [                    # (text, x, y, side, size)
    ("C", CX["C"], 1.4, "F", 1.2), ("B", CX["B"], 1.4, "F", 1.2), ("A", CX["A"], 1.4, "F", 1.2),
    ("BAT-", 3.3, 41.6, "F", 1.0), ("BAT+", 45.0, 41.6, "F", 1.0),
    ("SIG", 9.0, 49.6, "F", 0.8), ("5V", 42.4, 44.5, "F", 0.8), ("GND", 12.2, 49.6, "F", 0.8), ("3V3", 15.4, 49.6, "F", 0.8),
    ("ESC V2", 24.0, 2.2, "B", 1.0),
]


def tidy_silkscreen(board) -> None:
    for fp in board.GetFootprints():
        bottom = fp.IsFlipped()
        fab = pcbnew.B_Fab if bottom else pcbnew.F_Fab
        fp.Value().SetLayer(fab)
        for field in fp.GetFields():
            if field.GetName() in ("LCSC", "MPN", "Manufacturer"):
                field.SetVisible(False)
        ref = fp.Reference()
        if fp.GetReference().startswith(SILK_REFS) and not fp.GetReference().startswith("U") or \
                fp.GetReference() in ("U4", "U5", "U9"):
            ref.SetTextSize(pcbnew.VECTOR2I(MM(0.8), MM(0.8)))
            ref.SetTextThickness(MM(0.12))
        else:
            ref.SetLayer(fab)
    for text, x, y, side, size in SILK_LABELS:
        t = pcbnew.PCB_TEXT(board)
        t.SetText(text)
        t.SetPosition(pt(x, y))
        t.SetLayer(pcbnew.B_SilkS if side == "B" else pcbnew.F_SilkS)
        t.SetTextSize(pcbnew.VECTOR2I(MM(size), MM(size)))
        t.SetTextThickness(MM(size * 0.15))
        if side == "B":
            t.SetMirrored(True)
        board.Add(t)


# ------------------------------------------------------------------ copper
def _poly(points):
    return [(OX + x, OY + y) for x, y in points]


def zone(board, net, layer, points, priority, solid=True, min_width=0.25, clearance=0.25):
    z = pcbnew.ZONE(board)
    z.SetLayer(layer)
    z.SetNet(net)
    ol = z.Outline()
    ol.NewOutline()
    for x, y in _poly(points):
        ol.Append(MM(x), MM(y))
    ol.Outline(0).SetClosed(True)
    z.SetAssignedPriority(priority)
    z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL if solid else pcbnew.ZONE_CONNECTION_THERMAL)
    z.SetMinThickness(MM(min_width))
    z.SetLocalClearance(MM(clearance))
    z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
    board.Add(z)
    return z


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def phase_poly(p):
    """Phase copper: everything above the FET row, plus the LS drain pad below it."""
    return [(mx(p, -6.4), 0.6), (mx(p, 6.4), 0.6), (mx(p, 6.4), 22.4), (mx(p, 0.5), 22.4), (mx(p, 0.5), 19.3),
            (mx(p, -6.4), 19.3)]


# In2 VM plane: full width under the FET rows and phase area, plus the right-hand block where C11+, the VM tie
# vias and the high-side drains meet. Left of x=32 below the FET rows (the DRV8320 / MCU area) In2 is a
# routing layer, so the gate driver's 0.5 mm-pitch pins can escape on two layers.
VM_PLANE = [(0.5, 0.5), (W - 0.5, 0.5), (W - 0.5, 36.8), (32.0, 36.8), (32.0, 29.6), (0.5, 29.6)]


def add_zones(board, nets) -> None:
    gnd, vm = nets["GND"], nets["/VM"]
    phase = {p: nets[f"/Phase_{p}"] for p in "ABC"}
    edge = (0.5, 0.5, W - 0.5, H - 0.5)
    for p, cx in CX.items():
        zone(board, phase[p], pcbnew.F_Cu, phase_poly(p), 3)
        zone(board, phase[p], pcbnew.B_Cu, phase_poly(p), 3)
        a, b = sorted((mx(p, 0.5), mx(p, 6.4)))     # 6.4: columns A and B (mirrored) leave a 0.2 mm gap
        zone(board, gnd, pcbnew.F_Cu, rect(a, 22.7, b, 29.3), 2)                    # LS source island
    zone(board, gnd, pcbnew.F_Cu, rect(0.5, 29.6, 18.0, H - 0.5), 2)               # left/bottom GND block
    zone(board, nets["/BATN"], pcbnew.F_Cu, rect(0.5, 29.6, 8.0, 40.6), 5)         # BAT- pad -> shunt R31 only
    zone(board, gnd, pcbnew.F_Cu, rect(18.0, 38.6, 28.0, 52.6), 4)                 # under the MCU: GND + routing
    zone(board, gnd, pcbnew.F_Cu, rect(17.5, 52.6, 23.4, H - 0.5), 3)               # TVS (D2) ground pad -> GND block
    zone(board, gnd, pcbnew.F_Cu, rect(28.4, 31.8, 33.6, 38.4), 2)                 # C11 ground pad (left of the can)
    # VM everywhere else on top, reaching up under each high-side drain pad (no fan-out needed)
    vm_poly = [(0.5, 22.7)]
    for p in sorted(CX, key=CX.get):          # left to right along the outline
        a, b = sorted((mx(p, -6.4), mx(p, -0.6)))                                 # notch up under the HS drain
        vm_poly += [(a, 22.7), (a, 19.6), (b, 19.6), (b, 22.7)]
    vm_poly += [(W - 0.5, 22.7), (W - 0.5, H - 0.5), (0.5, H - 0.5)]
    zone(board, vm, pcbnew.F_Cu, vm_poly, 1)
    zone(board, gnd, pcbnew.In1_Cu, rect(*edge), 0, solid=False)                    # GND plane
    zone(board, vm, pcbnew.In2_Cu, VM_PLANE, 1)       # VM plane: under the FETs + the C11 / tie-via block
    # (logic half of In2 is a routing layer; In1 GND is the reference plane for everything)
    zone(board, gnd, pcbnew.B_Cu, rect(*edge), 1)                                   # bottom GND fill (solid pads)


_STATIC = {}


def _static(board):
    """Pads, mounting holes and via keep-outs never move once parts are placed: gather them once (fast _blocked)."""
    if board.GetFileName() + str(id(board)) not in _STATIC:
        pads = []
        holes = []
        for fp in board.GetFootprints():
            for pad in fp.Pads():
                bb = pad.GetBoundingBox()
                pads.append((pcbnew.ToMM(bb.GetLeft()), pcbnew.ToMM(bb.GetTop()), pcbnew.ToMM(bb.GetRight()),
                             pcbnew.ToMM(bb.GetBottom()), pad.GetNetCode(), pad.GetAttribute() == pcbnew.PAD_ATTRIB_SMD))
            if fp.GetReference().startswith("H"):
                c = fp.GetPosition(); holes.append((pcbnew.ToMM(c.x), pcbnew.ToMM(c.y)))
        areas = [z.Outline() for z in board.Zones() if z.GetIsRuleArea() and z.GetDoNotAllowVias()]
        for f in board.GetFootprints():
            areas += [z.Outline() for z in f.Zones() if z.GetIsRuleArea() and z.GetDoNotAllowVias()]
        _STATIC.clear()
        _STATIC[board.GetFileName() + str(id(board))] = (pads, holes, areas)
    return _STATIC[board.GetFileName() + str(id(board))]


def _blocked(board, x, y, r=0.3, net=None, extra=0.2) -> bool:
    """True if a via of radius r at (x, y) would hit another net's pad / via / track, a via keep-out, or leave
    the board."""
    if not (0.8 <= x - OX <= W - 0.8 and 0.8 <= y - OY <= H - 0.8):
        return True
    pads, holes, areas = _static(board)
    e = r + extra
    for x0, y0, x1, y1, nc, smd in pads:
        if x0 - e <= x <= x1 + e and y0 - e <= y <= y1 + e and (net is None or nc != net.GetNetCode() or smd):
            return True
    for hx, hy in holes:
        if (hx - x) ** 2 + (hy - y) ** 2 < (3.6 + r) ** 2:
            return True
    p = pcbnew.VECTOR2I(MM(x), MM(y))
    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA":
            q = t.GetPosition()
            if (pcbnew.ToMM(q.x) - x) ** 2 + (pcbnew.ToMM(q.y) - y) ** 2 < (2 * r + extra) ** 2:
                return True
        elif net is None or t.GetNetCode() != net.GetNetCode():
            if t.HitTest(p, MM(e) + t.GetWidth() // 2):
                return True
    for ol in areas:      # rule areas that forbid vias (e.g. under the TVS): keep the whole disc out
        for dx, dy in ((0, 0), (r, 0), (-r, 0), (0, r), (0, -r), (0.7 * r, 0.7 * r), (-0.7 * r, 0.7 * r),
                       (0.7 * r, -0.7 * r), (-0.7 * r, -0.7 * r)):
            if ol.Contains(pcbnew.VECTOR2I(MM(x + dx), MM(y + dy))):
                return True
    return False


def via(board, net, x, y, size=0.6, drill=0.3) -> bool:
    if _blocked(board, x, y, size / 2, net):
        return False
    v = pcbnew.PCB_VIA(board)
    v.SetPosition(pcbnew.VECTOR2I(MM(x), MM(y)))
    v.SetWidth(MM(size))
    v.SetDrill(MM(drill))
    v.SetNet(net)
    v.SetViaType(pcbnew.VIATYPE_THROUGH)
    v.SetLocked(True)
    board.Add(v)
    return True


def via_field(board, net, x0, y0, x1, y1, pitch=1.2) -> int:
    n = 0
    y = y0
    while y <= y1 + 1e-6:
        x = x0
        while x <= x1 + 1e-6:
            n += via(board, net, OX + x, OY + y)
            x += pitch
        y += pitch
    return n


def add_power_vias(board, nets) -> None:
    gnd, vm = nets["GND"], nets["/VM"]
    counts = {}
    for p, cx in CX.items():
        ph = nets[f"/Phase_{p}"]
        counts[f"Phase_{p}"] = via_field(board, ph, cx - 5.6, 13.2, cx + 5.6, 16.8)
        a, b = sorted((mx(p, -6.0), mx(p, -1.2)))
        counts[f"VM_HS_{p}"] = via_field(board, vm, a, 24.8, b, 28.4)
        a, b = sorted((mx(p, 1.4), mx(p, 6.2)))
        counts[f"GND_LS_{p}"] = via_field(board, gnd, a, 24.8, b, 28.4)
    counts["VM tie"] = via_field(board, vm, 34.2, 30.4, 41.6, 36.4)                 # BAT+ / C11+ VM pour <-> In2 VM plane
    # the GND side of the shunt to the plane: all battery return current goes plane -> these vias -> R31 -> BAT-
    counts["Bat-"] = via_field(board, gnd, 13.6, 32.4, 17.2, 42.0)
    counts["D2 GND"] = via_field(board, gnd, 19.6, 53.2, 19.6, 55.6)               # D2 GND patch -> plane (between BAT- and D2)
    counts["C11-"] = via_field(board, gnd, 29.0, 32.2, 33.0, 32.2) + via_field(board, gnd, 29.0, 37.8, 33.0, 37.8)
    # GND stitching across the board (ties F/In1/B grounds together)
    counts["stitch"] = sum(via(board, gnd, OX + x, OY + y, 0.6, 0.3)
                           for x in (2.0, 46.0) for y in (12.0, 18.0, 24.0, 30.0, 47.0)
                           if not (x < 8 and 29 < y < 41))          # (not inside the BATN pour)
    print("power vias:", counts)


def _in_own_zone(board, pad) -> bool:
    layer = pcbnew.B_Cu if pad.IsOnLayer(pcbnew.B_Cu) and not pad.IsOnLayer(pcbnew.F_Cu) else pcbnew.F_Cu
    best = None
    for z in board.Zones():
        if z.GetLayer() == layer and z.Outline().Contains(pad.GetPosition()):
            if best is None or z.GetAssignedPriority() > best.GetAssignedPriority():
                best = z
    return best is not None and best.GetNetCode() == pad.GetNetCode()


def _reaches_net(board, x, y, netcode) -> bool:
    """A through via at (x, y) lands in a pour of its own net on at least one copper layer."""
    p = pcbnew.VECTOR2I(MM(x), MM(y))
    for layer in (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu):
        best = None
        for z in board.Zones():
            if not z.GetIsRuleArea() and z.GetLayer() == layer and z.Outline().Contains(p):
                if best is None or z.GetAssignedPriority() > best.GetAssignedPriority():
                    best = z
        if best is not None and best.GetNetCode() == netcode:
            return True
    return False


def strap_tab_pins(board) -> None:
    """Pads of one footprint that share a number (SOT-223 pin 2 + tab) are joined in the package, but the PCB
    still needs copper between them: a short wide strap under the body."""
    for fp in board.GetFootprints():
        by_num = {}
        for pd in fp.Pads():
            if pd.GetNumber() and pd.GetNetCode():
                by_num.setdefault(pd.GetNumber(), []).append(pd)
        for num, pads in by_num.items():
            if len(pads) == 2 and fp.IsFlipped():          # top-side parts sit in their own pours already
                t = pcbnew.PCB_TRACK(board)
                t.SetStart(pads[0].GetPosition()); t.SetEnd(pads[1].GetPosition())
                t.SetWidth(MM(0.6)); t.SetNet(pads[0].GetNet()); t.SetLocked(True)
                t.SetLayer(pcbnew.B_Cu if fp.IsFlipped() else pcbnew.F_Cu)
                board.Add(t)
                print(f"strapped {fp.GetReference()} pin {num} to its tab")


def _stub_clear(board, pad, fp, x, y, width=0.25, clr=0.25) -> bool:
    """The fan-out stub from the pad centre to (x, y) must clear every other-net pad on its layer."""
    layer = pcbnew.B_Cu if fp.IsFlipped() else pcbnew.F_Cu
    c = pad.GetPosition()
    ax, ay = pcbnew.ToMM(c.x), pcbnew.ToMM(c.y)
    for k in range(13):
        px, py = ax + (x - ax) * k / 12, ay + (y - ay) * k / 12
        p = pcbnew.VECTOR2I(MM(px), MM(py))
        for f in board.GetFootprints():
            for q in f.Pads():
                if q.GetNetCode() == pad.GetNetCode() or not q.IsOnLayer(layer):
                    continue
                bb = q.GetBoundingBox(); bb.Inflate(MM(width / 2 + clr))
                if bb.Contains(p):
                    return False
        for t in board.GetTracks():             # vias (any layer) and same-layer tracks of other nets
            if t.GetNetCode() == pad.GetNetCode():
                continue
            if t.GetClass() == "PCB_VIA" or t.GetLayer() == layer:
                if t.HitTest(p, MM(width / 2 + clr)):
                    return False
    return True


def fanout_plane_pads(board, nets) -> None:
    """GND/VM pads that don't sit in a zone of their own net get a short track + via to the plane."""
    added = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetname() not in FANOUT_NETS or pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD:
                continue
            fine = min(pcbnew.ToMM(pad.GetSize().x), pcbnew.ToMM(pad.GetSize().y)) < 0.35
            in_window = _in_window(pcbnew.ToMM(pad.GetPosition().x), pcbnew.ToMM(pad.GetPosition().y),
                                   F_WINDOWS[:2], 0.0)
            if "SolderWirePad" in fp.GetFPIDAsString():
                continue       # wire pads (BAT-, GND1...) sit in their pour and BAT- has its own via field
            if _in_own_zone(board, pad) and not fine and not fp.IsFlipped() and not in_window:
                # Top-side pads sit in solid pours. Bottom-side ones always get their own via (or an exposed-pad
                # stub): routing chops the bottom pour into islands, so a pad can't rely on it to reach the plane.
                continue
            epad = next((q for q in fp.Pads() if q.GetNetCode() == pad.GetNetCode()
                         and pcbnew.ToMM(q.GetSize().x) * pcbnew.ToMM(q.GetSize().y) > 2.0), None)
            if fine and epad is not None:
                # QFN ground pin next to its own exposed pad: short stub straight inward, no via needed
                c, e = pad.GetPosition(), epad.GetPosition()
                bw, bh = pad.GetBoundingBox().GetWidth(), pad.GetBoundingBox().GetHeight()
                end = pcbnew.VECTOR2I(e.x, c.y) if bw > bh else pcbnew.VECTOR2I(c.x, e.y)
                t = pcbnew.PCB_TRACK(board); t.SetStart(c); t.SetEnd(end)
                t.SetWidth(MM(0.2)); t.SetNet(pad.GetNet()); t.SetLocked(True)
                t.SetLayer(pcbnew.B_Cu if fp.IsFlipped() else pcbnew.F_Cu); board.Add(t)
                added.append(f"{fp.GetReference()}.{pad.GetNumber()}->EP")
                continue
            c = pad.GetPosition()
            cx, cy = pcbnew.ToMM(c.x), pcbnew.ToMM(c.y)
            half = max(pcbnew.ToMM(pad.GetBoundingBox().GetWidth()), pcbnew.ToMM(pad.GetBoundingBox().GetHeight())) / 2
            # try directions pointing away from the footprint centre first (QFN/QFP escape outward)
            fc = fp.GetPosition()
            ox, oy = cx - pcbnew.ToMM(fc.x), cy - pcbnew.ToMM(fc.y)
            dirs = [(0, -1), (0, 1), (-1, 0), (1, 0), (-0.7, -0.7), (0.7, -0.7), (-0.7, 0.7), (0.7, 0.7)]
            dirs.sort(key=lambda d: -(d[0] * ox + d[1] * oy))
            dists = (half + 0.55, half + 0.9, half + 1.3, half + 1.8)
            sz = pad.GetSize()
            if min(pcbnew.ToMM(sz.x), pcbnew.ToMM(sz.y)) < 0.35:
                # fine-pitch pin: go straight out, past the neighbours' escape lanes (maze_route.LANE)
                bw, bh = pcbnew.ToMM(pad.GetBoundingBox().GetWidth()), pcbnew.ToMM(pad.GetBoundingBox().GetHeight())
                dirs = [((1 if ox > 0 else -1), 0)] if bw > bh else [(0, (1 if oy > 0 else -1))]
                dists = (half + 1.25, half + 1.6, half + 2.0)
            done = False
            for dist in dists:
                for dx, dy in dirs:
                    x, y = cx + dx * dist, cy + dy * dist
                    if not _stub_clear(board, pad, fp, x, y, 0.25, 0.15 if fine else 0.25)                             or not _reaches_net(board, x, y, pad.GetNetCode()):
                        continue
                    if via(board, pad.GetNet(), x, y):
                        t = pcbnew.PCB_TRACK(board)
                        t.SetStart(c); t.SetEnd(pcbnew.VECTOR2I(MM(x), MM(y)))
                        t.SetWidth(MM(0.25)); t.SetNet(pad.GetNet())
                        t.SetLayer(pcbnew.B_Cu if fp.IsFlipped() else pcbnew.F_Cu)
                        t.SetLocked(True)
                        board.Add(t)
                        added.append(f"{fp.GetReference()}.{pad.GetNumber()}")
                        done = True
                        break
                if done:
                    break
            if not done:
                # no room for its own via (e.g. a keep-out right outside it): an L-shaped stub, straight out past
                # the escape lanes and then sideways, to the nearest via of the same net within 3.5 mm
                bw, bh = pad.GetBoundingBox().GetWidth(), pad.GetBoundingBox().GetHeight()
                layer = pcbnew.B_Cu if fp.IsFlipped() else pcbnew.F_Cu
                cands = sorted((t for t in board.GetTracks() if t.GetClass() == "PCB_VIA"
                                and t.GetNetCode() == pad.GetNetCode()
                                and (t.GetPosition() - c).EuclideanNorm() < MM(3.5)),
                               key=lambda t: (t.GetPosition() - c).EuclideanNorm())
                for v in cands:
                    vp = v.GetPosition()
                    if fine:      # IC pin: straight out along the pin first, past the escape lanes
                        corners = [pcbnew.VECTOR2I(vp.x, c.y) if bw > bh else pcbnew.VECTOR2I(c.x, vp.y)]
                    else:         # passive: either L will do
                        corners = [pcbnew.VECTOR2I(vp.x, c.y), pcbnew.VECTOR2I(c.x, vp.y)]
                    for corner in corners:
                        if fine:
                            outward = (corner - c).x * (c - fc).x + (corner - c).y * (c - fc).y > 0
                            if not outward or (corner - c).EuclideanNorm() < MM(half + 0.9):
                                continue      # the straight leg must leave the chip and clear its escape lanes
                        if _seg_clear(board, pad.GetNetCode(), layer, c, corner, 0.2, 0.15) and \
                                _seg_clear(board, pad.GetNetCode(), layer, corner, vp, 0.2, 0.15):
                            for a_, b_ in ((c, corner), (corner, vp)):
                                if a_ == b_:
                                    continue
                                t = pcbnew.PCB_TRACK(board); t.SetStart(a_); t.SetEnd(b_)
                                t.SetWidth(MM(0.2)); t.SetNet(pad.GetNet()); t.SetLocked(True); t.SetLayer(layer)
                                board.Add(t)
                            added.append(f"{fp.GetReference()}.{pad.GetNumber()}->via")
                            done = True
                            break
                    if done:
                        break
            if not done and pad.GetNetname() in POWER_NETS:
                # pour-only nets aren't routed later: bridge to an adjacent same-net pin of the same part
                mate = next((q for q in fp.Pads() if q.GetPosition() != pad.GetPosition()
                             and q.GetNetCode() == pad.GetNetCode()
                             and (q.GetPosition() - c).EuclideanNorm() <= MM(1.05)), None)
                if mate is not None:
                    t = pcbnew.PCB_TRACK(board); t.SetStart(c); t.SetEnd(mate.GetPosition())
                    t.SetWidth(MM(0.2)); t.SetNet(pad.GetNet()); t.SetLocked(True)
                    t.SetLayer(pcbnew.B_Cu if fp.IsFlipped() else pcbnew.F_Cu); board.Add(t)
                    added.append(f"{fp.GetReference()}.{pad.GetNumber()}->{mate.GetNumber()}")
                    done = True
            if not done:
                print("  could not fan out", fp.GetReference(), pad.GetNumber(), pad.GetNetname())
    print("fanned out:", added)


def _seg_clear(board, netcode, layer, a, b, width, clr) -> bool:
    """A track a->b on `layer` clears every other-net pad, via and same-layer track."""
    ax, ay, bx, by = pcbnew.ToMM(a.x), pcbnew.ToMM(a.y), pcbnew.ToMM(b.x), pcbnew.ToMM(b.y)
    n = max(2, int(((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5 / 0.05))
    pads = [q for f in board.GetFootprints() for q in f.Pads() if q.GetNetCode() != netcode and q.IsOnLayer(layer)]
    others = [t for t in board.GetTracks() if t.GetNetCode() != netcode
              and (t.GetClass() == "PCB_VIA" or t.GetLayer() == layer)]
    for k in range(n + 1):
        p = pcbnew.VECTOR2I(MM(ax + (bx - ax) * k / n), MM(ay + (by - ay) * k / n))
        for q in pads:
            bb = q.GetBoundingBox(); bb.Inflate(MM(width / 2 + clr))
            if bb.Contains(p):
                return False
        for t in others:
            if t.HitTest(p, MM(width / 2 + clr)):
                return False
    return True


def _in_window(x, y, windows, margin=0.15) -> bool:
    """Inside a window with room for half a stub's width (0.125) plus a little slack."""
    return any(OX + a + margin <= x <= OX + c - margin and OY + b + margin <= y <= OY + d - margin
               for a, b, c, d in windows)


def fanout_signal_pads(board, windows) -> None:
    """Top-side signal pads (FET gates, J1, wire pads) get a locked stub + via inside an F.Cu window,
    so the autorouter can work on In2/B only and never needs a track on the top layer."""
    added = []
    for fp in board.GetFootprints():
        if fp.IsFlipped():
            continue
        for pad in fp.Pads():
            name = pad.GetNetname()
            if not name or name in POUR_ONLY or name.startswith("/Phase_") or name.startswith("unconnected-"):
                continue
            if pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD:
                continue
            c = pad.GetPosition()
            cx, cy = pcbnew.ToMM(c.x), pcbnew.ToMM(c.y)
            bb = pad.GetBoundingBox()
            half = max(pcbnew.ToMM(bb.GetWidth()), pcbnew.ToMM(bb.GetHeight())) / 2
            fc = fp.GetPosition()
            ox, oy = cx - pcbnew.ToMM(fc.x), cy - pcbnew.ToMM(fc.y)
            dirs = [(0, -1), (0, 1), (-1, 0), (1, 0), (-0.7, -0.7), (0.7, -0.7), (-0.7, 0.7), (0.7, 0.7)]
            dirs.sort(key=lambda d: -(d[0] * ox + d[1] * oy))
            done = False
            for dist in [half * f + 0.5 for f in (0.0, 0.5, 1.0)] + [half + 0.9, half + 1.4]:
                for dx, dy in dirs:
                    x, y = cx + dx * dist, cy + dy * dist
                    if not _in_window(x, y, windows) or not _stub_clear(board, pad, fp, x, y, 0.25, 0.2):
                        continue
                    if via(board, pad.GetNet(), x, y):
                        t = pcbnew.PCB_TRACK(board)
                        t.SetStart(c); t.SetEnd(pcbnew.VECTOR2I(MM(x), MM(y)))
                        t.SetWidth(MM(0.25)); t.SetNet(pad.GetNet()); t.SetLayer(pcbnew.F_Cu)
                        t.SetLocked(True); board.Add(t)
                        added.append(f"{fp.GetReference()}.{pad.GetNumber()}")
                        done = True
                        break
                if done:
                    break
            if not done:
                print("  could not fan out top pad", fp.GetReference(), pad.GetNumber(), name)
    print("top signal pads fanned out:", added)


IC_REFS = ("U4", "U5", "U9")


def check_overlaps(board) -> list[str]:
    """Same-side body overlaps, and any top part sitting over a bottom IC (or vice versa)."""
    def box(f):
        bb = f.GetBoundingBox(False, False)
        return pcbnew.ToMM(bb.GetX()), pcbnew.ToMM(bb.GetY()), pcbnew.ToMM(bb.GetRight()), pcbnew.ToMM(bb.GetBottom())
    fps = [f for f in board.GetFootprints() if not f.GetReference().startswith("H")]
    out = []
    for i, a in enumerate(fps):
        for b in fps[i + 1:]:
            same = a.IsFlipped() == b.IsFlipped()
            if not same and a.GetReference() not in IC_REFS and b.GetReference() not in IC_REFS:
                continue
            A, B = box(a), box(b)
            ox = min(A[2], B[2]) - max(A[0], B[0])
            oy = min(A[3], B[3]) - max(A[1], B[1])
            if ox > 0 and oy > 0:
                out.append(f"{a.GetReference()}/{b.GetReference()} {'same side' if same else 'over IC'} "
                           f"{ox:.2f}x{oy:.2f} mm")
    return out


# Net classes live in the .kicad_pro. 0.15/0.15 escapes the 0.5 mm-pitch QFN/LQFP pins; gate drive
# gets 0.25 mm for the ~1 A gate-charge peaks.
NET_CLASSES = {"Default": (0.15, 0.15), "Gate": (0.25, 0.15)}
NET_CLASS_PATTERNS = [("Gate", p) for p in ("GH*", "GL*", "*U5-GH*", "*U5-GL*", "Net-(U?-G)")]


def write_net_classes(pro: Path) -> None:
    import copy
    d = json.loads(pro.read_text(encoding="utf-8")) if pro.exists() else {}
    ns = d.setdefault("net_settings", {})
    classes = ns.get("classes") or [{"name": "Default"}]
    base = next((c for c in classes if c.get("name") == "Default"), classes[0])
    base.setdefault("via_diameter", 0.6); base.setdefault("via_drill", 0.3)
    out = []
    for name, (tw, cl) in NET_CLASSES.items():
        c = copy.deepcopy(base); c.update(name=name, track_width=tw, clearance=cl)
        if name != "Default":
            c["priority"] = 0
        out.append(c)
    ns["classes"] = out
    ns["netclass_patterns"] = [{"netclass": c, "pattern": p} for c, p in NET_CLASS_PATTERNS]
    pro.write_text(json.dumps(d, indent=2), encoding="utf-8")


# Top-layer track keep-out: the power pours on F.Cu must stay whole. Signals live on B.Cu; these windows
# are the only places a track may run on top (short escapes from top-side signal pads to a via).
WINDOWS: list = []
F_WINDOWS = [(0.5, 29.6, 17.8, 57.5),     # left GND block: wire pads, J1, crystal
             (18.0, 38.8, 27.8, 52.4),     # under the MCU: a third routing layer for its pins (VM stays a strip above)
             (43.6, 40.6, 47.6, 48.4)]     # the 5V pad on the right edge
# (ground current in these areas runs in the solid In1 plane, so signals cutting the top pour don't choke it)


def gate_windows(board) -> list[tuple[float, float, float, float]]:
    out = []
    for fet, _, _ in GATE_PARTS:
        g = next(p for p in board.FindFootprintByReference(fet).Pads() if p.GetNumber() == "4")
        gx, gy = pcbnew.ToMM(g.GetPosition().x) - OX, pcbnew.ToMM(g.GetPosition().y) - OY
        if gy < FET_Y:   # high-side gate on the phase edge: escape upward into the phase area
            out.append((gx - 1.7, gy - 3.0, gx + 1.7, gy + 0.7))
        else:            # low-side gate on the GND edge: escape downward
            out.append((gx - 1.7, gy - 0.7, gx + 1.7, gy + 3.0))
    return out


def add_keepouts(board, project: Path) -> None:
    windows = F_WINDOWS + gate_windows(board)
    z = pcbnew.ZONE(board)
    z.SetIsRuleArea(True)
    z.SetDoNotAllowTracks(True)
    z.SetDoNotAllowVias(False)
    z.SetDoNotAllowPads(False)
    z.SetDoNotAllowCopperPour(False)
    z.SetDoNotAllowFootprints(False)
    z.SetLayer(pcbnew.F_Cu)
    ol = z.Outline()
    ol.NewOutline()
    for x, y in _poly(rect(0.2, 0.2, W - 0.2, H - 0.2)):
        ol.Append(MM(x), MM(y))
    ol.Outline(0).SetClosed(True)
    for x0, y0, x1, y1 in windows:
        ol.NewHole()
        for x, y in _poly(rect(x0, y0, x1, y1)):
            ol.Append(MM(x), MM(y), 0, ol.HoleCount(0) - 1)
        ol.Hole(0, ol.HoleCount(0) - 1).SetClosed(True)
    z.SetZoneName("F.Cu signal keep-out")
    board.Add(z)
    # In2: the VM plane under the power half stays whole too
    k = pcbnew.ZONE(board)
    k.SetIsRuleArea(True); k.SetDoNotAllowTracks(True); k.SetDoNotAllowVias(False)
    k.SetDoNotAllowPads(False); k.SetDoNotAllowCopperPour(False); k.SetDoNotAllowFootprints(False)
    k.SetLayer(pcbnew.In2_Cu)
    ko = k.Outline(); ko.NewOutline()
    for x, y in _poly(VM_PLANE):
        ko.Append(MM(x), MM(y))
    ko.Outline(0).SetClosed(True)
    k.SetZoneName("In2 VM plane keep-out")
    board.Add(k)
    global WINDOWS
    WINDOWS = windows
    (project / ".copilot").mkdir(exist_ok=True)
    (project / ".copilot" / "f_windows.json").write_text(json.dumps(
        [[OX + a, OY + b, OX + c, OY + d] for a, b, c, d in windows]))
    print("F.Cu keep-out with", len(windows), "windows")


def netlist(project: Path, name: str) -> ET.Element:
    out = project / ".copilot" / "v2_netlist.xml"
    out.parent.mkdir(exist_ok=True)
    cli = KICAD_CLI
    subprocess.run([cli, "sch", "export", "netlist", "--format", "kicadxml", "-o", str(out),
                    str(project / f"{name}.kicad_sch")], check=True, capture_output=True)
    return ET.parse(out).getroot()


# KiCad library model -> the team's model for the same part (github.com/Autonomous-Flight-Team/ESC_PCB)
TEAM_MODELS = {"CP_Elec_10x10.5.step": "Panasonic_Elec_10x10.2_SizeG.stp",
               "C_1210_3225Metric.step": "Murata_GRM32_1210.step"}
# Some STEP files are drawn with another "up" axis: rotation that stands them upright on the board (checked in render)
MODEL_ROTATION = {"Panasonic_Elec_10x10.2_SizeG.stp": "-90 0 0"}


def relink_3d_models(board_file: Path, project: Path) -> None:
    """Point any 3D model whose file doesn't exist (e.g. a KiCad library model this install lacks) at the copy in
    <project>/3dmodels/ when there is one. Text edit: KiCad 9's FOOTPRINT.Models() hands back copies."""
    import re
    env = {"KIPRJMOD": str(project), "KICAD9_3DMODEL_DIR": str(KICAD_FP.parent / "3dmodels")}
    raw = board_file.read_bytes().decode("utf-8")        # bytes: keep KiCad's line endings exactly
    fixed = []

    def swap(m):
        path = m.group(1)
        real = re.sub(r"\$\{(\w+)\}", lambda v: env.get(v.group(1), v.group(0)), path)
        name = Path(path).name
        if name in TEAM_MODELS and (project / "3dmodels" / TEAM_MODELS[name]).exists():
            fixed.append(TEAM_MODELS[name])          # the team's own model for this part (from the ESC_PCB repo)
            return f'(model "${{KIPRJMOD}}/3dmodels/{TEAM_MODELS[name]}"'
        local = project / "3dmodels" / name
        if not Path(real).exists() and local.exists():
            fixed.append(name)
            return f'(model "${{KIPRJMOD}}/3dmodels/{name}"'
        return m.group(0)

    raw = re.sub(r'\(model "([^"]+)"', swap, raw)
    for name, rot in MODEL_ROTATION.items():            # set the model's (rotate (xyz ...)) for those files
        raw = re.sub(r'(\(model "[^"]*' + re.escape(name) + r'"(?:(?!\(model ).)*?\(rotate\s*\(xyz )[^)]*\)',
                     lambda m: m.group(1) + rot + ")", raw, flags=re.S)
    board_file.write_bytes(raw.encode("utf-8"))
    if fixed:
        print("3D models relinked to the project copy:", sorted(set(fixed)))


def lib_path(project: Path, lib: str) -> str:
    local = project / "libs" / f"{lib}.pretty"
    return str(local if local.exists() else KICAD_FP / f"{lib}.pretty")


def pt(x: float, y: float) -> pcbnew.VECTOR2I:
    return pcbnew.VECTOR2I(MM(OX + x), MM(OY + y))


def build(project: Path) -> Path:
    name = "ESC PCB V2"
    root = netlist(project, name)
    board = pcbnew.CreateEmptyBoard()
    board.SetCopperLayerCount(4)
    board.SetLayerType(pcbnew.In1_Cu, pcbnew.LT_POWER)
    board.SetLayerType(pcbnew.In2_Cu, pcbnew.LT_MIXED)   # VM plane under the power half, signals in the logic half
    board.SetLayerName(pcbnew.In1_Cu, "In1.Cu")
    board.SetLayerName(pcbnew.In2_Cu, "In2.Cu")
    ds = board.GetDesignSettings()
    ds.SetBoardThickness(MM(1.6))
    ds.m_TrackMinWidth = MM(0.09)     # JLCPCB 4-layer minimum; autorouter neck-downs at fine-pitch pins
    ds.m_MinClearance = MM(0.1)       # (net classes still ask for 0.15 everywhere else)
    ds.m_ViasMinSize = MM(0.56)
    ds.m_MinThroughDrill = MM(0.3)
    ds.m_ViasMinAnnularWidth = MM(0.13)
    ds.m_CopperEdgeClearance = MM(0.3)
    ds.m_HoleToHoleMin = MM(0.5)

    # nets
    nets: dict[str, pcbnew.NETINFO_ITEM] = {}
    pin_net: dict[tuple[str, str], str] = {}
    for n in root.iterfind("nets/net"):
        ni = pcbnew.NETINFO_ITEM(board, n.get("name"))
        board.Add(ni)
        nets[n.get("name")] = ni
        for node in n.iterfind("node"):
            pin_net[(node.get("ref"), node.get("pin"))] = n.get("name")

    place = {r: (x, y, rot, side) for r, x, y, rot, side in PLACEMENT}
    missing = []
    for comp in root.iterfind("components/comp"):
        ref = comp.get("ref")
        fpid = comp.findtext("footprint", "")
        if not fpid:
            missing.append(f"{ref}: no footprint"); continue
        lib, fpname = fpid.split(":", 1)
        fp = pcbnew.FootprintLoad(lib_path(project, lib), fpname)
        if fp is None:
            missing.append(f"{ref}: cannot load {fpid}"); continue
        fp.SetFPID(pcbnew.LIB_ID(lib, fpname))
        fp.SetReference(ref)
        fp.SetValue(comp.findtext("value", ""))
        sheet = comp.find("sheetpath").get("tstamps")
        fp.SetPath(pcbnew.KIID_PATH(sheet + comp.findtext("tstamps")))
        for f in comp.iterfind("fields/field"):
            if f.get("name") in ("LCSC", "MPN", "Manufacturer"):
                fp.SetField(f.get("name"), f.text or "")
        board.Add(fp)
        for pad in fp.Pads():
            net = pin_net.get((ref, pad.GetNumber()))
            if net:
                pad.SetNet(nets[net])
        if ref not in place:
            if not any(ref in g for g in GATE_PARTS):
                missing.append(f"{ref}: no placement")
            continue
        x, y, rot, side = place[ref]
        fp.SetPosition(pt(x, y))
        if side == "B":
            fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        fp.SetOrientationDegrees(rot)
    place_gate_parts(board)
    if missing:
        print("PLACEMENT PROBLEMS:", *missing, sep="\n  ")

    # board outline
    edge = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_RECT)
    edge.SetStart(pt(0, 0)); edge.SetEnd(pt(W, H))
    edge.SetLayer(pcbnew.Edge_Cuts); edge.SetWidth(MM(0.1))
    board.Add(edge)

    tidy_silkscreen(board)
    add_zones(board, nets)
    add_keepouts(board, project)
    add_power_vias(board, nets)
    strap_tab_pins(board)
    fanout_signal_pads(board, WINDOWS)     # first: top-side signal pads only have room inside their windows
    fanout_plane_pads(board, nets)
    problems = check_overlaps(board)
    print("overlaps:", problems or "none")

    out = project / f"{name}.kicad_pcb"
    board.Save(str(out))
    # KiCad's zone filler crashes on a board built in memory; fill a freshly loaded copy instead.
    loaded = pcbnew.LoadBoard(str(out))
    pcbnew.ZONE_FILLER(loaded).Fill(loaded.Zones())
    loaded.Save(str(out))
    write_net_classes(project / f"{name}.kicad_pro")   # after saving: Save() rewrites the project file
    relink_3d_models(out, project)
    print("saved", out, "footprints:", len(board.GetFootprints()), "nets:", len(nets))
    return out


if __name__ == "__main__":
    build(Path(sys.argv[1]).resolve())

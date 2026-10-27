"""Finish a board's routing: rip up given nets, then maze-route exactly what KiCad's ratsnest says is missing.

Run with the copilot venv (it drives KiCad's Python and kicad-cli as subprocesses):
    .venv/Scripts/python.exe tools/finish_route.py <board> [NET ...] [--all] [--narrow]
        [--rip-ref U5,...] [--first NET,NET,...]
        [--layers In2.Cu,B.Cu] [--width W] [--clearance C] [--grid 0.05]

1. remove every unlocked track/via of the listed nets (locked copper - fan-outs, via fields - stays).
   --narrow also rips every net with a track thinner than its net class (autorouter neck-downs);
   --rip-ref rips every signal net of the given parts (for a fine-pitch IC the autorouter boxed in).
   --first routes those nets before the rest, in that order.
2. run DRC and read the unconnected pairs (KiCad's own ratsnest). --all takes every signal net;
   pour-only nets (GND, /VM) are left to the zones and stitching.
3. resolve each end to something on a routing layer: the pad itself, an existing via or track end,
   or - for a pad / pour that only exists on another layer - the nearest via of that net
4. route each pair with tools/maze_route.py (net-class widths, rule areas, edge keep-out)
5. repeat for anything still open, then report
"""

from __future__ import annotations

import sys as _sys, os as _os; _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _quiet  # noqa: E402,F401  - no console windows for child processes on Windows

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
from _env import KPY, KICAD_CLI, KICAD_SHARE  # noqa: E402  - KiCad paths (Windows default / Docker override)
sys.path.insert(0, str(ROOT / "tools"))
import drc_summary  # noqa: E402

PLANE_NETS = {"GND", "/VM", "/BATN"}   # pour-only nets

# Runs under KiCad's Python: rip nets, optionally find necked-down nets.
RIP = r"""
import json, sys, pcbnew, wx
wx.DisableAsserts()
board, nets, narrow = sys.argv[1], set(json.loads(sys.argv[2])), sys.argv[3] == "1"
b = pcbnew.LoadBoard(board)
for ref in json.loads(sys.argv[5]):        # --rip-ref: every signal net on that part
    for p in b.FindFootprintByReference(ref).Pads():
        n = p.GetNetname()
        if n and n not in ("GND", "/VM") and not n.startswith("unconnected-"):
            nets.add(n)
sys.path.insert(0, sys.argv[4])
from maze_route import net_class_rules
rules = net_class_rules(board)
if narrow:
    for t in list(b.GetTracks()):
        if t.GetClass() == "PCB_TRACK" and not t.IsLocked() and t.GetNetname() not in ("GND", "/VM"):
            if pcbnew.ToMM(t.GetWidth()) < rules(t.GetNetname())[0] - 0.001:
                nets.add(t.GetNetname())
gone = [t for t in list(b.GetTracks()) if t.GetNetname() in nets and not t.IsLocked()]
for t in gone:
    b.Remove(t)
pcbnew.ZONE_FILLER(b).Fill(b.Zones())
b.Save(board)
print(json.dumps({"ripped": len(gone), "nets": sorted(nets)}))
"""

# Runs under KiCad's Python: turn DRC unconnected pairs into maze jobs.
RESOLVE = r"""
import json, re, sys, pcbnew, wx
wx.DisableAsserts()
board, layers, pairs = sys.argv[1], sys.argv[2].split(","), json.loads(open(sys.argv[3]).read())
ids = {"F.Cu": pcbnew.F_Cu, "In1.Cu": pcbnew.In1_Cu, "In2.Cu": pcbnew.In2_Cu, "B.Cu": pcbnew.B_Cu}
L = [ids[n] for n in layers]
b = pcbnew.LoadBoard(board)
mm, P = pcbnew.ToMM, lambda x, y: pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))

def via_at(net, x, y):
    for t in b.GetTracks():
        if t.GetClass() == "PCB_VIA" and t.GetNetname() == net:
            q = t.GetPosition()
            if (mm(q.x) - x) ** 2 + (mm(q.y) - y) ** 2 < 0.05 ** 2:
                return True
    return False

def nearest_via(net, x, y, exclude=None):
    best = None
    for t in b.GetTracks():
        if t.GetClass() == "PCB_VIA" and t.GetNetname() == net:
            q = t.GetPosition(); d = (mm(q.x) - x) ** 2 + (mm(q.y) - y) ** 2
            if best is None or d < best[0]:
                best = (d, f"{mm(q.x):.4f},{mm(q.y):.4f}")
    return best and best[1]

def resolve(item, other_xy):
    d, (x, y) = item["description"], (item["pos"]["x"], item["pos"]["y"])
    net = re.search(r"\[([^\]]+)\]", d).group(1)
    m = re.match(r"Pad (\S+) \[[^\]]+\] of (\S+) on", d)
    if m:
        fp = b.FindFootprintByReference(m.group(2))
        pad = next(p for p in fp.Pads() if p.GetNumber() == m.group(1))
        if any(pad.IsOnLayer(l) for l in L):
            return net, f"{m.group(2)}.{m.group(1)}"
        q = pad.GetPosition()
        return net, nearest_via(net, mm(q.x), mm(q.y))
    if d.startswith("Via"):
        return net, f"{x:.4f},{y:.4f}"
    if d.startswith("Track"):
        ox, oy = other_xy
        best = None
        if not any(t.GetClass() == "PCB_TRACK" and t.GetNetname() == net and t.GetLayer() in L
                   and t.HitTest(P(x, y), pcbnew.FromMM(0.01)) for t in b.GetTracks()):
            return net, nearest_via(net, x, y)      # the loose track is on a non-routing layer
        for t in b.GetTracks():
            if t.GetClass() == "PCB_TRACK" and t.GetNetname() == net and t.GetLayer() in L:
                for e in (t.GetStart(), t.GetEnd()):
                    dd = (mm(e.x) - ox) ** 2 + (mm(e.y) - oy) ** 2
                    near_item = (mm(t.GetStart().x) - x) ** 2 + (mm(t.GetStart().y) - y) ** 2 < 1e-4 or \
                                (mm(t.GetEnd().x) - x) ** 2 + (mm(t.GetEnd().y) - y) ** 2 < 1e-4 or \
                                t.HitTest(P(x, y), pcbnew.FromMM(0.01))
                    if near_item and (best is None or dd < best[0]):
                        best = (dd, f"{mm(e.x):.4f},{mm(e.y):.4f}")
        return net, (best and best[1]) or nearest_via(net, x, y)
    # a pour: the closest via of that net to the other end
    return net, nearest_via(net, *other_xy)

jobs = []
for a, c in pairs:
    na, ea = resolve(a, (c["pos"]["x"], c["pos"]["y"]))
    nc, ec = resolve(c, (a["pos"]["x"], a["pos"]["y"]))
    if na in ("GND", "/VM"):
        # stranded plane-net pad: connect it to the nearest via of the net (that via reaches the plane)
        pad_item = a if a["description"].startswith("Pad") else c
        pn, pe = resolve(pad_item, (pad_item["pos"]["x"], pad_item["pos"]["y"]))
        best = None
        px, py = pad_item["pos"]["x"], pad_item["pos"]["y"]
        for t in b.GetTracks():
            if t.GetClass() == "PCB_VIA" and t.GetNetname() == na:
                q = t.GetPosition(); d = (mm(q.x) - px) ** 2 + (mm(q.y) - py) ** 2
                if d > 0.36 and (best is None or d < best[0]):
                    best = (d, f"{mm(q.x):.4f},{mm(q.y):.4f}")
        if pe and best:
            jobs.append((best[0] ** 0.5, f"{na}:{pe}:{best[1]}"))
        continue
    if ea and ec and ea != ec:
        length = abs(a["pos"]["x"] - c["pos"]["x"]) + abs(a["pos"]["y"] - c["pos"]["y"])
        jobs.append((length, f"{na}:{ea}:{ec}"))
jobs.sort()                                  # short connections first: they have the fewest ways around
print(json.dumps(list(dict.fromkeys(j for _, j in jobs))))
"""


def kpy(code: str, *args: str) -> str:
    out = subprocess.run([KPY, "-c", code, *args], capture_output=True, text=True)
    lines = [l for l in out.stdout.splitlines() if l.startswith(("{", "["))]
    if not lines:
        raise SystemExit(out.stdout[-800:] + out.stderr[-1500:])
    return lines[-1]


def missing(board: str, nets: set[str] | None, layers: str) -> list[str]:
    r = drc_summary.run(board, parity=False)
    pairs = []
    for v in r.get("unconnected_items", []):
        items = v["items"]
        if len(items) != 2:
            continue
        net = items[0]["description"].split("[", 1)[-1].split("]", 1)[0]
        if net in PLANE_NETS:
            # pour-only nets: only a stranded pad needs a track (to the nearest via of its net); zone-to-zone
            # gaps are the stitching step's job
            if nets is not None or not any(i["description"].startswith("Pad") for i in items):
                continue
        elif nets is not None and net not in nets:
            continue
        pairs.append(items)
    if not pairs:
        return []
    f = Path(tempfile.mkdtemp()) / "pairs.json"
    f.write_text(json.dumps(pairs), encoding="utf-8")
    return json.loads(kpy(RESOLVE, board, layers, str(f)))


def main() -> None:
    args = sys.argv[1:]
    opts, layers = [], "F.Cu,B.Cu"
    for k in ("--width", "--grid", "--clearance", "--layers"):
        if k in args:
            i = args.index(k)
            if k == "--layers":
                layers = args[i + 1]
            opts += [k, args[i + 1]]; del args[i:i + 2]
    rip_refs, first = [], []
    for k in ("--rip-ref", "--first"):
        if k in args:
            i = args.index(k)
            (rip_refs if k == "--rip-ref" else first).extend(args[i + 1].split(",")); del args[i:i + 2]
    rounds = 1
    if "--rounds" in args:
        i = args.index("--rounds"); rounds = int(args[i + 1]); del args[i:i + 2]
    every = "--all" in args
    narrow = "--narrow" in args
    args = [a for a in args if a not in ("--all", "--narrow")]
    board, nets = args[0], args[1:]

    routed_here: set[str] = set()      # --all: nets this step routes (ripped again in later rounds)

    def rip_now():
        r = json.loads(kpy(RIP, board, json.dumps(sorted(set(nets) | routed_here)), "1" if narrow else "0",
                           str(ROOT / "tools"), json.dumps(rip_refs)))
        print("ripped", r["ripped"], "items on", len(r["nets"]), "nets", flush=True)
        return r

    r = rip_now()
    scope = None if every else set(r["nets"])
    order = list(first)
    best, best_file = None, Path(tempfile.mkdtemp()) / "best.kicad_pcb"
    for rnd in range(rounds):
        if rnd:
            # rip-up and reroute: put the connections that failed first and route this set again from scratch
            print(f"round {rnd + 1}/{rounds}: rerouting with the {len(best_left)} failed nets first", flush=True)
            rip_now()
        for attempt in range(2):
            jobs = missing(board, scope, layers)
            if rnd == 0:
                routed_here.update(j.split(":", 1)[0] for j in jobs)
            rank = {n: i for i, n in enumerate(order)}          # --first order, failed nets moved up front
            jobs.sort(key=lambda j: rank.get(j.split(":", 1)[0], len(rank)))
            print(f"pass {attempt + 1}/2: {len(jobs)} connections to route", flush=True)
            if not jobs:
                break
            # pass 1 may rip up blockers; pass 2 puts the ripped nets back (no further ripping)
            rip = ["--ripup"] if attempt == 0 else []
            proc = subprocess.Popen([KPY, "-u", str(ROOT / "tools" / "maze_route.py"), board, *jobs, *opts, *rip],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in proc.stdout:      # live: one line per connection as the router finishes it
                line = line.rstrip()
                if line.startswith("[") or line.startswith("routed ") or "Error" in line or "Traceback" in line:
                    print("   ", line, flush=True)
            proc.wait()
        left = missing(board, scope, layers)
        print(f"round {rnd + 1}/{rounds}: {len(left)} unrouted", flush=True)
        if best is None or len(left) < best:
            best, best_left = len(left), left
            best_file.write_bytes(Path(board).read_bytes())
        if not left:
            break
        failed = list(dict.fromkeys(j.split(":", 1)[0] for j in left))
        order = failed + [n for n in order if n not in failed]
    if best is not None and best_file.exists():
        Path(board).write_bytes(best_file.read_bytes())       # keep the best round
    print("still unrouted:", best, best_left or "", flush=True)


if __name__ == "__main__":
    main()

"""Build and route the V2 ESC board end to end, then report DRC. One command, no hand steps.

Run with the copilot venv:
    .venv/Scripts/python.exe tools/make_esc_v2.py [workspace/esc_v2] [--passes 100] [--skip-build] [--no-publish]

Watch it from any other console with  autopcb_logs  (repo root): a live progress bar, every finished
step with its result, and the router's latest output. Everything also goes to logs/autopcb.log.

1. build_esc_v2.py   placement, stackup, pours, via fields, fan-outs (top-side pads get stubs + vias)
2. finish_route.py   the gate driver's nets first (maze router, escape lanes) on the empty board
3. finish_route.py   everything else with the maze router: shortest first, rip-up-and-reroute rounds
                     (--freerouting adds a Freerouting pass before it; slower and weaker on this board)
5. stitch_islands.py tie stray GND pour islands to the GND plane
6. prune_tracks.py   remove dangling stubs / vias
7. silk_fix.py       move reference text off pads and other silk
8. drc_summary.py    the verdict (with schematic parity)
"""

from __future__ import annotations

import sys as _sys, os as _os; _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _quiet  # noqa: E402,F401  - no console windows for child processes on Windows

import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
from _env import KPY, KICAD_CLI, KICAD_SHARE  # noqa: E402  - KiCad paths (Windows default / Docker override)
VPY = sys.executable
T = ROOT / "tools"
LOG_DIR = ROOT / "logs"
LOG = LOG_DIR / "autopcb.log"
STATUS = LOG_DIR / "autopcb_status.json"
NOISE = ("image handler", "memory leak")

# gates + phase sense (up to the power stage), then the six PWM lines to the MCU (they untangle as two
# planar groups, INH on one layer and INL on the other), then the charge pump and status pins, which can detour
# nets that cross the whole logic area: routed before the gate driver's bundles split the board in two
LONG_NETS = ["/SIG_IN", "BEMF_C", "BEMF_B", "BEMF_A", "+5V", "Net-(J1-Pin_2)", "Net-(J1-Pin_3)", "/ISENSE"]
U5_ORDER = ",".join(["Net-(U5-GHA)", "/Phase_A", "Net-(U5-GLA)", "GLB", "/Phase_B", "GHB", "GHC", "/Phase_C", "GLC",
                     "INHC", "INHB", "INHA", "INLC", "INLB", "INLA",
                     "Net-(U5-VCP)", "Net-(U5-CPH)", "Net-(U5-CPL)", "Net-(U5-DVDD)", "/VDS_LVL", "nFAULT", "ENABLE"])


def bar(frac: float, width: int = 30) -> str:
    n = int(round(max(0.0, min(1.0, frac)) * width))
    return "[" + "#" * n + "-" * (width - n) + f"] {frac * 100:5.1f}%"


class Run:
    """Progress state shared with autopcb_logs through logs/autopcb_status.json, plus the plain log."""

    def __init__(self, board: str, steps: list[tuple[str, float]]):
        LOG_DIR.mkdir(exist_ok=True)
        self.t0 = time.time()
        self.state = {"board": board, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "state": "running",
                      "steps": [{"name": n, "weight": w, "status": "pending", "summary": "", "sub": None,
                                 "seconds": None} for n, w in steps],
                      "current": None, "tail": [], "overall": 0.0, "elapsed": 0}
        self.log = LOG.open("a", encoding="utf-8")
        self.write_log(f"\n===== run started {self.state['started']}  board: {board}")
        self.save()

    def write_log(self, text: str) -> None:
        self.log.write(text + "\n"); self.log.flush()

    def overall(self) -> float:
        tot = sum(s["weight"] for s in self.state["steps"])
        done = 0.0
        for s in self.state["steps"]:
            if s["status"] in ("done", "failed"):
                done += s["weight"]
            elif s["status"] == "running" and s["sub"]:
                done += s["weight"] * s["sub"][0] / max(1, s["sub"][1])
        return done / tot

    def save(self) -> None:
        """Publish progress for autopcb_logs. Never fatal: on Windows the viewer may hold the file open at the
        moment we replace it (PermissionError), so retry briefly and otherwise skip this update."""
        self.state["overall"] = self.overall()
        self.state["elapsed"] = int(time.time() - self.t0)
        tmp = STATUS.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        except OSError:
            return
        for _ in range(20):
            try:
                tmp.replace(STATUS)
                return
            except OSError:
                time.sleep(0.05)

    def line(self, idx: int, text: str) -> None:
        s = self.state["steps"][idx]
        self.write_log(f"  {text}")
        self.state["tail"] = (self.state["tail"] + [text])[-40:]
        m = re.match(r"\s*\[(\d+)/(\d+)\]", text)                      # maze router: [k/N] per connection
        if m:
            s["sub"] = [int(m.group(1)), int(m.group(2))]
        m = re.search(r"pass (\d+)/(\d+)", text)                         # finish_route passes
        if m and not s["sub"]:
            s["sub"] = [int(m.group(1)) - 1, int(m.group(2))]
        m = re.search(r"[Pp]ass #?(\d+)", text)                          # Freerouting passes
        if m and "FR" in text and s.get("max_passes"):
            s["sub"] = [min(int(m.group(1)), s["max_passes"]), s["max_passes"]]
        if re.search(r"routed \d+/\d+|still unrouted|stage completed|unconnected:|violations:|overlaps:|"
                     r"fanned out|stitching vias|removed|references:|routed \+ zones", text):
            s["summary"] = text.strip()[:160]
        self.save()

    def step(self, idx: int, cmd: list[str], max_passes: int | None = None) -> None:
        s = self.state["steps"][idx]
        s["status"], s["sub"], s["max_passes"] = "running", None, max_passes
        self.state["current"] = s["name"]
        t0 = time.time()
        head = f"{bar(self.overall())}  step {idx + 1}/{len(self.state['steps'])}: {s['name']}"
        print(head, flush=True); self.write_log(head); self.save()
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                bufsize=1, encoding="utf-8", errors="replace")
        for raw in proc.stdout:
            text = raw.rstrip()
            if not text or any(n in text for n in NOISE):
                continue
            print("    " + text[:200], flush=True)
            self.line(idx, text)
        proc.wait()
        s["seconds"] = int(time.time() - t0)
        if proc.returncode:
            s["status"] = "failed"; self.state["state"] = "failed"; self.save()
            msg = f"FAILED: {s['name']} (exit {proc.returncode}) after {s['seconds']} s"
            print(msg); self.write_log(msg)
            raise SystemExit(msg)
        s["status"] = "done"; s["sub"] = None
        done = f"{bar(self.overall())}  done {idx + 1}/{len(self.state['steps'])}: {s['name']} " \
               f"({s['seconds']} s)  {s['summary']}"
        print(done, flush=True); self.write_log(done); self.save()

    def finish(self) -> None:
        self.state["state"], self.state["current"] = "done", None
        self.save()
        msg = f"===== run finished in {int(time.time() - self.t0)} s"
        print(msg); self.write_log(msg)


def main() -> None:
    args = sys.argv[1:]
    passes = "100"
    if "--passes" in args:
        i = args.index("--passes"); passes = args[i + 1]; del args[i:i + 2]
    skip_build = "--skip-build" in args
    use_freerouting = "--freerouting" in args
    publish = "--no-publish" not in args
    args = [a for a in args if a not in ("--skip-build", "--freerouting", "--no-publish")]
    project = Path(args[0] if args else "workspace/esc_v2").resolve()
    board = str(project / "ESC PCB V2.kicad_pcb")
    empty = Path(tempfile.mkdtemp()) / "none.json"
    empty.write_text(json.dumps({"nets": []}))

    # (name, rough share of the run time - drives the overall bar, command, Freerouting pass cap)
    plan = []
    if not skip_build:
        plan.append(("build board", 1, [KPY, "-u", str(T / "build_esc_v2.py"), str(project)], None))
    plan += [
        # The gate driver's 0.5 mm-pitch pins get boxed in if the autorouter goes first (it hugs pin rows), so its
        # ~22 nets are routed first on the empty board, with escape lanes, power-stage side first...
        ("gate driver + long nets first (maze)", 25, [VPY, "-u", str(T / "finish_route.py"), board, *LONG_NETS,
                                               "--rip-ref", "U5", "--first", ",".join(LONG_NETS) + "," + U5_ORDER, "--rounds", "4", "--layers", "F.Cu,In2.Cu,B.Cu",
                                               "--grid", "0.05"], None),
        # ...then everything else, shortest connections first, with rip-up-and-reroute rounds for any that fail.
        # (Freerouting is optional, --freerouting: on this board it leaves 80-100 connections and takes 30+ min.)
        ("route everything else (maze)", 45, [VPY, "-u", str(T / "finish_route.py"), board, "--all", "--rounds", "6", "--first", "+5V,+3.3V,+3.3VA",
                                              "--layers", "F.Cu,In2.Cu,B.Cu", "--grid", "0.05"], None),
        # tie pour islands to the plane (their pads may push a route aside)...
        ("stitch GND islands", 1, [KPY, "-u", str(T / "stitch_islands.py"), board, "GND", "--min-area", "0.3"], None),
        # ...then a polish pass on whatever is left: those connections go first and may push other routes aside
        ("polish leftovers (maze, rip-up)", 15, [VPY, "-u", str(T / "finish_route.py"), board, "--all", "--rounds",
                                                 "8", "--layers", "F.Cu,In2.Cu,B.Cu", "--grid", "0.05"], None),
        # ...then close out: tie islands / re-polish, repeated until DRC shows nothing unconnected
        ("close out (islands + leftovers)", 6, [VPY, "-u", str(T / "close_out.py"), board, "--rounds", "3"], None),
        ("prune dangling copper", 1, [KPY, "-u", str(T / "prune_tracks.py"), board, str(empty)], None),
        ("silkscreen", 1, [KPY, "-u", str(T / "silk_fix.py"), board], None),
        ("DRC", 2, [VPY, "-u", str(T / "drc_summary.py"), board, "4"], None),
        # what DRC can't see: switching loops, charge pump, gate drive, phase crossings, via-in-pad, ground return
        ("design checks", 1, [KPY, "-u", str(T / "esc_checks.py"), board], None),
        ("review images (per layer + 3D)", 2, [KPY, "-u", str(T / "render_review.py"), board], None),
    ]
    if publish:      # every finished loop goes to the team repo as a commit on the esc-v2 branch
        plan.append(("publish to GitHub (esc-v2)", 1, [VPY, "-u", str(T / "publish_branch.py"), str(project)], None))
    if use_freerouting:
        plan.insert(len(plan) - 5, ("autoroute (Freerouting)", 25, [KPY, "-u", str(T / "route_board.py"), board, passes,
                                    "--keep", "--no-planes", "--keep-plane-nets", "--no-keepouts",
                                    "--layers=In2.Cu,B.Cu", "--obstacle-planes"], int(passes)))
    run = Run(board, [(n, w) for n, w, _, _ in plan])
    for i, (_, _, cmd, mp) in enumerate(plan):
        run.step(i, cmd, mp)
    run.finish()


if __name__ == "__main__":
    main()

"""Close out a routed board: alternate island-tying and leftover polishing until KiCad's DRC reports nothing
unconnected (or the round limit is hit). Each fix can disturb the other - tying a ground island may push a
signal aside, re-routing that signal may strand another pad - so loop until both are clean.

Run with the copilot venv:
    .venv/Scripts/python.exe tools/close_out.py <board> [--rounds 3] [--layers F.Cu,In2.Cu,B.Cu]
"""

from __future__ import annotations

import sys as _sys, os as _os; _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _quiet  # noqa: E402,F401  - no console windows for child processes on Windows

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
from _env import KPY, KICAD_CLI, KICAD_SHARE  # noqa: E402  - KiCad paths (Windows default / Docker override)
sys.path.insert(0, str(ROOT / "tools"))
import drc_summary  # noqa: E402

NOISE = ("image handler", "memory leak")


def run(cmd: list[str]) -> None:
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in proc.stdout:
        line = line.rstrip()
        if line and not any(n in line for n in NOISE):
            print("   ", line, flush=True)
    proc.wait()


def main() -> None:
    args = sys.argv[1:]
    rounds, layers = 3, "F.Cu,In2.Cu,B.Cu"
    if "--rounds" in args:
        i = args.index("--rounds"); rounds = int(args[i + 1]); del args[i:i + 2]
    if "--layers" in args:
        i = args.index("--layers"); layers = args[i + 1]; del args[i:i + 2]
    board = args[0]
    for r in range(rounds):
        left = len(drc_summary.run(board, parity=False).get("unconnected_items", []))
        print(f"close-out {r + 1}/{rounds}: {left} unconnected", flush=True)
        if not left:
            break
        run([KPY, "-u", str(ROOT / "tools" / "stitch_islands.py"), board, "GND", "--min-area", "0.3"])
        run([sys.executable, "-u", str(ROOT / "tools" / "finish_route.py"), board, "--all", "--rounds", "4",
             "--layers", layers, "--grid", "0.05"])
    left = len(drc_summary.run(board, parity=False).get("unconnected_items", []))
    print(f"close-out done: {left} unconnected", flush=True)


if __name__ == "__main__":
    main()

"""Run kicad-cli DRC on a board and print a compact summary.

Usage: python tools/drc_summary.py <board.kicad_pcb> [examples_per_type] [--no-parity]
"""

from __future__ import annotations

import sys as _sys, os as _os; _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _quiet  # noqa: E402,F401  - no console windows for child processes on Windows

import collections
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from _env import KPY, KICAD_CLI, KICAD_SHARE  # noqa: E402  - KiCad paths (Windows default / Docker override)
CLI = KICAD_CLI


def run(board: str, parity: bool = True) -> dict:
    out = Path(tempfile.mkdtemp()) / "drc.json"
    args = [CLI, "pcb", "drc", "--format", "json", "--units", "mm", "--severity-all", "-o", str(out)]
    if parity:
        args.append("--schematic-parity")
    subprocess.run(args + [board], capture_output=True, text=True)
    return json.loads(out.read_text(encoding="utf-8"))


def main() -> None:
    board = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 2
    d = run(board, "--no-parity" not in sys.argv)
    groups = {"violations": d.get("violations", []), "unconnected": d.get("unconnected_items", []),
              "parity": d.get("schematic_parity", [])}
    for name, items in groups.items():
        by_type = collections.defaultdict(list)
        for v in items:
            by_type[(v["severity"], v["type"])].append(v)
        total = len(items)
        print(f"{name}: {total}")
        for (sev, typ), vs in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
            print(f"  {len(vs):4}x {sev:8} {typ}")
            for v in vs[:n]:
                print("        -", "; ".join(i["description"][:60] for i in v["items"])[:160])


if __name__ == "__main__":
    main()

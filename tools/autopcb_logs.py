"""Live console view of an autopcb run (tools/make_esc_v2.py): progress bar, every step with its result,
and the latest router output. Open it with  autopcb_logs  from the repo root, in any console.

    autopcb_logs            watch the current / last run, and any run after it (Ctrl+C to close)
    autopcb_logs --once     close when the current run ends
    autopcb_logs --log      print the full log of every run instead
    autopcb_status          (= autopcb_logs --snapshot) print where the run is right now, once
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATUS = ROOT / "logs" / "autopcb_status.json"
LOG = ROOT / "logs" / "autopcb.log"
ICON = {"done": "[x]", "running": "[>]", "pending": "[ ]", "failed": "[!]"}


def bar(frac: float, width: int) -> str:
    frac = max(0.0, min(1.0, frac))
    n = int(round(frac * width))
    return "[" + "#" * n + "-" * (width - n) + f"] {frac * 100:5.1f}%"


def fmt_time(s: int) -> str:
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def render(st: dict) -> str:
    cols = shutil.get_terminal_size((100, 30)).columns
    w = max(20, min(60, cols - 20))
    out = [f"autopcb  -  {Path(st['board']).name}",
           f"started {st['started']}   elapsed {fmt_time(st['elapsed'])}   state: {st['state'].upper()}",
           "",
           f"overall  {bar(st['overall'], w)}"]
    cur = next((s for s in st["steps"] if s["status"] == "running"), None)
    if cur:
        sub = cur.get("sub")
        detail = f"  {sub[0]}/{sub[1]}" if sub else ""
        frac = sub[0] / max(1, sub[1]) if sub else 0.0
        out.append(f"step     {bar(frac, w)}{detail}  {cur['name']}")
    out.append("")
    for i, s in enumerate(st["steps"], 1):
        secs = f"{s['seconds']:>5} s" if s.get("seconds") is not None else "       "
        line = f" {ICON.get(s['status'], '[?]')} {i}. {s['name']:<34} {secs}  {s.get('summary', '')}"
        out.append(line[:cols - 1])
    out += ["", "latest output:"]
    n = max(5, shutil.get_terminal_size((100, 30)).lines - len(out) - 2)
    out += ["  " + t[:cols - 3] for t in st.get("tail", [])[-n:]]
    return "\n".join(out)


def main() -> None:
    if "--snapshot" in sys.argv:        # print the current state once and return to the prompt
        try:
            st = json.loads(STATUS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            st = None
        print(render(st) if st else "no autopcb run yet")
        return
    if "--log" in sys.argv:
        print(LOG.read_text(encoding="utf-8") if LOG.exists() else "no runs logged yet")
        return
    follow = "--once" not in sys.argv
    os.system("")                      # turn on ANSI escape handling in the Windows console
    last = None
    while True:
        try:
            st = json.loads(STATUS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            st = None
        screen = render(st) if st else f"no autopcb run yet (waiting for {STATUS})"
        if st and st["state"] in ("done", "failed") and follow:
            screen += f"\n\nrun {st['state']}. Watching for the next run - Ctrl+C to close."
        if screen != last:
            sys.stdout.write("\x1b[2J\x1b[H" + screen + "\n")
            sys.stdout.flush()
            last = screen
        if st and st["state"] in ("done", "failed") and not follow:
            print(f"\nrun {st['state']}.  Full log: {LOG}")
            return
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass

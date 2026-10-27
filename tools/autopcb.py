"""Run the V2 board pipeline and show its live dashboard in the same window (one command does everything).

    autopcb_run [workspace\\esc_v2] [--passes 100] [--skip-build] [--docker] [--no-publish]

--docker runs the build + route + checks inside the autopcb-kicad container (nothing opens on the desktop);
the 3D views and the GitHub push then run on the host.

The pipeline (tools/make_esc_v2.py) runs as a child process; its full output goes to logs/autopcb.log and
logs/autopcb_console.txt. This window redraws the progress bar, the step checklist and the latest router
output every second, and stays on the final summary when the run ends. Ctrl+C stops the run.
"""

from __future__ import annotations

import sys as _sys, os as _os; _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _quiet  # noqa: E402,F401  - no console windows for child processes on Windows

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from autopcb_logs import LOG, STATUS, render  # noqa: E402

CONSOLE = ROOT / "logs" / "autopcb_console.txt"


def read_status() -> dict | None:
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


IMAGE = "autopcb-kicad"


def docker_ready() -> None:
    """Engine up (started headless - no Docker Desktop window) and the image built."""
    if subprocess.run(["docker", "info"], capture_output=True).returncode:
        print("starting the Docker engine (headless)...", flush=True)
        subprocess.run(["docker", "desktop", "start"], capture_output=True)
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
        print(f"building the {IMAGE} image (one time, a few minutes)...", flush=True)
        subprocess.run(["docker", "build", "-q", "-t", IMAGE, str(ROOT / "docker")], check=True, capture_output=True)


def main() -> None:
    os.system("")                                   # ANSI escapes on in the Windows console
    CONSOLE.parent.mkdir(exist_ok=True)
    args = sys.argv[1:]
    docker = "--docker" in args
    args = [a for a in args if a != "--docker"]
    if docker:
        docker_ready()
        # the whole build + route + checks run in Linux; the repo is mounted so logs/ and the board are shared
        cmd = ["docker", "run", "--rm", "-v", f"{ROOT}:/work", IMAGE, "python3", "-u", "tools/make_esc_v2.py",
               *args, "--no-publish"]
    else:
        cmd = [sys.executable, "-u", str(ROOT / "tools" / "make_esc_v2.py"), *args]
    before = (read_status() or {}).get("started")
    with CONSOLE.open("w", encoding="utf-8") as out:
        child = subprocess.Popen(cmd, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT)
        last = None
        try:
            while True:
                st = read_status()
                fresh = st and st.get("started") != before
                screen = render(st) if fresh else "starting the run..."
                if screen != last:
                    sys.stdout.write("\x1b[2J\x1b[H" + screen + "\n"); sys.stdout.flush()
                    last = screen
                if child.poll() is not None:
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            child.terminate()
            print("\nstopped by Ctrl+C")
            return
    st = read_status()
    if st and st.get("started") != before:
        sys.stdout.write("\x1b[2J\x1b[H" + render(st) + "\n")
    if child.returncode:
        tail = CONSOLE.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
        print(f"\nrun FAILED (exit {child.returncode}). Last output:\n  " + "\n  ".join(tail))
    elif docker and "--no-publish" not in args:
        # host side: the 3D views need KiCad's 3D library (not in the image); the push needs your git login
        from _env import KPY
        project = next((a for a in args if not a.startswith("--") and not a.isdigit()), "workspace/esc_v2")
        board = str(ROOT / project / "ESC PCB V2.kicad_pcb")
        for step in ([KPY, "-u", str(ROOT / "tools" / "esc_checks.py"), board],     # full checks incl. 3D models
                     [KPY, "-u", str(ROOT / "tools" / "render_review.py"), board, "--3d-only"],
                     [sys.executable, "-u", str(ROOT / "tools" / "publish_branch.py"), project]):
            r = subprocess.run(step, cwd=ROOT, capture_output=True, text=True)
            print("  " + (r.stdout.strip().splitlines() or [r.stderr.strip()[-300:]])[-1])
    print(f"\nfull log: {LOG}")


if __name__ == "__main__":
    main()

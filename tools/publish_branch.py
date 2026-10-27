"""Publish the V2 board to the team repo as a commit on a branch (default: esc-v2).

Run with the copilot venv:
    .venv/Scripts/python.exe tools/publish_branch.py [workspace/esc_v2] [--branch esc-v2] [--no-push]

Keeps a clone in workspace/_publish/ESC_PCB, copies the KiCad project (schematic, board, project, library
tables, project libraries, 3D models), the review folder (design checks + layer images) and the design notes
into "ESC PCB V2/", commits with the run's DRC / design-check numbers in the message, and pushes. Nothing is
committed when nothing changed.
"""

from __future__ import annotations

import sys as _sys, os as _os; _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _quiet  # noqa: E402,F401  - no console windows for child processes on Windows

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_URL = "https://github.com/Autonomous-Flight-Team/ESC_PCB"
sys.path.insert(0, str(ROOT / "tools"))
import drc_summary  # noqa: E402

FILES = ["ESC PCB V2.kicad_pro", "ESC PCB V2.kicad_sch", "ESC PCB V2.kicad_pcb", "fp-lib-table", "sym-lib-table"]
DIRS = ["libs", "3dmodels", "review"]
TRAILER = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"


def git(repo: Path, *args: str, check: bool = True) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                         env={**__import__("os").environ, "GIT_TERMINAL_PROMPT": "0"})
    if check and out.returncode:
        raise SystemExit(f"git {' '.join(args)} failed:\n{out.stdout}{out.stderr}")
    return out.stdout.strip()


def main() -> None:
    args = sys.argv[1:]
    branch = "esc-v2"
    if "--branch" in args:
        i = args.index("--branch"); branch = args[i + 1]; del args[i:i + 2]
    push = "--no-push" not in args
    args = [a for a in args if a != "--no-push"]
    project = (ROOT / (args[0] if args else "workspace/esc_v2")).resolve()
    repo = ROOT / "workspace" / "_publish" / "ESC_PCB"

    if not (repo / ".git").exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", REPO_URL, str(repo)], check=True)
    git(repo, "fetch", "-q", "origin")
    remote = git(repo, "ls-remote", "--heads", "origin", branch)
    if remote:
        git(repo, "checkout", "-q", "-B", branch, f"origin/{branch}")
    else:
        git(repo, "checkout", "-q", "-B", branch, "origin/main")

    dest = repo / "ESC PCB V2"
    dest.mkdir(exist_ok=True)
    for f in FILES:
        shutil.copy2(project / f, dest / f)
    for d in DIRS:
        if (project / d).exists():
            shutil.rmtree(dest / d, ignore_errors=True)
            shutil.copytree(project / d, dest / d)
    review_doc = ROOT / "docs" / "ESC_DESIGN_REVIEW.md"
    if review_doc.exists():
        shutil.copy2(review_doc, dest / "DESIGN_REVIEW.md")

    git(repo, "add", "-A", "ESC PCB V2")
    if not git(repo, "status", "--porcelain", "ESC PCB V2"):
        print(f"nothing changed since the last publish on {branch}")
        return

    d = drc_summary.run(str(project / "ESC PCB V2.kicad_pcb"), parity=True)
    nv, nu, np_ = len(d.get("violations", [])), len(d.get("unconnected_items", [])), len(d.get("schematic_parity", []))
    checks = {}
    cj = project / "review" / "design_checks.json"
    if cj.exists():
        checks = json.loads(cj.read_text(encoding="utf-8"))
    c = checks.get("counts", {})
    title = (f"autopcb: V2 DRC {nv} violations / {nu} unconnected / parity {np_}; "
             f"design checks {c.get('FAIL', '?')} fail / {c.get('WARN', '?')} warn")
    body = [f"Automated build + route run, {time.strftime('%Y-%m-%d %H:%M')}.", ""]
    for r in checks.get("results", []):
        if r["status"] != "PASS":
            body.append(f"- {r['status']} {r['check']}: {r['message']}")
    body += ["", "Layer images and the full check report are in ESC PCB V2/review/.", "", TRAILER]
    msg = repo / ".git" / "AUTOPCB_MSG"
    msg.write_text(title + "\n\n" + "\n".join(body) + "\n", encoding="utf-8")
    git(repo, "commit", "-q", "-F", str(msg))
    sha = git(repo, "rev-parse", "--short", "HEAD")
    if push:
        git(repo, "push", "-q", "-u", "origin", branch)
        print(f"published {sha} to {REPO_URL}/tree/{branch}: {title}")
    else:
        print(f"committed {sha} on {branch} (not pushed): {title}")


if __name__ == "__main__":
    main()

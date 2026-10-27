"""Autoroute signal nets with Freerouting, leaving plane nets (pours + vias) alone.

Run with KiCad's Python:
    vendor/KiCAD-MCP-Server/venv/Scripts/python.exe tools/route_board.py <board.kicad_pcb> [passes]

Steps: export Specctra DSN -> drop plane nets from the DSN network -> Freerouting
(headless) -> import the SES session -> refill zones -> save.
"""

from __future__ import annotations

import sys as _sys, os as _os; _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _quiet  # noqa: E402,F401  - no console windows for child processes on Windows

import os
import re
import subprocess
import sys
from pathlib import Path

import pcbnew
import wx

# KiCad's debug asserts pop up modal dialogs on the user's desktop; never show them from scripts.
wx.DisableAsserts()

PLANE_NETS = {"GND", "/VM"}


def find_java() -> str:
    """Freerouting 2.x needs Java 25+. Prefer $FREEROUTING_JAVA, else the newest JDK in the usual places
    (an older JDK earlier on PATH would otherwise fail with UnsupportedClassVersionError)."""
    if os.environ.get("FREEROUTING_JAVA"):
        return os.environ["FREEROUTING_JAVA"]
    cands = []
    for root in (r"C:\Program Files\Java", r"C:\Program Files\Eclipse Adoptium", r"C:\Program Files\Microsoft"):
        r = Path(root)
        if r.is_dir():
            for d in r.iterdir():
                exe = d / "bin" / "java.exe"
                m = re.search(r"(\d+)", d.name)
                if exe.exists() and m:
                    cands.append((int(m.group(1)), str(exe)))
    cands.sort()
    return cands[-1][1] if cands and cands[-1][0] >= 25 else "java"
JAR = os.environ.get("FREEROUTING_JAR", str(Path.home() / ".kicad-mcp" / "freerouting.jar"))


def strip_plane_nets(dsn: str) -> str:
    """Remove (net "NAME" (pins ...)) entries so Freerouting neither routes nor rips them."""
    out, i, removed = [], 0, []
    pattern = re.compile(r'\(net\s+("?)([^\s()"]+|[^"]*)\1\s*\n?\s*\(pins')
    while True:
        m = pattern.search(dsn, i)
        if not m:
            out.append(dsn[i:])
            break
        name = m.group(2)
        start = m.start()
        depth, j = 0, start
        while True:
            if dsn[j] == "(":
                depth += 1
            elif dsn[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append(dsn[i:start])
        if name in PLANE_NETS:
            removed.append(name)
        else:
            out.append(dsn[start:j + 1])
        i = j + 1
    print("plane nets left to pours:", removed)
    return "".join(out)


def _drop_scopes(dsn: str, head: str) -> tuple[str, int]:
    out, i, n = [], 0, 0
    while True:
        k = dsn.find(head, i)
        if k < 0:
            out.append(dsn[i:])
            return "".join(out), n
        depth, j = 0, k
        while True:
            depth += {"(": 1, ")": -1}.get(dsn[j], 0)
            if depth == 0:
                break
            j += 1
        out.append(dsn[i:k])
        i, n = j + 1, n + 1


def strip_plane_shapes(dsn: str) -> str:
    """Drop the structure's (plane NET (polygon ...)) entries. Freerouting treats a pour whose net
    was stripped as an anonymous obstacle, walling off whole layers; the pours are refilled
    after import and clear around the new tracks anyway."""
    out, i, n = [], 0, 0
    keep_layers = ("In1.Cu", "In2.Cu") if "--keep-plane-nets" in sys.argv else ()
    while True:
        k = dsn.find("(plane ", i)
        if k < 0:
            out.append(dsn[i:])
            break
        depth, j = 0, k
        while True:
            depth += {"(": 1, ")": -1}.get(dsn[j], 0)
            if depth == 0:
                break
            j += 1
        if any(f"(polygon {l} " in dsn[k:j] for l in keep_layers):
            out.append(dsn[i:j + 1])
        else:
            out.append(dsn[i:k])
            n += 1
        i = j + 1
    print("pour shapes removed from DSN:", n)
    return "".join(out)


def main() -> None:
    board_path = Path(sys.argv[1]).resolve()
    passes = next((a for a in sys.argv[2:] if a.isdigit()), "40")
    work = board_path.parent / ".copilot" / "route"
    work.mkdir(parents=True, exist_ok=True)
    dsn, ses = work / "board.dsn", work / "board.ses"

    keep = "--keep" in sys.argv
    board = pcbnew.LoadBoard(str(board_path))
    zones = list(board.Zones())   # Zones()/GetTracks() proxies go stale after Remove(); snapshot first
    if keep:
        # incremental: existing routing is locked -> exported as protected, Freerouting only adds
        for t in board.GetTracks():
            t.SetLocked(True)
    else:
        # remove old signal routing; keep plane-net copper and anything locked (fan-outs, via fields)
        for t in list(board.GetTracks()):
            if t.GetNetname() not in PLANE_NETS and not t.IsLocked():
                board.Remove(t)
    # Filled pours would be exported as solid copper and wall the router in; they reflow afterwards.
    for z in zones:
        z.UnFill()
    if not pcbnew.ExportSpecctraDSN(board, str(dsn)):
        raise SystemExit("DSN export failed")
    text = dsn.read_text(encoding="utf-8")
    if "--keep-plane-nets" not in sys.argv:
        text = strip_plane_nets(text)
    if "--no-planes" in sys.argv:
        text = strip_plane_shapes(text)
    if "--no-keepouts" in sys.argv:
        # Freerouting treats every via inside a (wire_)keepout as a violation and won't add new ones there,
        # which walls off any area full of via fields. Layer limits + obstacle planes do the job instead.
        text, n = _drop_scopes(text, "(wire_keepout ")
        print("board keep-outs removed from DSN:", n)
    layers = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--layers=")), None)
    if layers:
        use = "(use_layer " + " ".join(layers.split(",")) + ")"
        text = re.sub(r"(\(use_via [^)]*\))", lambda m: m.group(1) + "\n        " + use, text)
        print("signal layers limited to:", layers)
    dsn.write_text(text, encoding="utf-8")

    ses.unlink(missing_ok=True)   # never import a stale session from an earlier run
    cmd = [find_java(), "-jar", JAR, "-de", str(dsn), "-do", str(ses), "-mp", passes, "-mt", "8"]
    if "--obstacle-planes" in sys.argv:
        cmd.append("--router.conduction_is_obstacle=true")   # inner-plane copper is off limits to other nets' tracks
    print(f"running Freerouting ({passes} passes max)", flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    last = []
    for line in proc.stdout:              # live: Freerouting logs one line per pass with the unrouted count
        line = line.rstrip()
        last = (last + [line])[-30:]
        low = line.lower()
        if ("pass" in low and ("unrouted" in low or "completed" in low)) or "stage completed" in low \
                or "ERROR" in line or "WARN" in line:
            print("  FR", line.split("] ", 1)[-1][:200], flush=True)
    proc.wait(timeout=3600)
    if not ses.exists():
        print("\n".join(last))
        raise SystemExit("Freerouting produced no session file")

    if not pcbnew.ImportSpecctraSES(board, str(ses)):
        raise SystemExit("SES import failed")
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.Save(str(board_path))
    print("routed + zones filled", flush=True)


if __name__ == "__main__":
    main()

"""Run one SPICE deck through ngspice's shared library (the ngspice.dll KiCad ships).

Invoked as a subprocess by copilot.spice so a simulator crash or a stuck
analysis can never take down the MCP server, and every run starts from a
fresh ngspice state:

    python -m copilot.ngspice_runner <deck.cir> <out.raw> <out.log> [dll]
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

SendChar = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
SendStat = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
ControlledExit = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_bool, ctypes.c_bool, ctypes.c_int, ctypes.c_void_p)
BGThreadRunning = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_bool, ctypes.c_int, ctypes.c_void_p)


def main(deck_path: str, raw_path: str, log_path: str, dll_path: str) -> int:
    # absolute paths: we chdir into the output folder before `write` (ngspice splits on spaces)
    deck_path, raw_path, log_path = (str(Path(p).resolve()) for p in (deck_path, raw_path, log_path))
    os.add_dll_directory(str(Path(dll_path).parent))
    lib = ctypes.CDLL(dll_path)
    log: list[str] = []
    exited = {"code": None}

    @SendChar
    def on_char(text, _id, _user):
        line = text.decode(errors="replace")
        # ngspice prefixes lines with "stdout " / "stderr "
        for prefix in ("stdout ", "stderr "):
            if line.startswith(prefix):
                line = line[len(prefix):] if prefix == "stdout " else "ERR: " + line[len(prefix):]
        log.append(line)
        return 0

    @SendStat
    def on_stat(_text, _id, _user):
        return 0

    @ControlledExit
    def on_exit(status, _unload, _quit, _id, _user):
        exited["code"] = status
        return 0

    @BGThreadRunning
    def on_bg(_running, _id, _user):
        return 0

    lib.ngSpice_Init(on_char, on_stat, on_exit, None, None, on_bg, None)
    lines = Path(deck_path).read_text(encoding="utf-8", errors="replace").splitlines()
    # Circuit lines must end with .end; ngSpice_Circ takes a NULL-terminated char* array.
    if not any(l.strip().lower() == ".end" for l in lines):
        lines.append(".end")
    arr = (ctypes.c_char_p * (len(lines) + 1))(*[l.encode() for l in lines], None)
    cmd = lambda c: lib.ngSpice_Command(c.encode())  # noqa: E731

    cmd("set ngbehavior=ltpsa")  # accept common LTspice/PSpice syntax
    rc = lib.ngSpice_Circ(arr)
    cmd("option rshunt=1e12")  # tame floating nodes from partial schematics
    if rc != 0 or exited["code"] not in (None, 0):
        Path(log_path).write_text("\n".join(log), encoding="utf-8")
        return 2
    cmd("run")
    cmd("set filetype=binary")
    # ngspice splits `write` arguments on spaces, so write a bare filename from the output folder.
    os.chdir(Path(raw_path).resolve().parent)
    cmd(f"write {Path(raw_path).name} all")
    Path(log_path).write_text("\n".join(log), encoding="utf-8")
    return 0 if Path(raw_path).exists() else 3


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:5]))

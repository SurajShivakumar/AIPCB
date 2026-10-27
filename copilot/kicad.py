"""Locating KiCad, resolving projects, and running kicad-cli."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

KICAD_FILE_SUFFIXES = (".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_sym", ".kicad_mod")
TRACKED_NAMES = ("sym-lib-table", "fp-lib-table")


class KicadError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def kicad_cli() -> str:
    """Path to kicad-cli: $KICAD_CLI, then the newest standard install, then PATH."""
    if env := os.environ.get("KICAD_CLI"):
        return env
    roots = [Path(r"C:\Program Files\KiCad"), Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "KiCad"]
    candidates = []
    for root in roots:
        if root.is_dir():
            for version_dir in root.iterdir():
                exe = version_dir / "bin" / "kicad-cli.exe"
                if exe.exists():
                    candidates.append((_version_key(version_dir.name), exe))
    if candidates:
        return str(max(candidates)[1])
    for name in ("kicad-cli", "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"):
        if _runs(name):
            return name
    raise KicadError("kicad-cli not found. Install KiCad 9+ or set KICAD_CLI.")


def _version_key(name: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-]", name))


def _runs(exe: str) -> bool:
    try:
        subprocess.run([exe, "version"], capture_output=True, timeout=20)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def run_cli(*args: str, timeout: float = 300) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [kicad_cli(), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )
    # kicad-cli exits non-zero when ERC/DRC finds violations; only treat a missing
    # output as failure, which callers check themselves.
    return proc


@dataclass(frozen=True)
class Project:
    """A KiCad project: one directory holding <name>.kicad_pro/.kicad_sch/.kicad_pcb."""

    dir: Path
    name: str

    @property
    def sch(self) -> Path:
        return self.dir / f"{self.name}.kicad_sch"

    @property
    def pcb(self) -> Path:
        return self.dir / f"{self.name}.kicad_pcb"

    @property
    def pro(self) -> Path:
        return self.dir / f"{self.name}.kicad_pro"

    @property
    def state_dir(self) -> Path:
        """Copilot's private folder inside the project (checkpoints, caches, reports)."""
        d = self.dir / ".copilot"
        d.mkdir(exist_ok=True)
        ignore = d / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n")
        return d

    def tracked_files(self) -> list[Path]:
        """KiCad design files under the project dir (recursive, skipping backups/state)."""
        out = []
        for p in self.dir.rglob("*"):
            rel = p.relative_to(self.dir).parts
            if any(part.startswith(".") or part.endswith("-backups") for part in rel[:-1]):
                continue
            if p.is_file() and (p.suffix in KICAD_FILE_SUFFIXES or p.name in TRACKED_NAMES):
                out.append(p)
        return sorted(out)


def resolve_project(path: str | os.PathLike) -> Project:
    """Accept a project dir or any .kicad_pro/.kicad_sch/.kicad_pcb path."""
    p = Path(path).expanduser().resolve()
    if p.is_dir():
        pros = sorted(p.glob("*.kicad_pro"))
        if len(pros) == 1:
            return Project(p, pros[0].stem)
        schs = sorted(p.glob("*.kicad_sch"))
        if not pros and len(schs) == 1:
            return Project(p, schs[0].stem)
        raise KicadError(f"{p} holds {len(pros)} .kicad_pro files; pass the one you mean.")
    if p.suffix in (".kicad_pro", ".kicad_sch", ".kicad_pcb"):
        if not p.exists():
            raise KicadError(f"{p} does not exist")
        root = p.with_suffix(".kicad_pro")
        if p.suffix == ".kicad_sch" and not root.exists():
            # A sub-sheet: its project is the .kicad_pro in the same folder.
            pros = sorted(p.parent.glob("*.kicad_pro"))
            if len(pros) == 1:
                return Project(p.parent, pros[0].stem)
        return Project(p.parent, p.stem)
    raise KicadError(f"Not a KiCad project path: {p}")


def erc(sch: Path, out_dir: Path) -> dict:
    """Run kicad-cli ERC and return its JSON report."""
    out = out_dir / "erc.json"
    proc = run_cli("sch", "erc", "--format", "json", "--severity-all", "-o", str(out), str(sch))
    if not out.exists():
        raise KicadError(f"ERC failed: {proc.stderr or proc.stdout}")
    return json.loads(out.read_text(encoding="utf-8"))


def drc(pcb: Path, out_dir: Path, *, schematic_parity: bool = True) -> dict:
    """Run kicad-cli DRC and return its JSON report."""
    out = out_dir / "drc.json"
    args = ["pcb", "drc", "--format", "json", "--units", "mm", "--severity-all", "-o", str(out)]
    if schematic_parity:
        args.append("--schematic-parity")
    proc = run_cli(*args, str(pcb), timeout=900)
    if not out.exists():
        raise KicadError(f"DRC failed: {proc.stderr or proc.stdout}")
    return json.loads(out.read_text(encoding="utf-8"))


def export_netlist_xml(sch: Path, out: Path) -> Path:
    proc = run_cli("sch", "export", "netlist", "--format", "kicadxml", "-o", str(out), str(sch))
    if not out.exists():
        raise KicadError(f"Netlist export failed: {proc.stderr or proc.stdout}")
    return out


def export_svgs(path: Path, out_dir: Path, *, pcb_layers: str | None = None) -> list[Path]:
    """Render a schematic (all sheets) or a board to SVG. Returns the files written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.svg"):
        old.unlink()
    if path.suffix == ".kicad_sch":
        proc = run_cli("sch", "export", "svg", "-o", str(out_dir), str(path))
    else:
        layers = pcb_layers or "F.Cu,B.Cu,F.Silkscreen,B.Silkscreen,Edge.Cuts,F.Fab"
        out = out_dir / f"{path.stem}.svg"
        proc = run_cli(
            "pcb", "export", "svg", "--mode-single", "--fit-page-to-board", "--exclude-drawing-sheet",
            "-l", layers, "-o", str(out), str(path),
        )
    files = sorted(out_dir.glob("*.svg"))
    if not files:
        raise KicadError(f"SVG export failed: {proc.stderr or proc.stdout}")
    return files

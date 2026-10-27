"""ERC/DRC reports normalized into comparable violations, and deltas between two runs."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import kicad

_REF_PATTERNS = [
    re.compile(r"\bSymbol (?P<ref>[A-Za-z_#][\w#]*)\b"),
    re.compile(r"\bof (?P<ref>[A-Za-z_][\w]*) on\b"),
    re.compile(r"\bFootprint (?P<ref>[A-Za-z_][\w]*)\b"),
]
# ERC item positions come out of kicad-cli in units of 100 mm.
ERC_POS_SCALE = 100.0


@dataclass
class Violation:
    source: str  # erc | drc | parity | unconnected
    type: str
    severity: str
    description: str
    items: list[str]
    refs: list[str]
    positions: list[tuple[float, float]] = field(default_factory=list)
    sheet: str = "/"

    @property
    def key(self) -> tuple:
        # Position-free identity so moving a symbol doesn't make a violation "new".
        return (self.source, self.type, tuple(sorted(self.items)))

    def to_dict(self) -> dict:
        d = {
            "source": self.source,
            "type": self.type,
            "severity": self.severity,
            "description": self.description,
            "items": self.items,
        }
        if self.refs:
            d["refs"] = self.refs
        if self.sheet != "/":
            d["sheet"] = self.sheet
        return d

    def line(self) -> str:
        where = f" [{', '.join(self.refs)}]" if self.refs else ""
        return f"{self.severity.upper()} {self.source}/{self.type}: {self.description}{where}"


def _refs(descriptions: list[str]) -> list[str]:
    out = []
    for d in descriptions:
        for pat in _REF_PATTERNS:
            if m := pat.search(d):
                ref = m.group("ref")
                if not ref.startswith("#") and ref not in out:
                    out.append(ref)
    return out


def _violation(source: str, v: dict, scale: float = 1.0, sheet: str = "/") -> Violation:
    items = [i.get("description", "") for i in v.get("items", [])]
    positions = [
        (i["pos"]["x"] * scale, i["pos"]["y"] * scale) for i in v.get("items", []) if "pos" in i
    ]
    return Violation(source, v.get("type", ""), v.get("severity", ""), v.get("description", ""), items,
                     _refs(items), positions, sheet)


def parse_erc(report: dict) -> list[Violation]:
    out = []
    for sheet in report.get("sheets", []):
        for v in sheet.get("violations", []):
            out.append(_violation("erc", v, ERC_POS_SCALE, sheet.get("path", "/")))
    for v in report.get("violations", []):  # KiCad 8 layout
        out.append(_violation("erc", v, ERC_POS_SCALE))
    return out


def parse_drc(report: dict) -> list[Violation]:
    out = [_violation("drc", v) for v in report.get("violations", [])]
    out += [_violation("unconnected", v) for v in report.get("unconnected_items", [])]
    out += [_violation("parity", v) for v in report.get("schematic_parity", [])]
    return out


def run_erc(sch: Path, work_dir: Path) -> list[Violation]:
    work_dir.mkdir(parents=True, exist_ok=True)
    return parse_erc(kicad.erc(sch, work_dir))


def run_drc(pcb: Path, work_dir: Path) -> list[Violation]:
    work_dir.mkdir(parents=True, exist_ok=True)
    return parse_drc(kicad.drc(pcb, work_dir))


def summarize(violations: list[Violation], max_lines: int = 40) -> str:
    if not violations:
        return "No violations."
    counts = Counter((v.severity, v.source, v.type) for v in violations)
    head = ", ".join(f"{n}x {sev} {src}/{typ}" for (sev, src, typ), n in counts.most_common())
    order = {"error": 0, "warning": 1, "exclusion": 3}
    lines = [v.line() for v in sorted(violations, key=lambda v: order.get(v.severity, 2))[:max_lines]]
    more = f"\n... {len(violations) - max_lines} more" if len(violations) > max_lines else ""
    return f"{len(violations)} violations: {head}\n" + "\n".join(lines) + more


def delta(before: list[Violation], after: list[Violation]) -> dict:
    """Violations introduced and resolved between two runs (multiset by key)."""
    cb, ca = Counter(v.key for v in before), Counter(v.key for v in after)
    new_keys, gone_keys = ca - cb, cb - ca
    new, resolved = [], []
    for v in after:
        if new_keys[v.key] > 0:
            new.append(v)
            new_keys[v.key] -= 1
    for v in before:
        if gone_keys[v.key] > 0:
            resolved.append(v)
            gone_keys[v.key] -= 1
    return {"new": new, "resolved": resolved}

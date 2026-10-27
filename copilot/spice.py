"""SPICE: build decks from the KiCad netlist, run LTspice or ngspice, and check results.

Two simulators, one interface:
  - LTspice (ADI, free) in batch mode, if installed.
  - ngspice through the ngspice.dll that ships with KiCad (always available).
Running "both" cross-checks one simulator against the other.

Assertions are evaluated here in Python on the raw waveforms, so they mean the
same thing on either simulator and give a hard pass/fail (measured, not judged).
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .kicad import kicad_cli
from .netlist import Netlist, _ref_key

# ------------------------------------------------------------- simulators
def find_ltspice() -> str | None:
    candidates = [
        os.environ.get("LTSPICE", ""),
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "ADI" / "LTspice" / "LTspice.exe"),
        r"C:\Program Files\ADI\LTspice\LTspice.exe",
        r"C:\Program Files\LTC\LTspiceXVII\XVIIx64.exe",
        "/Applications/LTspice.app/Contents/MacOS/LTspice",
    ]
    return next((c for c in candidates if c and Path(c).exists()), None)


def find_ngspice_dll() -> str | None:
    if env := os.environ.get("NGSPICE_DLL"):
        return env
    try:
        dll = Path(kicad_cli()).parent / "ngspice.dll"
        if dll.exists():
            return str(dll)
    except Exception:
        pass
    return None


def available() -> dict:
    return {"ltspice": find_ltspice(), "ngspice": find_ngspice_dll()}


class SpiceError(RuntimeError):
    pass


@dataclass
class RunResult:
    simulator: str
    raw: Path | None
    log: str
    ok: bool


def _run_ltspice(deck: Path, timeout: float) -> RunResult:
    exe = find_ltspice()
    if not exe:
        raise SpiceError("LTspice not found (install it from analog.com or set LTSPICE).")
    try:
        subprocess.run([exe, "-b", deck.name], cwd=deck.parent, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return RunResult("ltspice", None, f"LTspice timed out after {timeout}s", False)
    log_path = deck.with_suffix(".log")
    log = ""
    if log_path.exists():
        data = log_path.read_bytes()
        log = data.decode("utf-16-le", errors="replace") if data[1:2] == b"\x00" else data.decode(errors="replace")
    raw = deck.with_suffix(".raw")
    failed = not raw.exists() or re.search(r"^(Error|Fatal)|singular matrix|Unknown|can't find", log, re.I | re.M)
    return RunResult("ltspice", raw if raw.exists() else None, log, not failed)


def _run_ngspice(deck: Path, timeout: float) -> RunResult:
    dll = find_ngspice_dll()
    if not dll:
        raise SpiceError("ngspice.dll not found (it ships with KiCad; or set NGSPICE_DLL).")
    raw, log_path = deck.with_name(deck.stem + "_ng.raw"), deck.with_name(deck.stem + "_ng.log")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "copilot.ngspice_runner", str(deck), str(raw), str(log_path), dll],
            cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return RunResult("ngspice", None, f"ngspice timed out after {timeout}s", False)
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else proc.stderr
    failed = proc.returncode != 0 or not raw.exists() or re.search(r"^ERR: .*(error|fatal)", log, re.I | re.M)
    return RunResult("ngspice", raw if raw.exists() else None, log, not failed)


def run_deck(deck_text: str, simulator: str = "auto", workdir: Path | None = None, timeout: float = 120) -> list[RunResult]:
    sims = {"auto": ["ltspice" if find_ltspice() else "ngspice"], "both": ["ltspice", "ngspice"]}.get(simulator, [simulator])
    # absolute: the ngspice runner is a subprocess with its own working directory
    workdir = Path(workdir or tempfile.mkdtemp(prefix="copilot-spice-")).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    deck = workdir / "deck.cir"
    deck.write_text(deck_text if deck_text.lstrip().startswith("*") else "* copilot deck\n" + deck_text, encoding="utf-8")
    runners = {"ltspice": _run_ltspice, "ngspice": _run_ngspice}
    out = []
    for sim in sims:
        if sim not in runners:
            raise SpiceError(f"Unknown simulator {sim!r}; use auto, ltspice, ngspice or both.")
        out.append(runners[sim](deck, timeout))
    return out


# ------------------------------------------------------------- waveforms
class Waves:
    def __init__(self, raw: Path):
        from spicelib import RawRead

        self._raw = RawRead(str(raw), verbose=False)
        names = self._raw.get_trace_names()
        self.plot = (self._raw.get_raw_property("Plotname") or "").lower()
        self.axis_name = names[0].lower() if names else ""
        self.traces = {n.lower(): n for n in names}
        self.is_op = "operating point" in self.plot or self.axis_name not in ("time", "frequency")

    def axis(self) -> np.ndarray | None:
        if self.is_op:
            return None
        return np.real(np.asarray(self._raw.get_axis(0)))

    def wave(self, name: str) -> np.ndarray:
        key = name.lower()
        if key in ("v(0)", "0"):
            ax = self.axis()
            return np.zeros(1 if ax is None else len(ax))
        candidates = [key]
        if m := re.fullmatch(r"v\((.+)\)", key):
            candidates.append(m.group(1))
        if m := re.fullmatch(r"i\((.+)\)", key):
            candidates += [f"{m.group(1)}#branch", f"i({m.group(1)})"]
        for c in candidates:
            if c in self.traces:
                return np.asarray(self._raw.get_trace(self.traces[c]).get_wave(0))
        raise KeyError(name)

    def names(self) -> list[str]:
        return list(self.traces)


_EXPR = re.compile(r"^\s*([VvIi])\(\s*([^,()]+?)\s*(?:,\s*([^,()]+?)\s*)?\)\s*$")


def evaluate(waves: Waves, expr: str, node_map: dict[str, str]) -> np.ndarray:
    """V(net) | V(net1,net2) | I(Vsource); net names may be KiCad names or SPICE nodes."""
    m = _EXPR.match(expr)
    if not m:
        raise SpiceError(f"Unsupported expression {expr!r}; use V(net), V(a,b) or I(Vname).")
    kind, a, b = m.group(1).upper(), m.group(2), m.group(3)
    node = lambda n: node_map.get(n, node_map.get("/" + n, n))  # noqa: E731
    if kind == "I":
        return waves.wave(f"i({a})")
    va = waves.wave(f"v({node(a)})")
    return va - waves.wave(f"v({node(b)})") if b else va


def statistic(waves: Waves, y: np.ndarray, stat: str, at: float | None = None) -> float:
    x = waves.axis()
    if np.iscomplexobj(y):
        y = np.abs(y)
    y = np.real(y)
    if x is None or len(y) == 1:
        return float(y[0])
    stat = stat.lower()
    if stat == "final":
        return float(y[-1])
    if stat == "max":
        return float(np.max(y))
    if stat == "min":
        return float(np.min(y))
    if stat == "pp":
        return float(np.max(y) - np.min(y))
    if stat in ("avg", "rms"):
        span = x[-1] - x[0]
        if span <= 0:
            return float(np.mean(y))
        val = np.trapezoid(y if stat == "avg" else y * y, x) / span
        return float(val if stat == "avg" else math.sqrt(max(val, 0)))
    if stat == "at":
        if at is None:
            raise SpiceError("stat 'at' needs an 'at' value (time or frequency).")
        return float(np.interp(at, x, y))
    raise SpiceError(f"Unknown stat {stat!r}; use final, max, min, avg, rms, pp or at.")


def check_results(waves: Waves, checks: list[dict], node_map: dict[str, str]) -> list[dict]:
    out = []
    for c in checks:
        expr, stat = c["expr"], c.get("stat", "final")
        try:
            value = statistic(waves, evaluate(waves, expr, node_map), stat, c.get("at"))
        except (KeyError, SpiceError) as exc:
            out.append({**c, "pass": False, "error": f"cannot evaluate: {exc}"})
            continue
        lo, hi = c.get("min"), c.get("max")
        ok = (lo is None or value >= lo) and (hi is None or value <= hi)
        out.append({**c, "value": round(value, 6), "pass": bool(ok)})
    return out


def node_summary(waves: Waves, limit: int = 40) -> dict[str, dict]:
    out = {}
    for name in waves.names():
        if not name.startswith("v(") or len(out) >= limit:
            continue
        y = np.real(np.abs(waves.wave(name)) if waves.plot.startswith("ac") else waves.wave(name))
        if waves.is_op or len(y) == 1:
            out[name] = {"value": round(float(y[0]), 6)}
        else:
            out[name] = {"min": round(float(y.min()), 6), "max": round(float(y.max()), 6), "final": round(float(y[-1]), 6)}
    return out


# ------------------------------------------------------------- deck builder
_SI = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3,
       "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "R": 1.0, "r": 1.0}


def parse_value(text: str) -> float | None:
    """Schematic value -> float. Handles 10k, 4k7, 4.7uF, 0R, 100R, 1M (mega), 10uF/25V, 2.2µH."""
    s = text.strip().split()[0] if text.strip() else ""
    s = s.split("/")[0].replace(",", ".")
    s = re.sub(r"(?i)(ohms?|Ω|F|H)$", "", s)
    if m := re.fullmatch(r"(\d+)([fpnuµμmkKMGTRr])(\d+)", s):  # 4k7, 2R2, 4u7
        return float(f"{m.group(1)}.{m.group(3)}") * _SI[m.group(2)]
    if m := re.fullmatch(r"(\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*(meg|Meg|MEG)?([fpnuµμmkKMGTRr])?", s):
        mult = 1e6 if m.group(2) else _SI.get(m.group(3) or "", 1.0)
        return float(m.group(1)) * mult
    return None


def _node_name(net: str, used: dict[str, str]) -> str:
    if Netlist.is_ground(net):
        return "0"
    bare = net.rsplit("/", 1)[-1] if not net.startswith(("Net-(", "unconnected-(")) else net
    s = bare.replace("Net-(", "N_").replace("unconnected-(", "NC_").replace(")", "")
    s = re.sub(r"^\+", "P", s)
    s = s.replace("+", "_P").replace("-", "_N").replace(".", "p")
    s = re.sub(r"[^A-Za-z0-9_]", "_", s).strip("_") or "N"
    if s[0].isdigit():
        s = "N" + s
    base, i = s, 2
    while s.lower() in {v.lower() for k, v in used.items() if k != net}:
        s, i = f"{base}_{i}", i + 1
    return s


DEFAULT_MODELS = {
    "D_GENERIC": ".model D_GENERIC D(Is=2.5n Rs=0.5 N=1.75 Cjo=4p BV=100 IBV=1u)",
    "LED_RED": ".model LED_RED D(Is=1e-19 N=1.6 Rs=2 Cjo=20p)",       # Vf ~1.9V @ 10mA
    "LED_GREEN": ".model LED_GREEN D(Is=1e-22 N=1.9 Rs=3 Cjo=20p)",   # Vf ~2.2V @ 10mA
    "LED_BLUE": ".model LED_BLUE D(Is=1e-26 N=2.2 Rs=4 Cjo=20p)",     # Vf ~3.0V @ 10mA
}


@dataclass
class Deck:
    text: str
    node_map: dict[str, str]
    modeled: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def coverage(self) -> dict:
        return {"modeled": self.modeled, "not_modeled": self.skipped, "notes": self.notes}


def build_deck(
    nl: Netlist,
    analysis: str,
    *,
    refs: list[str] | None = None,
    sources: dict[str, str | float] | None = None,
    loads: dict[str, str | float] | None = None,
    models: dict[str, str] | None = None,
    extra: str = "",
    title: str = "copilot deck",
) -> Deck:
    """Turn (part of) a KiCad netlist into a SPICE deck.

    refs     only include these parts (default: every part that can be modeled)
    sources  {net: 3.3 | "PULSE(...)" | "SIN(...)"}: voltage source from net to ground
    loads    {net: "50m"}: DC current sink from net to ground (stand-in for an IC's draw)
    models   {ref: "XU1 {1} {2} MYMODEL"}: your own element line for a part; {pin} is
             replaced by the node on that pin (number or pin name). Add .model/.subckt in extra.
    """
    if not analysis.strip().startswith("."):
        raise SpiceError("analysis must be a SPICE directive such as '.op', '.tran 1u 5m' or '.ac dec 50 10 1meg'.")
    sources, loads, models = sources or {}, loads or {}, models or {}
    node_map: dict[str, str] = {}
    for net in sorted(nl.nets):
        node_map[net] = _node_name(net, node_map)

    def resolve_net(name: str) -> str:
        for cand in (name, "/" + name):
            if cand in node_map:
                return cand
        raise SpiceError(f"No net named {name!r} in the schematic.")

    p2n = nl.pin_to_net()
    wanted = set(refs) if refs else set(nl.real_components())
    unknown = wanted - set(nl.real_components())
    if unknown:
        raise SpiceError(f"Unknown refs: {sorted(unknown)}")

    lines = [f"* {title}", "* generated by AI PCB Copilot from the KiCad netlist"]
    used_models: set[str] = set()
    deck = Deck("", node_map)

    def pin_node(ref: str, pin: str) -> str:
        net = p2n.get((ref, pin))
        return node_map[net] if net else f"NC_{ref}_{pin}"

    for ref in sorted(wanted, key=_ref_key):
        comp = nl.components[ref]
        pins = sorted({p for (r, p) in p2n if r == ref}, key=lambda p: (len(p), p))
        funcs = {nl.pin_functions.get((ref, p), ""): p for p in pins}
        if ref in models:
            line = re.sub(
                r"\{([^{}]+)\}",
                lambda m: pin_node(ref, m.group(1) if m.group(1) in pins else funcs.get(m.group(1), m.group(1))),
                models[ref],
            )
            lines.append(line)
            deck.modeled.append(ref)
            continue
        lib = comp.lib_id.lower()
        prefix = comp.prefix.upper()
        value = parse_value(comp.value)
        if prefix == "R" and len(pins) == 2 and value is not None:
            lines.append(f"R{ref} {pin_node(ref, pins[0])} {pin_node(ref, pins[1])} {max(value, 1e-3):g}")
        elif prefix == "C" and len(pins) == 2 and value is not None:
            lines.append(f"C{ref} {pin_node(ref, pins[0])} {pin_node(ref, pins[1])} {value:g}")
        elif prefix == "L" and len(pins) == 2 and value is not None:
            lines.append(f"L{ref} {pin_node(ref, pins[0])} {pin_node(ref, pins[1])} {value:g} Rser=10m")
        elif prefix == "FB" or "ferrite" in lib:
            lines.append(f"R{ref} {pin_node(ref, pins[0])} {pin_node(ref, pins[1])} 0.1")
            deck.notes.append(f"{ref} ({comp.value}) modeled as 0.1 ohm (DC only).")
        elif prefix in ("D", "LED", "CR") and {"A", "K"} <= set(funcs):
            a, k = pin_node(ref, funcs["A"]), pin_node(ref, funcs["K"])
            if "led" in lib.split(":")[-1]:
                color = next((c for c in ("RED", "GREEN", "BLUE") if c in comp.value.upper()), "RED")
                model = f"LED_{color}"
            elif (z := re.search(r"C?(\d+)V(\d*)", comp.value.upper())) and ("zener" in lib or "zener" in comp.description.lower()):
                bv = float(f"{z.group(1)}.{z.group(2) or 0}")
                model = f"DZ_{z.group(1)}V{z.group(2)}"
                lines.append(f".model {model} D(Is=1n Rs=1 N=1.2 BV={bv:g} IBV=5m)")
            else:
                model = "D_GENERIC"
            used_models.add(model)
            lines.append(f"D{ref} {a} {k} {model}")
        else:
            deck.skipped[ref] = f"{comp.value} [{comp.lib_id}]: no generic model; pass models={{'{ref}': ...}} or loads"
            continue
        deck.modeled.append(ref)

    for net, spec in sources.items():
        key = resolve_net(net)
        node = node_map[key]
        if node == "0":
            raise SpiceError(f"Cannot put a source on ground net {net!r}.")
        spec_s = f"DC {spec}" if isinstance(spec, (int, float)) or re.fullmatch(r"[\d.eE+-]+[a-zA-Z]*", str(spec)) else str(spec)
        lines.append(f"V_{node} {node} 0 {spec_s}")
    for net, spec in loads.items():
        node = node_map[resolve_net(net)]
        lines.append(f"I_{node} {node} 0 {spec if not isinstance(spec, (int, float)) else f'DC {spec}'}")
    lines += [DEFAULT_MODELS[m] for m in sorted(used_models) if m in DEFAULT_MODELS]
    if extra.strip():
        lines += ["* --- extra", extra.strip()]
    lines += [analysis.strip(), ".end"]
    deck.text = "\n".join(lines) + "\n"
    if not sources:
        deck.notes.append("No sources given: every node will sit at 0 V. Pass sources={'+3.3V': 3.3, ...}.")
    return deck


def simulate(deck_text: str, *, simulator: str = "auto", checks: list[dict] | None = None,
             node_map: dict[str, str] | None = None, workdir: Path | None = None, timeout: float = 120) -> dict:
    """Run a deck and return per-simulator node summaries and assertion results."""
    node_map = node_map or {}
    results = {}
    for run in run_deck(deck_text, simulator, workdir, timeout):
        entry: dict = {"ok": run.ok}
        tail = "\n".join(l for l in run.log.splitlines() if l.strip())[-1500:]
        if not run.ok or run.raw is None:
            entry["log"] = tail
            results[run.simulator] = entry
            continue
        waves = Waves(run.raw)
        entry["analysis"] = waves.plot
        entry["nodes"] = node_summary(waves)
        if checks:
            entry["checks"] = check_results(waves, checks, node_map)
            entry["all_checks_pass"] = all(c["pass"] for c in entry["checks"])
        if re.search(r"warning", run.log, re.I):
            entry["log_warnings"] = [l for l in run.log.splitlines() if re.search(r"warning", l, re.I)][:10]
        results[run.simulator] = entry
    if len(results) == 2 and checks and all("checks" in r for r in results.values()):
        a, b = (results[k]["checks"] for k in ("ltspice", "ngspice"))
        disagreements = [
            {"expr": x["expr"], "stat": x.get("stat", "final"), "ltspice": x.get("value"), "ngspice": y.get("value")}
            for x, y in zip(a, b)
            if x.get("value") is not None and y.get("value") is not None
            and abs(x["value"] - y["value"]) > 0.02 * max(abs(x["value"]), abs(y["value"]), 1e-9)
        ]
        results["cross_check"] = {"agree": not disagreements, "disagreements": disagreements}
    return results

"""Measured design review: deterministic rules over the KiCad netlist, ERC, DRC and fab rules.

Findings here are facts with evidence (refs, nets). The LLM client adds its
own judgement on top, but a measured error always means the review fails:
the pass/fail verdict is never left to the LLM (the "measured vs judged"
rule from the pcb-ai pipeline).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import checks
from .kicad import Project
from .netlist import Netlist, _ref_key

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
FAB_PROFILES = Path(__file__).resolve().parent.parent / "fab-profiles"


@dataclass
class Finding:
    rule: str
    severity: str  # error | warning | info
    message: str
    refs: list[str] = field(default_factory=list)
    nets: list[str] = field(default_factory=list)
    evidence: str = ""
    fix: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v}


@dataclass
class Context:
    project: Project
    netlist: Netlist
    erc: list[checks.Violation] | None = None
    drc: list[checks.Violation] | None = None
    fab: dict | None = None

    # Helpers shared by rules -------------------------------------------------
    def kind(self, ref: str) -> str:
        c = self.netlist.components.get(ref)
        lib = (c.lib_id if c else "").lower()
        prefix = re.match(r"[A-Za-z]*", ref).group(0).upper()
        if "led" in lib.split(":")[-1] or prefix == "LED":
            return "led"
        if "crystal" in lib or "resonator" in lib or prefix in ("Y", "X"):
            return "crystal"
        if prefix == "C" or lib.startswith("device:c"):
            return "cap"
        if prefix == "R" and not lib.startswith("device:r_pot"):
            return "res"
        if prefix in ("L", "FB"):
            return "ind"
        if prefix in ("U", "IC"):
            return "ic"
        if prefix in ("J", "P", "CN", "USB"):
            return "conn"
        if prefix in ("D", "TVS", "CR"):
            return "diode"
        return "other"

    def two_pin_others(self, ref: str, net: str) -> list[str]:
        """For a 2-pin part with one pin on `net`, the net(s) its other pin is on."""
        pins = self.netlist.nets_of(ref)
        if len(pins) != 2:
            return []
        return [n for n in pins.values() if n != net]

    def parts_bridging(self, net: str, kind: str) -> dict[str, str]:
        """Parts of a kind with one pin on `net` -> the net on their other pin."""
        out = {}
        for node in self.netlist.nets[net].nodes:
            if self.kind(node.ref) == kind:
                others = self.two_pin_others(node.ref, net)
                if others:
                    out[node.ref] = others[0]
        return out


# ===================================================================== rules
def rule_missing_sheet_files(ctx: Context) -> list[Finding]:
    out = []
    for sheet in ctx.netlist.sheets[1:]:
        src = sheet.get("source") or ""
        if src and not (ctx.project.dir / src).exists():
            out.append(Finding(
                "missing-sheet-file", "warning",
                f"Sheet '{sheet['name']}' points to '{src}', which does not exist; KiCad treats it as empty.",
                evidence=f"sheet {sheet['name']} -> {src}",
                fix="Point the sheet at the right file, or delete the sheet box if it is unused.",
            ))
    return out


PAPER_MM = {"A5": (210, 148), "A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594),
            "A0": (1189, 841), "A": (279.4, 215.9), "B": (431.8, 279.4), "C": (558.8, 431.8),
            "D": (863.6, 558.8), "E": (1117.6, 863.6), "USLetter": (279.4, 215.9), "USLegal": (355.6, 215.9),
            "USLedger": (431.8, 279.4)}


def rule_off_page(ctx: Context) -> list[Finding]:
    """Symbols placed outside the drawing sheet vanish from PDFs/prints and are easy to miss."""
    from .viewer import symbol_positions

    text = ctx.project.sch.read_text(encoding="utf-8", errors="replace")[:4000]
    m = re.search(r'\(paper "([^"]+)"(?: ([\d.]+) ([\d.]+))?( portrait)?\)', text)
    if not m:
        return []
    if m.group(2):
        w, h = float(m.group(2)), float(m.group(3))
    elif m.group(1) in PAPER_MM:
        w, h = PAPER_MM[m.group(1)]
    else:
        return []
    if m.group(4):
        w, h = h, w
    off = sorted((r for r, (x, y) in symbol_positions(ctx.project.sch).items() if not (0 <= x <= w and 0 <= y <= h)),
                 key=_ref_key)
    if not off:
        return []
    return [Finding(
        "off-page", "warning",
        f"{len(off)} symbols sit outside the {m.group(1)} sheet, so they are cut from PDFs and printouts.",
        refs=off, evidence=f"sheet {w}x{h} mm",
        fix="Use a larger page size (Page Settings) or move the parts onto the sheet / into a sub-sheet.",
    )]


def rule_cap_only_nets(ctx: Context) -> list[Finding]:
    """Capacitors whose terminal connects only to other capacitors: a missing power/net label."""
    out = []
    for name, net in ctx.netlist.nets.items():
        refs = {n.ref for n in net.nodes}
        if len(net.nodes) >= 2 and all(ctx.kind(r) == "cap" for r in refs):
            if ctx.netlist.is_ground(name) or ctx.netlist.is_power(name):
                continue
            others = sorted({o for r in refs for o in ctx.two_pin_others(r, name)})
            out.append(Finding(
                "caps-only-net", "error",
                f"{len(refs)} capacitors are connected only to each other on '{name}'; "
                "they decouple nothing.",
                refs=sorted(refs, key=_ref_key), nets=[name] + others,
                evidence=", ".join(f"{n.ref}.{n.pin}" for n in net.nodes) + f"; other side: {', '.join(others)}",
                fix="This is usually a missing power label or wire: connect this node to the supply rail it should decouple.",
            ))
    return out


def rule_decoupling(ctx: Context) -> list[Finding]:
    """Every IC supply net needs at least one capacitor to ground."""
    out = []
    nl = ctx.netlist
    for ref in sorted(nl.real_components(), key=_ref_key):
        if ctx.kind(ref) != "ic":
            continue
        supply_nets = sorted({
            nl.pin_to_net()[(ref, pin)]
            for (r, pin), t in nl.pin_types.items()
            if r == ref and t == "power_in" and (ref, pin) in nl.pin_to_net()
        })
        for net in supply_nets:
            if nl.is_ground(net):
                continue
            bridging = ctx.parts_bridging(net, "cap")
            caps = [c for c, other in bridging.items() if nl.is_ground(other)]
            if not caps and net.startswith("Net-(") and bridging:
                continue  # bootstrap / flying cap (e.g. BST to SW): intentionally not to ground
            if not caps:
                out.append(Finding(
                    "missing-decoupling", "error" if not net.startswith(("Net-(", "unconnected")) else "warning",
                    f"{ref} ({nl.components[ref].value}) supply net '{net}' has no capacitor to ground.",
                    refs=[ref], nets=[net],
                    evidence=f"power_in pins of {ref} on {net}; caps from {net} to GND: none",
                    fix=f"Add a 100nF (plus bulk if needed) capacitor from {net} to GND close to {ref}.",
                ))
    return out


_I2C = re.compile(r"(^|[/_])(I2C\d*_)?(SDA|SCL)\d*$", re.I)
_OPEN_DRAIN = re.compile(r"(^|[/_])n?(FAULT|ALERT|INT|IRQ|PG|PGOOD|POWER_?GOOD)\w*$", re.I)


def rule_pullups(ctx: Context) -> list[Finding]:
    out = []
    nl = ctx.netlist
    for name, net in sorted(nl.nets.items()):
        bare = name.rsplit("/", 1)[-1]
        is_i2c = bool(_I2C.search(bare))
        if not (is_i2c or _OPEN_DRAIN.search(bare)):
            continue
        pullups = [r for r, other in ctx.parts_bridging(name, "res").items() if nl.is_power(other)]
        if pullups:
            if is_i2c and len(pullups) > 1:
                out.append(Finding(
                    "multiple-pullups", "info", f"'{name}' has {len(pullups)} pull-ups in parallel ({', '.join(pullups)}).",
                    refs=pullups, nets=[name], evidence="resistors from the net to a supply",
                    fix="Check the combined pull-up value is still within the bus spec.",
                ))
            continue
        has_conn = any(ctx.kind(n.ref) == "conn" for n in net.nodes)
        any_res = list(ctx.parts_bridging(name, "res"))
        what = "I2C line" if is_i2c else "open-drain/status line"
        sev = "info" if has_conn else "warning"
        note = " It goes to a connector, so the pull-up may live on the external module." if has_conn else ""
        out.append(Finding(
            "missing-pullup", sev, f"{what} '{name}' has no pull-up resistor to a supply.{note}",
            refs=sorted({n.ref for n in net.nodes}, key=_ref_key), nets=[name],
            evidence=f"pins: {', '.join(f'{n.ref}.{n.pin}' for n in net.nodes)}; series/other resistors: {any_res or 'none'}",
            fix=f"Add a pull-up (I2C: 2.2k-4.7k; open-drain: 10k) from {name} to the logic supply.",
        ))
    return out


def rule_crystal_load_caps(ctx: Context) -> list[Finding]:
    out = []
    nl = ctx.netlist
    for ref in sorted(nl.real_components(), key=_ref_key):
        if ctx.kind(ref) != "crystal":
            continue
        for pin, net in sorted(nl.nets_of(ref).items()):
            if nl.is_ground(net) or net.startswith("unconnected"):
                continue
            caps = [c for c, other in ctx.parts_bridging(net, "cap").items() if nl.is_ground(other)]
            if not caps:
                out.append(Finding(
                    "crystal-load-cap", "warning", f"Crystal {ref} pin {pin} ('{net}') has no load capacitor to ground.",
                    refs=[ref], nets=[net], evidence=f"caps from {net} to GND: none",
                    fix="Add a load capacitor sized from the crystal's CL (C ~ 2*(CL - Cstray)).",
                ))
    return out


def rule_led_current_limit(ctx: Context) -> list[Finding]:
    out = []
    nl = ctx.netlist
    for ref in sorted(nl.real_components(), key=_ref_key):
        if ctx.kind(ref) != "led":
            continue
        nets = nl.nets_of(ref)
        has_r = any(ctx.parts_bridging(net, "res") for net in nets.values() if net in nl.nets)
        if not has_r:
            out.append(Finding(
                "led-no-resistor", "warning", f"LED {ref} has no resistor on either terminal.",
                refs=[ref], nets=sorted(nets.values()), evidence=f"nets: {nets}",
                fix="Add a series resistor unless it is driven by a current-limited driver.",
            ))
    return out


def rule_usb_c_cc(ctx: Context) -> list[Finding]:
    """A USB-C sink/device needs 5.1k pull-downs on CC1 and CC2."""
    out = []
    nl = ctx.netlist
    for ref, comp in sorted(nl.real_components().items()):
        if "usb_c" not in comp.lib_id.lower():
            continue
        for (r, pin), func in nl.pin_functions.items():
            if r != ref or not re.fullmatch(r"CC[12]", func or ""):
                continue
            net = nl.pin_to_net().get((r, pin))
            if not net:
                continue
            rd = {res: other for res, other in ctx.parts_bridging(net, "res").items() if nl.is_ground(other)}
            values = [nl.components[x].value for x in rd]
            if not rd:
                out.append(Finding(
                    "usb-c-cc", "error", f"{ref} {func} has no pull-down to GND; a USB-C source will not supply power.",
                    refs=[ref], nets=[net], evidence=f"{ref}.{pin} ({func}) on {net}",
                    fix=f"Add a 5.1k resistor from {func} to GND (one per CC pin, not shared).",
                ))
            elif not any(re.match(r"5[.,]?1\s*k|5k1", v, re.I) for v in values):
                out.append(Finding(
                    "usb-c-cc", "warning", f"{ref} {func} pull-down is {', '.join(values)}, not 5.1k.",
                    refs=[ref, *rd], nets=[net], evidence=f"{list(rd)} = {values}", fix="Use 5.1k for a sink.",
                ))
    return out


def _norm_net(name: str) -> str:
    s = name.rsplit("/", 1)[-1].lower()
    s = s[1:] if s.startswith("+") else s  # "+3.3V" ~ "3V3"; a trailing +/- is polarity (D+/D-) and stays
    s = s.replace("_", "").replace(" ", "")
    s = re.sub(r"(\d)\.(\d+)v", r"\1v\2", s)  # 3.3v -> 3v3
    s = re.sub(r"(\d)v0?$", r"\1v", s)
    return s


def rule_similar_net_names(ctx: Context) -> list[Finding]:
    groups: dict[str, list[str]] = defaultdict(list)
    for name, net in ctx.netlist.nets.items():
        if not net.is_auto_named:
            groups[_norm_net(name)].append(name)
    return [
        Finding(
            "similar-net-names", "warning",
            f"Nets {', '.join(sorted(names))} look like the same signal but are NOT connected.",
            nets=sorted(names), evidence="names differ only in punctuation/case/format",
            fix="Rename to one spelling if they should be the same net; otherwise rename one to make the difference obvious.",
        )
        for names in groups.values() if len(names) > 1
    ]


def rule_single_pin_named_nets(ctx: Context) -> list[Finding]:
    out = []
    for name, net in sorted(ctx.netlist.nets.items()):
        if net.is_auto_named or len(net.nodes) != 1 or ctx.netlist.is_ground(name) or ctx.netlist.is_power(name):
            continue
        n = net.nodes[0]
        out.append(Finding(
            "single-pin-net", "warning", f"Net '{name}' reaches only {n.ref}.{n.pin}; its label may be misspelled or unconnected elsewhere.",
            refs=[n.ref], nets=[name], evidence=f"only node: {n.ref}.{n.pin}",
            fix="Check the label spelling at the other end, or remove the label.",
        ))
    return out


_SKIP_BOM = re.compile(r"^(H|MH|TP|FID|LOGO|G)\d", re.I)


def _is_pad_or_hardware(c) -> bool:
    """Bare copper pads (TestPoint / SolderWirePad) and mounting holes aren't purchasable parts."""
    lib = c.lib_id.lower()
    return "testpoint" in lib or "mountinghole" in lib or "solderwirepad" in c.footprint.lower()


def rule_bom_fields(ctx: Context) -> list[Finding]:
    out = []
    no_fp, no_value, unannotated, no_mpn = [], [], [], []
    for ref, c in sorted(ctx.netlist.real_components().items(), key=lambda kv: _ref_key(kv[0])):
        if "?" in ref:
            unannotated.append(ref)
        if _SKIP_BOM.match(ref) or _is_pad_or_hardware(c):
            continue
        if not c.footprint:
            no_fp.append(ref)
        if not c.value or c.value in ("R", "C", "L", "D", "~"):
            no_value.append(ref)
        keys = {k.lower() for k, v in c.fields.items() if v}
        if not keys & {"mpn", "lcsc", "lcsc part", "jlcpcb part", "manufacturer part number", "mfr part", "digikey", "partnumber"}:
            no_mpn.append(ref)
    if unannotated:
        out.append(Finding("unannotated", "error", f"{len(unannotated)} parts are not annotated.", refs=unannotated,
                           fix="Run annotate_schematic (or Tools > Annotate)."))
    if no_fp:
        out.append(Finding("missing-footprint", "error", f"{len(no_fp)} parts have no footprint.", refs=no_fp,
                           fix="Assign footprints before updating the PCB."))
    if no_value:
        out.append(Finding("missing-value", "warning", f"{len(no_value)} parts have no real value.", refs=no_value))
    if no_mpn:
        out.append(Finding("missing-mpn", "info",
                           f"{len(no_mpn)} of {len(ctx.netlist.real_components())} parts have no MPN/LCSC field, so the BOM cannot be ordered as-is.",
                           refs=no_mpn[:40], fix="Add MPN or LCSC fields (the engine's JLCPCB search can suggest parts)."))
    return out


def rule_erc(ctx: Context) -> list[Finding]:
    if ctx.erc is None:
        return []
    out = []
    by_type: dict[str, list[checks.Violation]] = defaultdict(list)
    for v in ctx.erc:
        if v.severity == "error":
            out.append(Finding(f"erc:{v.type}", "error", v.description, refs=v.refs, evidence="; ".join(v.items)))
        elif v.severity == "warning":
            by_type[v.type].append(v)
    noisy = {"endpoint_off_grid", "lib_symbol_issues", "footprint_link_issues", "lib_symbol_mismatch"}
    for typ, vs in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        refs = sorted({r for v in vs for r in v.refs}, key=_ref_key)
        examples = "; ".join(f"{v.description} ({', '.join(v.items)})" for v in vs[:3])
        out.append(Finding(f"erc:{typ}", "info" if typ in noisy else "warning",
                           f"ERC: {len(vs)}x {typ.replace('_', ' ')}", refs=refs[:30], evidence=examples))
    return out


def rule_drc(ctx: Context) -> list[Finding]:
    if ctx.drc is None:
        return []
    out = []
    groups: dict[tuple, list[checks.Violation]] = defaultdict(list)
    for v in ctx.drc:
        groups[(v.source, v.type, v.severity)].append(v)
    for (src, typ, sev), vs in sorted(groups.items(), key=lambda kv: (SEVERITY_ORDER.get(kv[0][2], 3), -len(kv[1]))):
        refs = sorted({r for v in vs for r in v.refs}, key=_ref_key)
        if src == "parity":
            sev_out, msg = "error", f"Schematic and PCB disagree: {len(vs)}x {typ.replace('_', ' ')}"
            fix = "Fix the schematic first if it is wrong, then Update PCB from Schematic (F8)."
        elif src == "unconnected":
            sev_out, msg, fix = "error", f"{len(vs)} unrouted connections on the PCB", "Route them or check the ratsnest."
        else:
            sev_out = "error" if sev == "error" else "warning"
            msg, fix = f"DRC: {len(vs)}x {typ.replace('_', ' ')}", ""
        examples = "; ".join(f"{v.description} ({', '.join(v.items)})" for v in vs[:3])
        out.append(Finding(f"{src}:{typ}", sev_out, msg, refs=refs[:30], evidence=examples, fix=fix))
    return out


def rule_fab(ctx: Context) -> list[Finding]:
    """Compare the board's actual geometry and the project's DRC limits with the fab profile."""
    if not ctx.fab or not ctx.project.pcb.exists():
        return []
    fab, out = ctx.fab, []
    text = ctx.project.pcb.read_text(encoding="utf-8", errors="replace")
    widths = [float(w) for w in re.findall(r"\(segment\b.*?\(width ([\d.]+)\)", text, re.S)]
    vias = [(float(s), float(d)) for s, d in
            re.findall(r"\(via\b(?:(?!\(via\b).)*?\(size ([\d.]+)\).*?\(drill ([\d.]+)\)", text, re.S)]
    thin = sorted({w for w in widths if w < fab["min_track_mm"] - 1e-6})
    if thin:
        n = sum(1 for w in widths if w < fab["min_track_mm"] - 1e-6)
        out.append(Finding("fab-track-width", "error",
                           f"{n} track segments are narrower than {fab['name']}'s {fab['min_track_mm']} mm minimum (widths {thin}).",
                           evidence=f"{len(widths)} segments scanned", fix="Widen them or move to a fab tier that supports them."))
    small_drill = [d for _, d in vias if d < fab["min_via_drill_mm"] - 1e-6]
    if small_drill:
        out.append(Finding("fab-via-drill", "error",
                           f"{len(small_drill)} vias have drills below {fab['min_via_drill_mm']} mm (smallest {min(small_drill)}).",
                           fix="Use a larger via or the fab's advanced via option."))
    thin_ring = [(s, d) for s, d in vias if (s - d) / 2 < fab["min_annular_ring_mm"] - 1e-6]
    if thin_ring:
        worst = min((s - d) / 2 for s, d in thin_ring)
        out.append(Finding("fab-annular-ring", "error",
                           f"{len(thin_ring)} vias have an annular ring below {fab['min_annular_ring_mm']} mm (worst {worst:.3f} mm).",
                           fix="Increase via pad size or reduce drill."))
    # Project DRC limits looser than the fab means KiCad's DRC can't catch fab violations.
    try:
        rules = json.loads(ctx.project.pro.read_text(encoding="utf-8"))["board"]["design_settings"]["rules"]
    except (OSError, KeyError, ValueError):
        rules = {}
    loose = []
    for key, fab_key in (("min_clearance", "min_clearance_mm"), ("min_track_width", "min_track_mm"),
                         ("min_via_annular_width", "min_annular_ring_mm"), ("min_through_hole_diameter", "min_via_drill_mm")):
        if key in rules and rules[key] + 1e-6 < fab[fab_key]:
            loose.append(f"{key}={rules[key]} < {fab[fab_key]}")
    if loose:
        out.append(Finding("fab-drc-limits", "warning",
                           f"The project's DRC limits are looser than {fab['name']}, so DRC can pass a board the fab rejects.",
                           evidence="; ".join(loose), fix="Board Setup > Design Rules > Constraints: raise them to the fab minimums."))
    return out


RULES = [
    rule_missing_sheet_files, rule_off_page, rule_cap_only_nets, rule_decoupling, rule_pullups, rule_crystal_load_caps,
    rule_led_current_limit, rule_usb_c_cc, rule_similar_net_names, rule_single_pin_named_nets,
    rule_bom_fields, rule_erc, rule_drc, rule_fab,
]


# =================================================================== running
def load_fab_profile(name: str | None) -> dict | None:
    if not name:
        return None
    path = FAB_PROFILES / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"No fab profile {name!r}; available: {[p.stem for p in FAB_PROFILES.glob('*.json')]}")
    return json.loads(path.read_text())


def run(ctx: Context) -> list[Finding]:
    findings = []
    for rule in RULES:
        try:
            findings.extend(rule(ctx))
        except Exception as exc:  # one broken rule must not hide the others
            findings.append(Finding(f"internal:{rule.__name__}", "info", f"Rule crashed: {exc!r}"))
    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.rule))


def verdict(findings: list[Finding]) -> dict:
    counts = {s: sum(1 for f in findings if f.severity == s) for s in SEVERITY_ORDER}
    return {"pass": counts["error"] == 0, "counts": counts}


def render_markdown(project: Project, findings: list[Finding], extra: dict) -> str:
    v = verdict(findings)
    lines = [
        f"# Design review: {project.name}",
        "",
        f"_Generated {datetime.now():%Y-%m-%d %H:%M} by AI PCB Copilot (measured checks)._",
        "",
        f"**Verdict: {'PASS' if v['pass'] else 'FAIL'}**: {v['counts']['error']} errors, "
        f"{v['counts']['warning']} warnings, {v['counts']['info']} notes.",
        "",
    ]
    for k, val in extra.items():
        lines.append(f"- {k}: {val}")
    lines.append("")
    for sev in ("error", "warning", "info"):
        group = [f for f in findings if f.severity == sev]
        if not group:
            continue
        lines += [f"## {sev.capitalize()}s ({len(group)})", ""]
        for f in group:
            lines.append(f"- **{f.rule}**: {f.message}")
            if f.refs:
                lines.append(f"  - refs: {', '.join(f.refs)}")
            if f.nets:
                lines.append(f"  - nets: {', '.join(f.nets)}")
            if f.evidence:
                lines.append(f"  - evidence: {f.evidence}")
            if f.fix:
                lines.append(f"  - fix: {f.fix}")
        lines.append("")
    return "\n".join(lines)

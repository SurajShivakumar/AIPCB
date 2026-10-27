"""Netlist model built from kicad-cli's XML export, plus a pin-level diff.

kicad-cli is the ground truth for connectivity: it resolves power symbols,
global/hierarchical labels and buses exactly as KiCad does, which the engine's
own file parser does not (it misses power-symbol nets).
"""

from __future__ import annotations

import re
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .kicad import export_netlist_xml

Pin = tuple[str, str]  # (ref, pin number)

GROUND_RE = re.compile(r"^(/)?(A|D|P|S)?GND[A-Z0-9_]*$|^VSS[A-Z0-9_]*$|^0V$|^EARTH$", re.I)
POWER_RE = re.compile(
    r"^(/)?(\+|-)?\d+(\.\d+)?V\d*[A-Z0-9_]*$|^(/)?\+?(VCC|VDD|VBUS|VBAT|VIN|VM|VSYS|V_?[A-Z0-9]+|\d+V\d+)[A-Z0-9_]*$",
    re.I,
)


@dataclass
class Component:
    ref: str
    value: str
    footprint: str
    lib_id: str
    description: str = ""
    datasheet: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    sheet: str = "/"
    dnp: bool = False

    @property
    def prefix(self) -> str:
        return re.match(r"[A-Za-z_#]*", self.ref).group(0)


@dataclass
class Node:
    ref: str
    pin: str
    pin_function: str
    pin_type: str


@dataclass
class Net:
    name: str
    nodes: list[Node]

    @property
    def pins(self) -> frozenset[Pin]:
        return frozenset((n.ref, n.pin) for n in self.nodes)

    @property
    def is_auto_named(self) -> bool:
        return self.name.startswith(("Net-(", "unconnected-("))


@dataclass
class Netlist:
    components: dict[str, Component]
    nets: dict[str, Net]
    pin_types: dict[Pin, str]
    pin_functions: dict[Pin, str]
    sheets: list[dict]

    # ---------------------------------------------------------------- loading
    @classmethod
    def from_xml(cls, path: Path) -> "Netlist":
        root = ET.parse(path).getroot()
        components: dict[str, Component] = {}
        for comp in root.iterfind("components/comp"):
            src = comp.find("libsource")
            fields = {f.get("name"): (f.text or "") for f in comp.iterfind("fields/field")}
            props = {p.get("name"): p.get("value", "") for p in comp.iterfind("property")}
            sheet = comp.find("sheetpath")
            components[comp.get("ref")] = Component(
                ref=comp.get("ref"),
                value=comp.findtext("value", ""),
                footprint=comp.findtext("footprint", ""),
                lib_id=f"{src.get('lib')}:{src.get('part')}" if src is not None else "",
                description=comp.findtext("description", ""),
                datasheet=comp.findtext("datasheet", ""),
                fields=fields,
                sheet=sheet.get("names", "/") if sheet is not None else "/",
                dnp="dnp" in props,
            )
        nets: dict[str, Net] = {}
        pin_types: dict[Pin, str] = {}
        pin_functions: dict[Pin, str] = {}
        for net in root.iterfind("nets/net"):
            nodes = []
            for n in net.iterfind("node"):
                node = Node(n.get("ref"), n.get("pin"), n.get("pinfunction", ""), n.get("pintype", ""))
                nodes.append(node)
                pin_types[(node.ref, node.pin)] = node.pin_type
                pin_functions[(node.ref, node.pin)] = node.pin_function
            nets[net.get("name")] = Net(net.get("name"), nodes)
        sheets = [
            {"name": s.get("name"), "source": s.findtext("title_block/source", "")}
            for s in root.iterfind("design/sheet")
        ]
        return cls(components, nets, pin_types, pin_functions, sheets)

    @classmethod
    def from_schematic(cls, sch: Path, cache_dir: Path | None = None) -> "Netlist":
        if cache_dir is None:
            with tempfile.TemporaryDirectory() as tmp:
                return cls.from_xml(export_netlist_xml(sch, Path(tmp) / "netlist.xml"))
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cls.from_xml(export_netlist_xml(sch, cache_dir / "netlist.xml"))

    # ---------------------------------------------------------------- queries
    def net_of(self, ref: str, pin: str) -> Net | None:
        name = self.pin_to_net().get((ref, pin))
        return self.nets[name] if name else None

    def pin_to_net(self) -> dict[Pin, str]:
        cached = self.__dict__.get("_pin_to_net")
        if cached is None:
            cached = {p: name for name, net in self.nets.items() for p in net.pins}
            self.__dict__["_pin_to_net"] = cached
        return cached

    def nets_of(self, ref: str) -> dict[str, str]:
        """pin -> net name for one component."""
        return {pin: name for (r, pin), name in self.pin_to_net().items() if r == ref}

    def real_components(self) -> dict[str, Component]:
        """Components that end up on the board (skips power symbols / flags)."""
        return {r: c for r, c in self.components.items() if not r.startswith("#")}

    @staticmethod
    def is_ground(name: str) -> bool:
        return bool(GROUND_RE.match(name.lstrip("/")))

    @staticmethod
    def is_power(name: str) -> bool:
        bare = name.lstrip("/")
        return not Netlist.is_ground(bare) and bool(POWER_RE.match(bare))

    def summary(self, max_nets: int = 400) -> str:
        """Compact text digest for an LLM: parts, then every net with its pins."""
        lines = [f"{len(self.real_components())} components, {len(self.nets)} nets"]
        for ref in sorted(self.real_components(), key=_ref_key):
            c = self.components[ref]
            lines.append(f"  {ref}: {c.value} [{c.lib_id}] fp={c.footprint or '-'}")
        lines.append("Nets:")
        for name, net in sorted(self.nets.items())[:max_nets]:
            pins = ", ".join(
                f"{n.ref}.{n.pin}" + (f"({n.pin_function})" if n.pin_function else "") for n in net.nodes
            )
            lines.append(f"  {name}: {pins}")
        return "\n".join(lines)


def _ref_key(ref: str):
    m = re.match(r"([A-Za-z_#]*)(\d*)", ref)
    return (m.group(1), int(m.group(2) or 0), ref)


# ------------------------------------------------------------------ diffing
@dataclass
class NetlistDiff:
    components_added: list[str]
    components_removed: list[str]
    components_changed: list[dict]
    nets_added: list[dict]
    nets_removed: list[dict]
    nets_renamed: list[dict]
    pins_connected: list[dict]
    pins_disconnected: list[dict]
    nets_merged: list[dict]
    nets_split: list[dict]
    similarity: float

    @property
    def empty(self) -> bool:
        return not any(
            [self.components_added, self.components_removed, self.components_changed, self.nets_added,
             self.nets_removed, self.nets_renamed, self.pins_connected, self.pins_disconnected,
             self.nets_merged, self.nets_split]
        )

    def touched_refs(self) -> set[str]:
        refs = set(self.components_added) | set(self.components_removed)
        refs |= {c["ref"] for c in self.components_changed}
        for item in self.pins_connected + self.pins_disconnected:
            refs.add(item["pin"].split(".")[0])
        merged_away = {na for m in self.nets_merged for na in m.get("absorbed", [])}
        for n in self.nets_added + self.nets_removed:
            if n["name"] not in merged_away:  # merges report their own refs
                refs |= {p.split(".")[0] for p in n["pins"]}
        for m in self.nets_merged + self.nets_split:
            refs |= set(m.get("refs", []))
        return {r.rstrip(REPLACED_SUFFIX) for r in refs}

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if v}
        d["similarity"] = round(self.similarity, 4)
        d["touched_refs"] = sorted(self.touched_refs(), key=_ref_key)
        return d

    def text(self) -> str:
        if self.empty:
            return "No connectivity or component changes."
        out = []
        for r in self.components_added:
            out.append(f"+ component {r}")
        for r in self.components_removed:
            out.append(f"- component {r}")
        for c in self.components_changed:
            out.append(f"~ component {c['ref']}: " + ", ".join(f"{k} {v[0]!r} -> {v[1]!r}" for k, v in c["changes"].items()))
        for n in self.nets_added:
            out.append(f"+ net {n['name']}: {', '.join(n['pins'])}")
        merged_away = {na for m in self.nets_merged for na in m.get("absorbed", [])}
        for n in self.nets_removed:
            if n["name"] not in merged_away:
                out.append(f"- net {n['name']}: {', '.join(n['pins'])}")
        for n in self.nets_renamed:
            out.append(f"~ net renamed {n['from']} -> {n['to']}")
        for m in self.nets_merged:
            out.append(f"! {', '.join(m['absorbed'])} merged into {m['into']} (parts: {', '.join(m['refs'])})")
        for s in self.nets_split:
            out.append(f"! net {s['from']} split into: {', '.join(s['into'])}")
        for p in self.pins_connected:
            out.append(f"+ {p['pin']} joined {p['net']}")
        for p in self.pins_disconnected:
            out.append(f"- {p['pin']} left {p['net']}")
        out.append(f"(pin-pair similarity {self.similarity:.3f})")
        return "\n".join(out)


REPLACED_SUFFIX = "'"


def _rename_refs(nl: Netlist, mapping: dict[str, str]) -> Netlist:
    comps = {}
    for ref, c in nl.components.items():
        new = mapping.get(ref, ref)
        comps[new] = Component(**{**c.__dict__, "ref": new})
    nets = {
        name: Net(name, [Node(mapping.get(n.ref, n.ref), n.pin, n.pin_function, n.pin_type) for n in net.nodes])
        for name, net in nl.nets.items()
    }
    remap = lambda p: (mapping.get(p[0], p[0]), p[1])  # noqa: E731
    return Netlist(comps, nets, {remap(p): t for p, t in nl.pin_types.items()},
                   {remap(p): f for p, f in nl.pin_functions.items()}, nl.sheets)


def _significant(net: Net) -> bool:
    return len(net.pins) > 1 or not net.is_auto_named


def diff(a: Netlist, b: Netlist) -> NetlistDiff:
    """What changed from a to b, independent of auto-generated net names.

    Nets are matched by shared pins, so a net KiCad renames because a
    reference changed is not reported as removed and re-added.

    A reference reused for a different part (lib_id changed) is treated as
    the old part removed and a new part added: its pins mean different
    things now, so matching them would invent merges and splits.
    """
    ca, cb = a.real_components(), b.real_components()
    replaced = {r for r in set(ca) & set(cb) if ca[r].lib_id != cb[r].lib_id}
    if replaced:
        b = _rename_refs(b, {r: r + REPLACED_SUFFIX for r in replaced})
        cb = b.real_components()
    added = sorted(set(cb) - set(ca), key=_ref_key)
    removed = sorted(set(ca) - set(cb), key=_ref_key)
    changed = []
    for ref in sorted(set(ca) & set(cb), key=_ref_key):
        x, y = ca[ref], cb[ref]
        changes = {}
        for attr in ("value", "footprint", "lib_id", "dnp"):
            if getattr(x, attr) != getattr(y, attr):
                changes[attr] = (getattr(x, attr), getattr(y, attr))
        for k in sorted(set(x.fields) | set(y.fields)):
            if k not in ("Footprint", "Datasheet", "Description") and x.fields.get(k) != y.fields.get(k):
                changes[f"field:{k}"] = (x.fields.get(k), y.fields.get(k))
        if changes:
            changed.append({"ref": ref, "changes": changes})

    nets_a = {n: net for n, net in a.nets.items() if _significant(net)}
    nets_b = {n: net for n, net in b.nets.items() if _significant(net)}
    # Only pins that exist on both sides say anything about rewiring.
    common = {p for net in nets_a.values() for p in net.pins} & {p for net in nets_b.values() for p in net.pins}

    # Overlap matrix on common pins.
    owner_b = {p: n for n, net in nets_b.items() for p in net.pins}
    overlaps: dict[str, dict[str, int]] = {}
    for na, net in nets_a.items():
        row: dict[str, int] = {}
        for p in net.pins & common:
            nb = owner_b.get(p)
            if nb:
                row[nb] = row.get(nb, 0) + 1
        overlaps[na] = row

    # Greedy one-to-one matching, largest overlap first; equal names win ties.
    pairs = sorted(
        ((cnt, na == nb, na, nb) for na, row in overlaps.items() for nb, cnt in row.items()),
        reverse=True,
    )
    match_ab: dict[str, str] = {}
    used_b: set[str] = set()
    for cnt, _, na, nb in pairs:
        if na not in match_ab and nb not in used_b:
            match_ab[na] = nb
            used_b.add(nb)
    for na in nets_a:  # nets with no common pins can still match by name
        if na not in match_ab and na in nets_b and na not in used_b:
            match_ab[na] = na
            used_b.add(na)

    renamed = [
        {"from": na, "to": nb}
        for na, nb in match_ab.items()
        if na != nb and not (nets_a[na].is_auto_named and nets_b[nb].is_auto_named)
    ]

    match_ba = {nb: na for na, nb in match_ab.items()}
    def substantial(na: str, nb: str) -> bool:
        # One pin hopping nets is a moved pin (reported below), not a merge or split.
        shared = overlaps[na].get(nb, 0)
        return shared >= 2

    merged = []
    for nb in nets_b:
        sources = [na for na, row in overlaps.items() if row.get(nb) and substantial(na, nb)]
        if len(sources) > 1:
            # Parts that actually changed net: those on the nets absorbed into nb.
            absorbed = [na for na in sources if na != match_ba.get(nb)]
            refs = sorted({p[0] for na in absorbed for p in nets_a[na].pins}, key=_ref_key)
            merged.append({"into": nb, "from": sorted(sources), "absorbed": absorbed, "refs": refs})
    split = []
    for na, row in overlaps.items():
        if sum(1 for nb in row if substantial(na, nb)) > 1:
            kept = match_ab.get(na)
            refs = sorted({p[0] for nb in row if nb != kept for p in nets_b[nb].pins & nets_a[na].pins}, key=_ref_key)
            split.append({"from": na, "into": sorted(row), "refs": refs})

    connected, disconnected = [], []
    for na, nb in match_ab.items():
        pa, pb = nets_a[na].pins, nets_b[nb].pins
        for p in sorted(pb - pa):
            if p[0] in ca and p[0] in cb:
                connected.append({"pin": f"{p[0]}.{p[1]}", "net": nb})
        for p in sorted(pa - pb):
            if p[0] in ca and p[0] in cb:
                disconnected.append({"pin": f"{p[0]}.{p[1]}", "net": na})
    # Pins of new parts are reported via the nets they joined.
    for na, nb in match_ab.items():
        for p in sorted(nets_b[nb].pins - nets_a[na].pins):
            if p[0] in added:
                connected.append({"pin": f"{p[0]}.{p[1]}", "net": nb})

    nets_added = [
        {"name": nb, "pins": [f"{r}.{p}" for r, p in sorted(net.pins)]}
        for nb, net in nets_b.items()
        if nb not in used_b
    ]
    nets_removed = [
        {"name": na, "pins": [f"{r}.{p}" for r, p in sorted(net.pins)]}
        for na, net in nets_a.items()
        if na not in match_ab
    ]
    return NetlistDiff(
        components_added=added,
        components_removed=removed,
        components_changed=changed,
        nets_added=nets_added,
        nets_removed=nets_removed,
        nets_renamed=renamed,
        pins_connected=connected,
        pins_disconnected=disconnected,
        nets_merged=merged,
        nets_split=split,
        similarity=pin_pair_similarity(a, b),
    )


def pin_pair_similarity(a: Netlist, b: Netlist) -> float:
    """F1 over unordered pin pairs sharing a net (name-independent), as in pcb-ai's bench metric.

    Large nets are capped so a 75-pin GND net does not swamp everything else.
    """

    def pairs(nl: Netlist) -> set[tuple[Pin, Pin]]:
        out = set()
        for net in nl.nets.values():
            pins = sorted(net.pins)
            if len(pins) > 60:  # star-connect big nets to a canonical pin
                out |= {(pins[0], p) for p in pins[1:]}
            else:
                out |= {(p, q) for i, p in enumerate(pins) for q in pins[i + 1 :]}
        return out

    pa, pb = pairs(a), pairs(b)
    if not pa and not pb:
        return 1.0
    tp = len(pa & pb)
    return 2 * tp / (len(pa) + len(pb))

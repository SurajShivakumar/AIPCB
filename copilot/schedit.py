"""Precise, style-consistent schematic edits that the engine's auto-connect can't do.

The engine's connect_to_net drops a text label on every pin and can draw
diagonal wires. Hand-drawn KiCad schematics use power symbols (GND, +3.3V...)
for rails and short orthogonal wires. This module writes exactly that:

  * pin_endpoints(sch, ref)        exact pin end coordinates of a placed symbol
  * add_power_symbol(...)          real power:* symbol (lib cache added if needed)
  * add_wire(...)                  straight wire, rejects diagonals and off-grid points
  * add_junction(...)              junction dot
  * remove_items_at(...)           remove labels/wires/flags touching given points

All coordinates are mm. Points must be on the 1.27 mm (50 mil) grid.
"""

from __future__ import annotations

import math
import re
import uuid as uuidlib
from pathlib import Path

GRID = 1.27
KICAD_SYMBOLS = [Path(r"C:\Program Files\KiCad\9.0\share\kicad\symbols"),
                 Path(r"C:\Program Files\KiCad\10.0\share\kicad\symbols")]


class SchEditError(RuntimeError):
    pass


def _uuid() -> str:
    return str(uuidlib.uuid4())


def _fmt(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def on_grid(v: float) -> bool:
    return abs(v / GRID - round(v / GRID)) < 1e-3


def _check_grid(*pts: tuple[float, float]) -> None:
    for x, y in pts:
        if not (on_grid(x) and on_grid(y)):
            raise SchEditError(f"Point ({x}, {y}) is off the 1.27 mm grid.")


class Schematic:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.text = self.path.read_text(encoding="utf-8")

    def save(self) -> None:
        bal = self.text.count("(") - self.text.count(")")
        if bal != 0:
            raise SchEditError(f"Refusing to save: unbalanced parentheses ({bal}).")
        self.path.write_text(self.text, encoding="utf-8", newline="")

    # ------------------------------------------------------------ structure
    def _lib_section(self) -> tuple[int, int]:
        s = self.text.find("\n\t(lib_symbols")
        e = self.text.find("\n\t)", s)
        return s, e

    def lib_symbol(self, lib_id: str) -> str | None:
        s, e = self._lib_section()
        m = re.search(rf'\n\t\t\(symbol "{re.escape(lib_id)}"\n', self.text[s:e])
        if not m:
            return None
        start = s + m.start()
        end = self.text.index("\n\t\t)", start) + 4
        return self.text[start:end]

    def ensure_lib_symbol(self, lib_id: str) -> str:
        """Make sure lib_symbols caches lib_id (copied from KiCad's stock library)."""
        block = self.lib_symbol(lib_id)
        if block:
            return block
        lib, _, name = lib_id.partition(":")
        src = next((d / f"{lib}.kicad_sym" for d in KICAD_SYMBOLS if (d / f"{lib}.kicad_sym").exists()), None)
        if not src:
            raise SchEditError(f"Library {lib} not found")
        lt = src.read_text(encoding="utf-8")
        m = re.search(rf'\n\t\(symbol "{re.escape(name)}"\n', lt)
        if not m:
            raise SchEditError(f"{lib_id} not in {src.name}")
        body = lt[m.start(): lt.index("\n\t)", m.start()) + 3]
        if "(extends" in body:
            raise SchEditError(f"{lib_id} extends another symbol; place it with the engine instead.")
        body = body.replace(f'(symbol "{name}"', f'(symbol "{lib_id}"', 1)
        body = "\n".join("\t" + line if line else line for line in body.split("\n"))
        s, e = self._lib_section()
        self.text = self.text[:e] + body + self.text[e:]
        return self.lib_symbol(lib_id)

    def root_uuid(self) -> str:
        return re.search(r'\n\t\(uuid "([^"]+)"\)', self.text).group(1)

    def project_name(self) -> str:
        m = re.search(r'\(instances\n\t\t\t\(project "([^"]+)"', self.text)
        return m.group(1) if m else self.path.stem

    def _symbol_blocks(self):
        s, e = self._lib_section()
        e2 = self.text.find("\n\t)", s) + 3
        for m in re.finditer(r"\n\t\(symbol\n.*?\n\t\)", self.text[e2:], re.S):
            yield e2 + m.start(), e2 + m.end(), m.group(0)

    def placed(self, ref: str) -> dict:
        for _, _, b in self._symbol_blocks():
            if re.search(rf'\(property "Reference" "{re.escape(ref)}"', b):
                lib_id = re.search(r'\(lib_id "([^"]+)"\)', b).group(1)
                x, y, rot = (float(v) for v in re.search(r"\n\t\t\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)", b).groups())
                mirror = re.search(r"\n\t\t\(mirror (\w)\)", b)
                unit = int(re.search(r"\(unit (\d+)\)", b).group(1))
                return {"lib_id": lib_id, "x": x, "y": y, "rot": rot, "mirror": mirror.group(1) if mirror else None,
                        "unit": unit}
        raise SchEditError(f"No symbol {ref}")

    def next_ref(self, prefix: str) -> str:
        found = re.findall(rf'\((?:reference|property "Reference") "{re.escape(prefix)}(\d+)"', self.text)
        width = max((len(n) for n in found), default=3)  # match the file's existing numbering style
        nxt = max((int(n) for n in found), default=0) + 1
        return f"{prefix}{nxt:0{width}d}"


# ------------------------------------------------------------------ pins
def _lib_pins(block: str, unit: int) -> list[dict]:
    pins = []
    for m in re.finditer(r'\n\t\t\t\(symbol "[^"]*_(\d+)_(\d+)"(.*?)\n\t\t\t\)', block, re.S):
        u = int(m.group(1))
        if u not in (0, unit):
            continue
        for p in re.finditer(r"\(pin \w+ \w+\s*\n\s*\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)\s*\n\s*\(length ([-\d.]+)\).*?"
                             r'\(name "([^"]*)".*?\(number "([^"]*)"', m.group(3), re.S):
            x, y, a, length = (float(v) for v in p.groups()[:4])
            pins.append({"number": p.group(6), "name": p.group(5), "x": x, "y": y, "angle": a, "length": length})
    return pins


def pin_endpoints(sch: Schematic, ref: str) -> dict[str, tuple[float, float]]:
    """Schematic coordinates of each pin's connection point (the pin's outer end)."""
    sym = sch.placed(ref)
    block = sch.lib_symbol(sym["lib_id"])
    if block is None:
        raise SchEditError(f"lib symbol {sym['lib_id']} not cached")
    out = {}
    th = math.radians(sym["rot"])
    for p in _lib_pins(block, sym["unit"]):
        # symbol space is y-up; schematic is y-down
        x, y = p["x"], -p["y"]
        if sym["mirror"] == "y":
            x = -x
        elif sym["mirror"] == "x":
            y = -y
        rx = x * math.cos(th) + y * math.sin(th)
        ry = -x * math.sin(th) + y * math.cos(th)
        out[p["number"]] = (round(sym["x"] + rx, 4), round(sym["y"] + ry, 4))
    return out


# ------------------------------------------------------------------ writers
def _insert(sch: Schematic, block: str) -> None:
    anchor = sch.text.rfind("\n\t(sheet_instances")
    if anchor == -1:
        anchor = sch.text.rfind("\n)")
    sch.text = sch.text[:anchor] + block + sch.text[anchor:]


def add_wire(sch: Schematic, a: tuple[float, float], b: tuple[float, float]) -> None:
    _check_grid(a, b)
    if abs(a[0] - b[0]) > 1e-6 and abs(a[1] - b[1]) > 1e-6:
        raise SchEditError(f"Diagonal wire {a}->{b} refused; route with two orthogonal segments.")
    if a == b:
        return
    _insert(sch, f"\n\t(wire\n\t\t(pts\n\t\t\t(xy {_fmt(a[0])} {_fmt(a[1])}) (xy {_fmt(b[0])} {_fmt(b[1])})\n\t\t)\n"
                 f"\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type default)\n\t\t)\n\t\t(uuid \"{_uuid()}\")\n\t)")


def add_path(sch: Schematic, pts: list[tuple[float, float]]) -> None:
    for a, b in zip(pts, pts[1:]):
        add_wire(sch, a, b)


def add_junction(sch: Schematic, p: tuple[float, float]) -> None:
    _check_grid(p)
    _insert(sch, f"\n\t(junction\n\t\t(at {_fmt(p[0])} {_fmt(p[1])})\n\t\t(diameter 0)\n\t\t(color 0 0 0 0)\n"
                 f"\t\t(uuid \"{_uuid()}\")\n\t)")


def add_power_symbol(sch: Schematic, lib_id: str, p: tuple[float, float], rot: int = 0) -> str:
    """Place power:GND / power:+3.3V / power:+5V / power:PWR_FLAG ... with its pin at p."""
    _check_grid(p)
    block = sch.ensure_lib_symbol(lib_id)
    name = lib_id.split(":")[1]
    prefix = "#FLG" if name == "PWR_FLAG" else "#PWR"
    ref = sch.next_ref(prefix)

    def lib_prop(prop: str) -> tuple[float, float, bool]:
        """Offset (schematic y-down) and visibility of a property in the library definition."""
        m = re.search(rf'\n\t\t\t\(property "{prop}" "(?:[^"\\]|\\.)*"\n(.*?)\n\t\t\t\)', block, re.S)
        if not m:
            return 0.0, 0.0, True
        at = re.search(r"\(at ([-\d.]+) ([-\d.]+)", m.group(1))
        return float(at.group(1)), -float(at.group(2)), "(hide yes)" in m.group(1)

    desc = re.search(r'\(property "Description" "((?:[^"\\]|\\.)*)"', block)
    desc = desc.group(1) if desc else ""
    x, y = p

    def prop(nm: str, val: str) -> str:
        dx, dy, hidden = lib_prop(nm)
        th = math.radians(rot)
        rx, ry = dx * math.cos(th) + dy * math.sin(th), -dx * math.sin(th) + dy * math.cos(th)
        hide = "\n\t\t\t\t(hide yes)" if hidden else ""
        return (f'\n\t\t(property "{nm}" "{val}"\n\t\t\t(at {_fmt(x + rx)} {_fmt(y + ry)} {rot % 180})\n'
                f"\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t){hide}\n\t\t\t)\n\t\t)")

    sym = (f"\n\t(symbol\n\t\t(lib_id \"{lib_id}\")\n\t\t(at {_fmt(x)} {_fmt(y)} {rot})\n\t\t(unit 1)\n"
           f"\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n\t\t(on_board yes)\n\t\t(dnp no)\n\t\t(uuid \"{_uuid()}\")"
           + prop("Reference", ref) + prop("Value", name)
           + prop("Footprint", "") + prop("Datasheet", "") + prop("Description", desc)
           + f"\n\t\t(pin \"1\"\n\t\t\t(uuid \"{_uuid()}\")\n\t\t)"
           f"\n\t\t(instances\n\t\t\t(project \"{sch.project_name()}\"\n\t\t\t\t(path \"/{sch.root_uuid()}\"\n"
           f"\t\t\t\t\t(reference \"{ref}\")\n\t\t\t\t\t(unit 1)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t)")
    _insert(sch, sym)
    return ref


def add_symbol(sch: Schematic, lib_id: str, ref: str, value: str, footprint: str, p: tuple[float, float],
               rot: int = 0, fields: dict[str, str] | None = None, unit: int = 1) -> str:
    """Place a library symbol (any lib_id KiCad ships, embedded into lib_symbols) at p.

    Reference / Value sit beside the body, the rest hidden; `fields` adds e.g. LCSC / MPN. Returns ref."""
    _check_grid(p)
    block = sch.ensure_lib_symbol(lib_id)
    if ref in {_ref_of(blk)["ref"] for _, _, blk in sch._symbol_blocks()}:
        raise SchEditError(f"Reference {ref} already used")
    pins = sorted(set(re.findall(r'\(number "([^"]+)"', block)), key=lambda n: (len(n), n))
    x, y = p
    desc = re.search(r'\(property "Description" "((?:[^"\\]|\\.)*)"', block)
    ds = re.search(r'\(property "Datasheet" "((?:[^"\\]|\\.)*)"', block)

    def prop(nm, val, dx, dy, hidden):
        hide = "\n\t\t\t\t(hide yes)" if hidden else ""
        just = "\n\t\t\t\t(justify left)" if not hidden else ""
        return (f'\n\t\t(property "{nm}" "{val}"\n\t\t\t(at {_fmt(x + dx)} {_fmt(y + dy)} 0)\n'
                f"\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t){just}{hide}\n\t\t\t)\n\t\t)")

    props = (prop("Reference", ref, 2.54, -1.27, False) + prop("Value", value, 2.54, 1.27, False)
             + prop("Footprint", footprint, 0, 0, True) + prop("Datasheet", ds.group(1) if ds else "~", 0, 0, True)
             + prop("Description", desc.group(1) if desc else "", 0, 0, True))
    for k, v in (fields or {}).items():
        props += prop(k, v, 0, 0, True)
    pin_blocks = "".join(f'\n\t\t(pin "{n}"\n\t\t\t(uuid "{_uuid()}")\n\t\t)' for n in pins)
    sym = (f"\n\t(symbol\n\t\t(lib_id \"{lib_id}\")\n\t\t(at {_fmt(x)} {_fmt(y)} {rot})\n\t\t(unit {unit})\n"
           f"\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n\t\t(on_board yes)\n\t\t(dnp no)\n\t\t(uuid \"{_uuid()}\")"
           + props + pin_blocks
           + f"\n\t\t(instances\n\t\t\t(project \"{sch.project_name()}\"\n\t\t\t\t(path \"/{sch.root_uuid()}\"\n"
           f"\t\t\t\t\t(reference \"{ref}\")\n\t\t\t\t\t(unit {unit})\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t)")
    _insert(sch, sym)
    return ref


def _ref_of(block: str) -> dict:
    m = re.search(r'\(property "Reference" "([^"]+)"', block)
    return {"ref": m.group(1) if m else None}


def add_label(sch: Schematic, name: str, p: tuple[float, float], angle: int = 0) -> None:
    """A local net label with its connection point at p (angle 0: text to the right, 180: to the left)."""
    _check_grid(p)
    just = "left" if angle in (0, 90) else "right"
    _insert(sch, f'\n\t(label "{name}"\n\t\t(at {_fmt(p[0])} {_fmt(p[1])} {angle})\n\t\t(fields_autoplaced yes)\n'
                 f'\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n\t\t\t(justify {just} bottom)\n\t\t)\n'
                 f'\t\t(uuid "{_uuid()}")\n\t)')


def add_text(sch: Schematic, text: str, p: tuple[float, float], size: float = 1.27) -> None:
    """Free text note (KiCad stores line breaks as \\n inside the string)."""
    _check_grid(p)
    body = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    _insert(sch, f"\n\t(text \"{body}\"\n\t\t(exclude_from_sim no)\n\t\t(at {_fmt(p[0])} {_fmt(p[1])} 0)\n"
                 f"\t\t(effects\n\t\t\t(font\n\t\t\t\t(size {_fmt(size)} {_fmt(size)})\n\t\t\t)\n"
                 f"\t\t\t(justify left bottom)\n\t\t)\n\t\t(uuid \"{_uuid()}\")\n\t)")


def set_field(sch: Schematic, ref: str, name: str, value: str) -> str:
    """Set a symbol field (Value, Footprint, LCSC, MPN...). New fields are added hidden, like KiCad does."""
    esc = value.replace("\\", "\\\\").replace('"', '\\"')
    for start, end, b in sch._symbol_blocks():
        if not re.search(rf'\(property "Reference" "{re.escape(ref)}"', b):
            continue
        m = re.search(rf'(\n\t\t\(property "{re.escape(name)}" )"(?:[^"\\]|\\.)*"', b)
        if m:
            nb = b[:m.start()] + m.group(1) + f'"{esc}"' + b[m.end():]
            action = "updated"
        else:
            at = re.search(r"\n\t\t\(at ([-\d.]+) ([-\d.]+)", b)
            prop = (f'\n\t\t(property "{name}" "{esc}"\n\t\t\t(at {at.group(1)} {at.group(2)} 0)\n\t\t\t(effects\n'
                    f"\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t\t(hide yes)\n\t\t\t)\n\t\t)")
            last = list(re.finditer(r"\n\t\t\(property .*?\n\t\t\)", b, re.S))[-1]
            nb = b[:last.end()] + prop + b[last.end():]
            action = "added"
        sch.text = sch.text[:start] + nb + sch.text[end:]
        return action
    raise SchEditError(f"No symbol {ref}")


# ------------------------------------------------------------------ text overlap check
CHAR_W = 0.84   # KiCad stroke font: ink advance per char as a fraction of text height (measured from kicad-cli SVG)


def _xf(sym: dict, x: float, y: float) -> tuple[float, float]:
    """Library (y-up) point -> schematic coordinates for a placed symbol."""
    y = -y
    if sym["mirror"] == "y":
        x = -x
    elif sym["mirror"] == "x":
        y = -y
    th = math.radians(sym["rot"])
    return (sym["x"] + x * math.cos(th) + y * math.sin(th), sym["y"] - x * math.sin(th) + y * math.cos(th))


def symbol_body(sch: Schematic, sym: dict) -> tuple[float, float, float, float] | None:
    """Bounding box of a placed symbol's graphics and pins (schematic coordinates)."""
    block = sch.lib_symbol(sym["lib_id"])
    if not block:
        return None
    pts = []
    for m in re.finditer(r'\n\t\t\t\(symbol "[^"]*_(\d+)_(\d+)"(.*?)\n\t\t\t\)', block, re.S):
        if int(m.group(1)) not in (0, sym["unit"]):
            continue
        body = m.group(3)
        for a, b in re.findall(r"\((?:start|end|xy|mid) ([-\d.]+) ([-\d.]+)\)", body):
            pts.append((float(a), float(b)))
        for cxy, r in re.findall(r"\(center ([-\d.]+ [-\d.]+)\)\s*\(radius ([\d.]+)\)", body):
            cx, cy = (float(v) for v in cxy.split()); r = float(r)
            pts += [(cx - r, cy - r), (cx + r, cy + r)]
        for p in _lib_pins(f"\n\t\t\t(symbol \"x_{m.group(1)}_{m.group(2)}\"{body}\n\t\t\t)", sym["unit"]):
            th = math.radians(p["angle"])
            pts += [(p["x"], p["y"]), (p["x"] + p["length"] * math.cos(th), p["y"] + p["length"] * math.sin(th))]
    if not pts:
        return None
    xs, ys = zip(*(_xf(sym, x, y) for x, y in pts))
    return min(xs), min(ys), max(xs), max(ys)


def _text_box(x, y, angle, text, size, justify="") -> tuple[float, float, float, float]:
    """Approximate extent of a KiCad text item."""
    w = max(1, len(text)) * size * CHAR_W
    h = size
    horiz = int(round(angle)) % 180 == 0
    j = justify.split()
    if horiz:
        x0 = x if "left" in j else x - w if "right" in j else x - w / 2
        y0 = y - h if "bottom" in j else y if "top" in j else y - h / 2
        return x0, y0, x0 + w, y0 + h
    # vertical text runs bottom-to-top for 90 deg
    y0 = y - w if "left" in j else y if "right" in j else y - w / 2
    x0 = x - h if "bottom" in j else x if "top" in j else x - h / 2
    return x0, y0, x0 + h, y0 + w


def _overlap(a, b, m=0.0) -> bool:
    return a[0] < b[2] - m and b[0] < a[2] - m and a[1] < b[3] - m and b[1] < a[3] - m


def _seg_hits_box(p, q, box) -> bool:
    (x1, y1), (x2, y2) = p, q
    if max(x1, x2) < box[0] or min(x1, x2) > box[2] or max(y1, y2) < box[1] or min(y1, y2) > box[3]:
        return False
    if x1 == x2 or y1 == y2:   # orthogonal wires: bbox test is exact
        return True
    for k in range(21):
        x, y = x1 + (x2 - x1) * k / 20, y1 + (y2 - y1) * k / 20
        if box[0] <= x <= box[2] and box[1] <= y <= box[3]:
            return True
    return False


def text_items(sch: Schematic) -> list[dict]:
    """Visible symbol fields and net labels with their approximate boxes."""
    items = []
    for _, _, b in sch._symbol_blocks():
        ref = re.search(r'\(property "Reference" "([^"]+)"', b).group(1)
        if ref.startswith("#"):
            continue
        sym_rot = float(re.search(r"\n\t\t\(at [-\d.]+ [-\d.]+ ([-\d.]+)\)", b).group(1))
        for m in re.finditer(r'\n\t\t\(property "(Reference|Value)" "((?:[^"\\]|\\.)*)"\n\t\t\t\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)(.*?)\n\t\t\)', b, re.S):
            if "(hide yes)" in m.group(6):
                continue
            size = float((re.search(r"\(size ([\d.]+)", m.group(6)) or [0, 1.27])[1])
            just = (re.search(r"\(justify ([^)]+)\)", m.group(6)) or [0, ""])[1]
            # field angles are stored relative to the symbol; what you see is (stored + symbol rotation)
            shown = (float(m.group(5)) + sym_rot) % 180
            box = _text_box(float(m.group(3)), float(m.group(4)), shown, m.group(2), size, just if shown == float(m.group(5)) % 180 else "")
            items.append({"kind": m.group(1), "ref": ref, "text": m.group(2), "box": box})
    s, e = sch._lib_section()
    body = sch.text[sch.text.find("\n\t)", s) + 3:]
    # global labels carry (shape ...) / (fields_autoplaced ...) lines before their position
    for m in re.finditer(r'\n\t\((label|global_label) "([^"]+)"\n(?:\t\t\((?:shape|fields_autoplaced)[^\n]*\n)*'
                         r'\t\t\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)(.*?)\n\t\)', body, re.S):
        size = float((re.search(r"\(size ([\d.]+)", m.group(6)) or [0, 1.27])[1])
        just = (re.search(r"\(justify ([^)]+)\)", m.group(6)) or [0, "left bottom"])[1]
        box = _text_box(float(m.group(3)), float(m.group(4)), float(m.group(5)), m.group(2), size, just)
        if m.group(1) == "global_label":
            # drawn as an outlined flag: arrow + margins around the text, anchored at the connection point
            ang = int(round(float(m.group(5)))) % 360
            ext, half = len(m.group(2)) * size * CHAR_W + 1.2 * size, 0.75 * size
            ax, ay = float(m.group(3)), float(m.group(4))
            box = {0: (ax, ay - half, ax + ext, ay + half), 180: (ax - ext, ay - half, ax, ay + half),
                   90: (ax - half, ay - ext, ax + half, ay), 270: (ax - half, ay, ax + half, ay + ext)}.get(ang, box)
        items.append({"kind": m.group(1), "ref": None, "text": m.group(2), "box": box,
                      "anchor": (float(m.group(3)), float(m.group(4)))})
    return items


def text_overlaps(sch: Schematic, margin: float = -0.3) -> list[dict]:
    """Fields/labels that sit on wires, on other symbols, or on other text."""
    bodies = {}
    for _, _, b in sch._symbol_blocks():
        ref = re.search(r'\(property "Reference" "([^"]+)"', b).group(1)
        if ref.startswith("#"):
            continue
        box = symbol_body(sch, sch.placed(ref))
        if box:
            bodies[ref] = box
    s, e = sch._lib_section()
    body_text = sch.text[sch.text.find("\n\t)", s) + 3:]
    wires = [((float(a), float(b)), (float(c), float(d))) for a, b, c, d in
             re.findall(r"\(wire\n\t\t\(pts\n\t\t\t\(xy ([-\d.]+) ([-\d.]+)\) \(xy ([-\d.]+) ([-\d.]+)\)", body_text)]
    items = text_items(sch)
    problems = []
    for i, it in enumerate(items):
        box = (it["box"][0] + margin, it["box"][1] + margin, it["box"][2] - margin, it["box"][3] - margin)
        hits = []
        # labels are anchored on a pin end by design: test them against parts without the padding
        body_box = it["box"] if it["kind"] in ("label", "global_label") else (box[0] - 0.3, box[1] - 0.3, box[2] + 0.3, box[3] + 0.3)
        for ref, bb in bodies.items():
            if ref != it["ref"] and _overlap(body_box, bb, 0.05 if body_box is it["box"] else 0.0):
                hits.append(f"symbol {ref}")
        if it["kind"] in ("Reference", "Value"):
            for p, q in wires:
                if _seg_hits_box(p, q, box):
                    hits.append("wire"); break
        for j, other in enumerate(items):
            if j != i and _overlap(box, other["box"]):
                hits.append(f"{other['kind']} {other['ref'] or other['text']}")
        if hits:
            problems.append({**it, "hits": hits})
    return problems


def _set_field_pos(sch: Schematic, ref: str, name: str, x: float, y: float, sym_rot: float) -> None:
    """Move a field, make it read horizontally and centre-justified."""
    stored = (-sym_rot) % 180
    for start, end, b in sch._symbol_blocks():
        if not re.search(rf'\(property "Reference" "{re.escape(ref)}"', b):
            continue
        m = re.search(rf'\n\t\t\(property "{name}" "(?:[^"\\]|\\.)*"\n\t\t\t\(at [^)]*\)(.*?)\n\t\t\)', b, re.S)
        prop = m.group(0)
        new = re.sub(r"\(at [^)]*\)", f"(at {_fmt(x)} {_fmt(y)} {_fmt(stored)})", prop, count=1)
        new = re.sub(r"\n\t\t\t\t\(justify [^)]*\)", "", new)
        nb = b.replace(prop, new, 1)
        sch.text = sch.text[:start] + nb + sch.text[end:]
        return


def fix_field_overlaps(sch: Schematic, gap: float = 0.8, refs: list[str] | None = None) -> dict:
    """Reposition Reference/Value of every part whose fields overlap wires, other parts or other text
    (or of exactly `refs`, for parts that are legal but visibly cramped)."""
    problems = text_overlaps(sch)
    todo = list(refs) if refs else list(dict.fromkeys(p["ref"] for p in problems if p["ref"]))
    s, e = sch._lib_section()
    body_text = sch.text[sch.text.find("\n\t)", s) + 3:]
    wires = [((float(a), float(b)), (float(c), float(d))) for a, b, c, d in
             re.findall(r"\(wire\n\t\t\(pts\n\t\t\t\(xy ([-\d.]+) ([-\d.]+)\) \(xy ([-\d.]+) ([-\d.]+)\)", body_text)]
    fixed, best_effort = [], []
    for ref in todo:
        sym = sch.placed(ref)
        body = symbol_body(sch, sym)
        if not body:
            continue
        bodies = {}
        for _, _, b in sch._symbol_blocks():
            r = re.search(r'\(property "Reference" "([^"]+)"', b).group(1)
            if not r.startswith("#") and r != ref:
                bb = symbol_body(sch, sch.placed(r))
                if bb:
                    bodies[r] = bb
        others = [it["box"] for it in text_items(sch) if it["ref"] != ref]
        fields = {it["kind"]: it for it in text_items(sch) if it["ref"] == ref and it["kind"] in ("Reference", "Value")}
        if not fields:
            continue
        x0, y0, x1, y1 = body
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        h, line = 1.27, 1.7
        w = {k: max(1, len(v["text"])) * h * CHAR_W for k, v in fields.items()}
        def at(kind, x, y):
            return (x, y)
        cands = []
        # (ref_pos, value_pos) centres
        wr, wv = w.get("Reference", 0), w.get("Value", 0)
        cands.append(((x1 + gap + wr / 2, cy - line / 2), (x1 + gap + wv / 2, cy + line / 2)))   # right
        cands.append(((x0 - gap - wr / 2, cy - line / 2), (x0 - gap - wv / 2, cy + line / 2)))   # left
        cands.append(((cx, y0 - gap - h / 2 - line), (cx, y0 - gap - h / 2)))                     # above
        cands.append(((cx, y1 + gap + h / 2), (cx, y1 + gap + h / 2 + line)))                     # below
        cands.append(((cx, y0 - gap - h / 2), (cx, y1 + gap + h / 2)))                            # split
        for sx in (1.5, 3.0):                                                                     # further out
            cands.append(((x1 + gap * sx + wr / 2, cy - line / 2), (x1 + gap * sx + wv / 2, cy + line / 2)))
            cands.append(((x0 - gap * sx - wr / 2, cy - line / 2), (x0 - gap * sx - wv / 2, cy + line / 2)))
        for r in (2.5, 4.0, 6.0):                                                                 # ring search
            for dx, dy in ((1, -1), (-1, -1), (1, 1), (-1, 1), (0, -1.5), (0, 1.5)):
                bx_, by_ = (x1 if dx > 0 else x0 if dx < 0 else cx), (y0 if dy < 0 else y1)
                px, py = bx_ + dx * r + (wr / 2 if dx > 0 else -wr / 2 if dx < 0 else 0), by_ + dy * r
                cands.append(((px, py), (px + (wv - wr) / 2 * (1 if dx >= 0 else -1), py + line)))
        def score(c):
            n = 0
            for kind, (fx, fy) in zip(("Reference", "Value"), c):
                if kind not in fields:
                    continue
                bx = (fx - w[kind] / 2 - 0.3, fy - h / 2 - 0.3, fx + w[kind] / 2 + 0.3, fy + h / 2 + 0.3)
                bxb = (bx[0] - 0.6, bx[1] - 0.6, bx[2] + 0.6, bx[3] + 0.6)   # extra room from part bodies
                n += sum(1 for bb in bodies.values() if _overlap(bxb, bb))
                n += 1 if _overlap(bxb, body) else 0
                n += sum(1 for o in others if _overlap(bx, o))
                n += sum(1 for p, q in wires if _seg_hits_box(p, q, bx))
            if "Reference" in fields and "Value" in fields:
                (rx, ry), (vx, vy) = c
                if _overlap((rx - wr / 2, ry - h / 2, rx + wr / 2, ry + h / 2), (vx - wv / 2, vy - h / 2, vx + wv / 2, vy + h / 2)):
                    n += 1
            return n
        best = min(cands, key=score)
        for kind, pos in zip(("Reference", "Value"), best):
            if kind in fields:
                _set_field_pos(sch, ref, kind, round(pos[0], 3), round(pos[1], 3), sym["rot"])
        (fixed if score(best) == 0 else best_effort).append(ref)
    return {"fixed": fixed, "still_tight": best_effort}


def _snap(v: float) -> float:
    return round(round(v / GRID) * GRID, 4)


def snap_to_grid(sch: Schematic) -> dict:
    """Move off-grid symbols onto the 1.27 mm grid (fields move with them) and round every
    wire / label / junction / no-connect coordinate to the grid. Pin offsets in KiCad
    libraries are grid multiples, so pins and the wires that met them stay together."""
    s, e = sch._lib_section()
    e2 = sch.text.find("\n\t)", s) + 3
    head, body = sch.text[:e2], sch.text[e2:]
    stats = {"symbols": 0, "items": 0}

    def fix(m):
        kind, b = m.group(1), m.group(0)
        if kind == "symbol":
            at = re.search(r"\n\t\t\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)", b)
            x, y = float(at.group(1)), float(at.group(2))
            if on_grid(x) and on_grid(y):
                return b
            dx, dy = _snap(x) - x, _snap(y) - y
            stats["symbols"] += 1
            # shift the symbol origin and its field positions (both are absolute)
            return re.sub(r"\(at ([-\d.]+) ([-\d.]+)( [-\d.]+)?\)",
                          lambda a: f"(at {_fmt(float(a.group(1)) + dx)} {_fmt(float(a.group(2)) + dy)}{a.group(3) or ''})", b)
        if kind in ("wire", "bus", "label", "global_label", "hierarchical_label", "junction", "no_connect", "bus_entry"):
            new = re.sub(r"\((at|xy) ([-\d.]+) ([-\d.]+)",
                         lambda a: f"({a.group(1)} {_fmt(_snap(float(a.group(2))))} {_fmt(_snap(float(a.group(3))))}", b)
            if new != b:
                stats["items"] += 1
                pts = re.findall(r"\(xy ([-\d.]+) ([-\d.]+)\)", new)
                if kind == "wire" and len(pts) == 2 and pts[0] == pts[1]:
                    return ""  # collapsed to nothing
            return new
        return b

    sch.text = head + re.sub(r"\n\t\((\w+)\b.*?\n\t\)", fix, body, flags=re.S)
    return stats


def remove_items_at(sch: Schematic, points: list[tuple[float, float]], kinds=("label", "wire", "no_connect", "junction")) -> int:
    """Remove labels/wires/flags that have any coordinate at one of the given points."""
    targets = {(round(x, 3), round(y, 3)) for x, y in points}
    s, e = sch._lib_section()
    e2 = sch.text.find("\n\t)", s) + 3
    head, body = sch.text[:e2], sch.text[e2:]
    n = 0

    def drop(m):
        nonlocal n
        if m.group(1) not in kinds:
            return m.group(0)
        pts = {(round(float(a), 3), round(float(b), 3)) for a, b in re.findall(r"\((?:at|xy) ([-\d.]+) ([-\d.]+)", m.group(0))}
        if pts & targets:
            n += 1
            return ""
        return m.group(0)

    body = re.sub(r"\n\t\((\w+)\b.*?\n\t\)", drop, body, flags=re.S)
    sch.text = head + body
    return n

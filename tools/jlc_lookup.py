"""Look up JLCPCB/LCSC candidates for part specs via the free jlcsearch API (jlcsearch.tscircuit.com).

Usage: python tools/jlc_lookup.py specs.json > results.json

Spec kinds:
  {"key": .., "kind": "resistor", "ohms": 10000, "package": "0603"}
  {"key": .., "kind": "capacitor", "farads": 1e-7, "package": "0603", "min_volts": 50}
  {"key": .., "kind": "text", "search": "SMBJ28CA", "must": ["SMBJ28CA"], "package": "SMB"}
Ranking: basic parts first (no JLCPCB extended-part setup fee), then preferred, then price.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://jlcsearch.tscircuit.com"


def _get(path: str, params: dict) -> dict:
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ai-pcb-copilot)"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            time.sleep(2)
    return {}


def unit_price(price, qty: int = 100) -> float | None:
    if isinstance(price, (int, float)):
        return round(float(price), 5)
    best = None
    for tier in (price or "").split(","):
        rng, _, val = tier.partition(":")
        try:
            if int(rng.split("-")[0]) <= qty:
                best = float(val)
        except ValueError:
            continue
    return best


def _row(c: dict, extra: str = "") -> dict:
    return {
        "lcsc": f"C{c['lcsc']}",
        "mfr": c.get("mfr", ""),
        "package": c.get("package", ""),
        "basic": bool(c.get("is_basic")),
        "preferred": bool(c.get("is_preferred")),
        "stock": c.get("stock", 0),
        "price_100": unit_price(c.get("price1", c.get("price", ""))),
        "description": (extra or c.get("description", ""))[:140],
    }


def search(spec: dict) -> list[dict]:
    kind = spec.get("kind", "text")
    rows = []
    if kind == "resistor":
        data = _get("/resistors/list.json", {"package": spec["package"], "resistance": spec["ohms"], "limit": 60})
        for c in data.get("resistors", []):
            if abs(c["resistance"] - spec["ohms"]) / spec["ohms"] > 1e-6:
                continue
            if c.get("tolerance_fraction") and c["tolerance_fraction"] > spec.get("max_tol", 0.05):
                continue
            rows.append(_row(c, f"{c['resistance']} ohm {c.get('tolerance_fraction')} {c.get('power_watts')}mW"))
    elif kind == "capacitor":
        data = _get("/capacitors/list.json", {"package": spec["package"], "capacitance": spec["farads"], "limit": 80})
        for c in data.get("capacitors", []):
            if abs(c["capacitance_farads"] - spec["farads"]) / spec["farads"] > 1e-6:
                continue
            if (c.get("voltage_rating") or 0) < spec.get("min_volts", 0):
                continue
            if spec.get("dielectric") and c.get("temperature_coefficient") not in spec["dielectric"]:
                continue
            rows.append(_row(c, f"{c['capacitance_farads']:.3g}F {c.get('voltage_rating')}V {c.get('temperature_coefficient')}"))
    else:
        data = _get("/components/list.json", {"search": spec["search"], "limit": spec.get("limit", 60)})
        for c in data.get("components", []):
            text = f"{c.get('mfr', '')} {c.get('description', '')} {c.get('package', '')}".lower()
            if spec.get("package") and spec["package"].lower() not in c.get("package", "").lower():
                continue
            if any(m.lower() not in text for m in spec.get("must", [])):
                continue
            if any(m.lower() in text for m in spec.get("exclude", [])):
                continue
            rows.append(_row(c))
    rows = [r for r in rows if (r["stock"] or 0) >= spec.get("min_stock", 500)]
    rows.sort(key=lambda r: (not r["basic"], not r["preferred"], r["price_100"] if r["price_100"] is not None else 99))
    return rows[: spec.get("top", 3)]


def main() -> None:
    specs = json.load(open(sys.argv[1], encoding="utf-8"))
    json.dump({s["key"]: search(s) for s in specs}, sys.stdout, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()

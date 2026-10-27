from pathlib import Path

import pytest

from copilot import spice
from copilot.netlist import Netlist


@pytest.mark.parametrize(
    "text,value",
    [("10k", 1e4), ("4k7", 4.7e3), ("4.7uF", 4.7e-6), ("0R", 0.0), ("100R", 100.0), ("1M", 1e6),
     ("10uF/25V", 1e-5), ("10uF 25V", 1e-5), ("2R2", 2.2), ("1meg", 1e6), ("100n", 1e-7), ("2.2µH", 2.2e-6),
     ("CSD18532", None)],
)
def test_parse_value(text, value):
    got = spice.parse_value(text)
    assert got == pytest.approx(value) if value is not None else got is None


RC = """* rc
V1 in 0 PULSE(0 3.3 1u 1n 1n 1 2)
R1 in out 10k
C1 out 0 100n
.tran 1u 5m
.end
"""

CHECKS = [{"expr": "V(out)", "stat": "at", "at": 1.001e-3, "min": 2.0, "max": 2.2},
          {"expr": "V(out)", "stat": "final", "min": 3.2, "max": 3.3}]


@pytest.mark.skipif(not spice.find_ngspice_dll(), reason="ngspice.dll not found")
def test_ngspice_rc(tmp_path):
    workdir = tmp_path / "path with spaces"  # real projects live in folders like "AI PCB Generator"
    res = spice.simulate(RC, simulator="ngspice", checks=CHECKS, workdir=workdir)
    assert res["ngspice"]["ok"], res
    assert res["ngspice"]["all_checks_pass"], res["ngspice"]["checks"]


@pytest.mark.skipif(not spice.find_ltspice(), reason="LTspice not installed")
def test_ltspice_matches_ngspice(tmp_path):
    res = spice.simulate(RC, simulator="both", checks=CHECKS, workdir=tmp_path)
    assert res["ltspice"]["all_checks_pass"] and res["ngspice"]["all_checks_pass"]
    assert res["cross_check"]["agree"], res["cross_check"]


def test_deck_from_esc_netlist():
    nl = Netlist.from_xml(Path(__file__).parent / "data" / "esc.net.xml")
    deck = spice.build_deck(nl, ".op", refs=["R20", "R23", "R26"], sources={"/Phase_A": 24})
    assert "RR20 Phase_A BEMF_A 20000" in deck.text
    assert "RR23 BEMF_A 0 2200" in deck.text
    assert "V_Phase_A Phase_A 0 DC 24" in deck.text
    assert deck.modeled == ["R20", "R23", "R26"] and not deck.skipped


@pytest.mark.skipif(not spice.find_ngspice_dll(), reason="ngspice.dll not found")
def test_ngspice_relative_workdir(tmp_path, monkeypatch):
    # callers often pass a relative workdir; the runner chdirs before writing, so paths must be absolute
    monkeypatch.chdir(tmp_path)
    res = spice.simulate(RC, simulator="ngspice", checks=CHECKS, workdir=Path("rel dir/run"))
    assert res["ngspice"]["ok"], res

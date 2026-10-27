"""Review rules against netlists exported from the real sample boards (tests/data)."""

import shutil
from pathlib import Path

import pytest

from copilot import review
from copilot.kicad import Project
from copilot.netlist import Netlist

DATA = Path(__file__).parent / "data"
SAMPLES = Path(__file__).parent.parent / "sample_boards"


def run_rules(net_xml: str, sch_name: str, tmp_path: Path) -> list[review.Finding]:
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    sch = SAMPLES / f"{sch_name}.kicad_sch"
    if sch.exists():
        shutil.copy(sch, proj_dir / sch.name)
    ctx = review.Context(Project(proj_dir, sch_name), Netlist.from_xml(DATA / net_xml))
    return review.run(ctx)


def rules(findings, name):
    return [f for f in findings if f.rule == name]


def test_flight_controller_caps_only_net(tmp_path):
    fs = run_rules("fc.net.xml", "flight_controller", tmp_path)
    caps = rules(fs, "caps-only-net")
    assert len(caps) == 1 and caps[0].severity == "error"
    assert set(caps[0].refs) == {"C1", "C2", "C3", "C4", "C8", "C10", "C13"}


def test_flight_controller_no_false_positives(tmp_path):
    fs = run_rules("fc.net.xml", "flight_controller", tmp_path)
    # D+ / D- are a pair, not a misspelling
    assert not any("/USB_D+" in f.nets for f in rules(fs, "similar-net-names"))
    # BST pin's cap goes to SW on purpose
    assert not any("BST" in n for f in rules(fs, "missing-decoupling") for n in f.nets)
    # baro I2C has pull-ups; GPS I2C goes to a connector, so only a note
    pull = rules(fs, "missing-pullup")
    assert all(f.severity == "info" for f in pull)
    assert {n for f in pull for n in f.nets} == {"/GPS_SCL", "/GPS_SDA"}
    # USB-C CC resistors are present
    assert not rules(fs, "usb-c-cc")


def test_missing_sheet_files_detected(tmp_path):
    fs = run_rules("esc.net.xml", "ESC PCB V1", tmp_path)
    assert len(rules(fs, "missing-sheet-file")) == 3


@pytest.mark.skipif(not (SAMPLES / "ESC PCB V1.kicad_sch").exists(), reason="sample board missing")
def test_esc_off_page(tmp_path):
    fs = run_rules("esc.net.xml", "ESC PCB V1", tmp_path)
    off = rules(fs, "off-page")
    assert off and "U2" in off[0].refs


def test_similar_names_normalisation():
    assert review._norm_net("+3.3V") == review._norm_net("3V3") == review._norm_net("/3.3V")
    assert review._norm_net("/USB_D+") != review._norm_net("/USB_D-")

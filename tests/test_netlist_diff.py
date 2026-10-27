from pathlib import Path

from copilot.netlist import Netlist, diff, pin_pair_similarity


def make(tmp_path: Path, name: str, comps: dict[str, str], nets: dict[str, list[tuple[str, str]]]) -> Netlist:
    comp_xml = "".join(
        f'<comp ref="{r}"><value>{v}</value><footprint>fp</footprint><libsource lib="Device" part="{r[0]}"/></comp>'
        for r, v in comps.items()
    )
    net_xml = "".join(
        f'<net code="{i}" name="{n}">' + "".join(f'<node ref="{r}" pin="{p}" pintype="passive"/>' for r, p in pins) + "</net>"
        for i, (n, pins) in enumerate(nets.items(), 1)
    )
    path = tmp_path / f"{name}.xml"
    path.write_text(f"<export><design/><components>{comp_xml}</components><nets>{net_xml}</nets></export>")
    return Netlist.from_xml(path)


BASE_COMPS = {"R1": "10k", "C1": "100n", "U1": "MCU"}
BASE_NETS = {
    "+3.3V": [("U1", "1"), ("C1", "1"), ("R1", "1")],
    "GND": [("U1", "2"), ("C1", "2")],
    "/SDA": [("U1", "3"), ("R1", "2")],
}


def test_identical_is_empty(tmp_path):
    a = make(tmp_path, "a", BASE_COMPS, BASE_NETS)
    d = diff(a, a)
    assert d.empty and d.similarity == 1.0


def test_added_part_reports_its_connections(tmp_path):
    a = make(tmp_path, "a", BASE_COMPS, BASE_NETS)
    nets = {k: list(v) for k, v in BASE_NETS.items()}
    nets["/SDA"].append(("R2", "1"))
    nets["+3.3V"].append(("R2", "2"))
    b = make(tmp_path, "b", {**BASE_COMPS, "R2": "4k7"}, nets)
    d = diff(a, b)
    assert d.components_added == ["R2"]
    assert {(p["pin"], p["net"]) for p in d.pins_connected} == {("R2.1", "/SDA"), ("R2.2", "+3.3V")}
    assert d.touched_refs() == {"R2"}


def test_auto_renamed_net_is_not_noise(tmp_path):
    a = make(tmp_path, "a", BASE_COMPS, {**BASE_NETS, "Net-(R1-Pad3)": [("R1", "3"), ("U1", "4")]})
    b = make(tmp_path, "b", BASE_COMPS, {**BASE_NETS, "Net-(U1-PA4)": [("R1", "3"), ("U1", "4")]})
    assert diff(a, b).empty


def test_user_rename_and_pin_move(tmp_path):
    a = make(tmp_path, "a", BASE_COMPS, BASE_NETS)
    nets = {"+3.3V": BASE_NETS["+3.3V"], "GND": BASE_NETS["GND"] + [("U1", "3")], "/I2C_SDA": [("R1", "2")]}
    b = make(tmp_path, "b", BASE_COMPS, nets)
    d = diff(a, b)
    assert {"pin": "U1.3", "net": "GND"} in d.pins_connected
    assert {"pin": "U1.3", "net": "/SDA"} in d.pins_disconnected
    assert {"from": "/SDA", "to": "/I2C_SDA"} in d.nets_renamed


def test_merge_is_flagged(tmp_path):
    a = make(tmp_path, "a", BASE_COMPS, BASE_NETS)
    b = make(tmp_path, "b", BASE_COMPS, {"+3.3V": BASE_NETS["+3.3V"] + BASE_NETS["/SDA"], "GND": BASE_NETS["GND"]})
    d = diff(a, b)
    assert d.nets_merged and d.nets_merged[0]["into"] == "+3.3V"
    assert d.nets_merged[0]["absorbed"] == ["/SDA"]
    # only parts on the absorbed net changed; the rest of +3.3V did not
    assert d.touched_refs() == {"U1", "R1"} and set(d.nets_merged[0]["refs"]) == {"U1", "R1"}
    assert pin_pair_similarity(a, b) < 1.0


def test_reused_reference_is_replace_not_short(tmp_path):
    # U9 was a buck (pin 2 = GND, pin 5 = VM); now U9 is an LDO (pin 2 = 3V3). Not a GND-3V3 short.
    a = make(tmp_path, "a", {"U9": "BUCK", "C1": "10u"},
             {"GND": [("U9", "2"), ("C1", "2")], "VM": [("U9", "5"), ("C1", "1")], "+3.3V": [("C2", "1")]})
    comps_b = {"U9": "LDO", "C1": "10u"}
    b = make(tmp_path, "b", comps_b,
             {"GND": [("U9", "1"), ("C1", "2")], "VM": [("C1", "1")], "+3.3V": [("U9", "2"), ("C2", "1")]})
    # make() builds lib ids from the ref's first letter, so force a different part on the new side
    b.components["U9"].lib_id = "Regulator_Linear:AMS1117-3.3"
    d = diff(a, b)
    assert not d.nets_merged and not d.nets_split
    assert "U9" in d.components_removed and "U9'" in d.components_added
    assert d.touched_refs() == {"U9"}


def test_single_pin_move_is_not_a_merge(tmp_path):
    # C1 reused: was a VM bulk cap, now a 3.3V output cap -> one pin hops nets; no merge/split.
    a = make(tmp_path, "a", BASE_COMPS, {**BASE_NETS, "+3.3V": BASE_NETS["+3.3V"]})
    nets = {"+3.3V": [p for p in BASE_NETS["+3.3V"] if p != ("C1", "1")], "GND": BASE_NETS["GND"] + [("C1", "1")],
            "/SDA": BASE_NETS["/SDA"]}
    b = make(tmp_path, "b", BASE_COMPS, nets)
    d = diff(a, b)
    assert not d.nets_merged and not d.nets_split
    assert {"pin": "C1.1", "net": "GND"} in d.pins_connected

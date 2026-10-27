"""AI PCB Copilot MCP server.

Runs next to the KiCad MCP engine (which does the editing). This server adds:
  * checkpoints / undo / netlist diff / verify_edit  (safety)
  * live view in the browser                          (watch edits happen)
  * measured design review                            (ERC, DRC, netlist rules, fab rules)
  * SPICE via LTspice or ngspice                       (simulate parts of the schematic)

Run:  python -m copilot.server
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import checks as erc_checks, review, spice
from .checkpoints import CheckpointStore
from .kicad import Project, resolve_project
from .netlist import Netlist, diff

server = MCPServer(
    "copilot",
    instructions=(
        "Safety, review, live view and SPICE for KiCad projects. Use alongside the 'kicad' engine server, "
        "which performs the edits. Workflow for every change: checkpoint -> edit with kicad tools -> "
        "verify_edit (netlist diff + ERC delta) -> show the user -> keep, or undo. "
        "Connectivity from this server (kicad-cli netlist) is ground truth; prefer it over the engine's "
        "list_schematic_nets, which misses power-symbol nets."
    ),
)

_state = {"project": None}
_lock = threading.Lock()


def _project(project: str | None) -> Project:
    if project:
        _state["project"] = resolve_project(project)
    if _state["project"] is None:
        raise ValueError("No project yet: pass `project` (a project folder or .kicad_pro/.kicad_sch path).")
    return _state["project"]


def _store(p: Project) -> CheckpointStore:
    return CheckpointStore(p)


def _dump(obj) -> str:
    return json.dumps(obj, indent=1, default=str)


# ================================================================== safety
@server.tool()
def project_status(project: str | None = None) -> str:
    """Project files, whether they changed since the last checkpoint, recent checkpoints and the live-view URL.
    Call this first with the project folder; later calls may omit `project`."""
    p = _project(project)
    st = _store(p)
    from .viewer import current_url

    return _dump({
        "project": str(p.dir),
        "name": p.name,
        "schematic": str(p.sch) if p.sch.exists() else None,
        "pcb": str(p.pcb) if p.pcb.exists() else None,
        "design_files": [str(f.relative_to(p.dir)) for f in p.tracked_files()],
        "changed_since_last_checkpoint": st.dirty(),
        "checkpoints": [c.to_dict() for c in st.list(limit=8)],
        "live_view": current_url(),
    })


@server.tool()
def checkpoint(label: str, project: str | None = None) -> str:
    """Snapshot the project's design files before an edit (or to mark a good state).
    Cheap and safe to call often; does nothing if nothing changed."""
    p = _project(project)
    with _lock:
        cp, created = _store(p).checkpoint(label)
    return _dump({"checkpoint": cp.to_dict(), "created": created})


@server.tool()
def list_checkpoints(project: str | None = None, limit: int = 20, include_auto: bool = False) -> str:
    """List checkpoints, newest first. Auto checkpoints are the ones saved before each restore."""
    return _dump([c.to_dict() for c in _store(_project(project)).list(limit, include_auto)])


@server.tool()
def undo(project: str | None = None) -> str:
    """Revert the design files to the newest checkpoint that differs from the current files.
    The current state is saved first as an [auto] checkpoint, so an undo can itself be undone with
    restore_checkpoint. Close or reload the file in KiCad afterwards (File > Revert)."""
    p = _project(project)
    with _lock:
        result = _store(p).undo()
    result["reminder"] = "If the schematic is open in KiCad, use File > Revert to see the restored version."
    return _dump(result)


@server.tool()
def restore_checkpoint(checkpoint_id: str, project: str | None = None) -> str:
    """Make the design files match a checkpoint exactly (current state is saved first)."""
    p = _project(project)
    with _lock:
        return _dump(_store(p).restore(checkpoint_id))


@server.tool()
def netlist_diff(from_checkpoint: str = "latest", to_checkpoint: str = "current", project: str | None = None) -> str:
    """Pin-level connectivity and part changes between two checkpoints ('latest', an id, or 'current' = files on disk).
    Net matching is by shared pins, so KiCad's auto-renamed nets don't show up as noise."""
    p = _project(project)
    st = _store(p)

    def rev(x: str):
        if x == "current":
            return None
        if x == "latest":
            cps = st.list(limit=1)
            if not cps:
                raise ValueError("No checkpoints yet; call checkpoint first.")
            return cps[0].id
        return x

    d = diff(st.netlist(rev(from_checkpoint)), st.netlist(rev(to_checkpoint)))
    return d.text() + "\n\n" + _dump(d.to_dict())


@server.tool()
def verify_edit(
    intent: str,
    expected_refs: list[str] | None = None,
    project: str | None = None,
    run_erc: bool = True,
) -> str:
    """Check the edits made since the last checkpoint: what changed in the netlist, which ERC
    violations are new or resolved, and whether anything outside `expected_refs` was touched.
    Always run this after editing and report the result to the user before moving on."""
    p = _project(project)
    st = _store(p)
    cps = st.list(limit=1)
    if not cps:
        raise ValueError("No checkpoint to compare against; call checkpoint before editing.")
    base = cps[0]
    d = diff(st.netlist(base.id), st.netlist(None))
    out: dict = {"intent": intent, "compared_to": base.to_dict(), "changes": d.text(), "touched_refs": sorted(d.touched_refs())}
    problems = []
    if expected_refs:
        unexpected = sorted(d.touched_refs() - set(expected_refs))
        if unexpected:
            problems.append(f"Touched parts outside the expected set: {unexpected}")
        untouched = sorted(set(expected_refs) - d.touched_refs())
        if untouched:
            out["expected_but_unchanged"] = untouched
    for m in d.nets_merged:
        line = f"{', '.join(m['absorbed'])} merged into {m['into']}"
        if expected_refs and set(m["refs"]) <= set(expected_refs):
            out.setdefault("intended_merges", []).append(line)
        else:
            problems.append(f"{line}: a merge you didn't plan is a short; confirm it is intended.")
    if run_erc:
        delta = erc_checks.delta(st.erc(base.id), st.erc(None))
        out["erc_new"] = [v.line() for v in delta["new"]]
        out["erc_resolved"] = [v.line() for v in delta["resolved"]]
        if any(v.severity == "error" for v in delta["new"]):
            problems.append("New ERC errors.")
    out["problems"] = problems
    out["verdict"] = "OK" if not problems else "CHECK"
    return _dump(out)


# ================================================================== reading
@server.tool()
def get_netlist(project: str | None = None, refs: list[str] | None = None, nets: list[str] | None = None) -> str:
    """Ground-truth connectivity from kicad-cli (power symbols, global labels and hierarchy resolved).
    With no filter: every part and net. With refs/nets: only those parts' pins / those nets."""
    p = _project(project)
    nl = Netlist.from_schematic(p.sch)
    if not refs and not nets:
        return nl.summary()
    lines = []
    for ref in refs or []:
        c = nl.components.get(ref)
        if not c:
            lines.append(f"{ref}: not found")
            continue
        lines.append(f"{ref}: {c.value} [{c.lib_id}] fp={c.footprint or '-'}")
        for pin, net in sorted(nl.nets_of(ref).items(), key=lambda kv: (len(kv[0]), kv[0])):
            fn = nl.pin_functions.get((ref, pin), "")
            lines.append(f"  pin {pin}{f' ({fn})' if fn else ''} [{nl.pin_types.get((ref, pin), '')}] -> {net}")
    for name in nets or []:
        net = nl.nets.get(name) or nl.nets.get("/" + name)
        if not net:
            lines.append(f"net {name}: not found")
            continue
        lines.append(f"net {net.name}: " + ", ".join(f"{n.ref}.{n.pin}{f'({n.pin_function})' if n.pin_function else ''}" for n in net.nodes))
    return "\n".join(lines)


# ================================================================== review
def _auto_fab(p: Project) -> str | None:
    if not p.pcb.exists():
        return None
    text = p.pcb.read_text(encoding="utf-8", errors="replace")
    head = text[: text.find("(setup")] if "(setup" in text else text[:20000]
    inner = len(set(__import__("re").findall(r'"In\d+\.Cu"', head)))
    return "jlcpcb-4layer" if inner else "jlcpcb-2layer"


@server.tool()
def review_design(
    project: str | None = None,
    run_drc: bool = False,
    fab_profile: str = "auto",
    write_report: bool = True,
) -> str:
    """Measured design review: ERC, netlist rules (decoupling, caps wired only to each other, pull-ups,
    crystal load caps, LED resistors, USB-C CC resistors, look-alike net names, single-pin nets,
    missing sheet files, BOM fields), fab-rule checks on the board, and optionally DRC with
    schematic parity (slow on big boards: ~1 min). An error means FAIL regardless of opinion.
    After this, add your own engineering judgement as separate, clearly-labelled findings."""
    p = _project(project)
    nl = Netlist.from_schematic(p.sch)
    work = p.state_dir / "review"
    erc = erc_checks.run_erc(p.sch, work)
    drc = erc_checks.run_drc(p.pcb, work) if run_drc and p.pcb.exists() else None
    fab_name = _auto_fab(p) if fab_profile == "auto" else (fab_profile or None)
    ctx = review.Context(p, nl, erc=erc, drc=drc, fab=review.load_fab_profile(fab_name))
    findings = review.run(ctx)
    result = review.verdict(findings)
    extra = {"fab profile": fab_name or "none", "DRC": "run" if drc is not None else "skipped (run_drc=true to include)"}
    out = {**result, **extra, "findings": [f.to_dict() for f in findings]}
    if write_report:
        md = review.render_markdown(p, findings, extra)
        (p.state_dir / "review.md").write_text(md, encoding="utf-8")
        (p.state_dir / "review.json").write_text(_dump(out), encoding="utf-8")
        out["report"] = str(p.state_dir / "review.md")
        from .viewer import publish_review

        publish_review(p, out)
    return _dump(out)


# ================================================================== SPICE
@server.tool()
def spice_simulators() -> str:
    """Which simulators are available (LTspice, ngspice via KiCad's ngspice.dll)."""
    return _dump(spice.available())


@server.tool()
def spice_build_deck(
    analysis: str,
    project: str | None = None,
    refs: list[str] | None = None,
    sources: dict[str, float | str] | None = None,
    loads: dict[str, float | str] | None = None,
    models: dict[str, str] | None = None,
    extra: str = "",
) -> str:
    """Build a SPICE deck from the schematic without running it, so you can inspect or edit it
    before spice_run. R, C, L, ferrite beads, diodes, zeners and LEDs are modeled automatically;
    ICs, transistors and connectors are listed under not_modeled.
      analysis: '.op' | '.tran 1u 5m' | '.ac dec 50 10 1meg'
      refs:     parts to include (default all modelable parts)
      sources:  {"+3.3V": 3.3, "/Phase_A": "PULSE(0 24 0 1u 1u 50u 100u)"}: voltage from net to GND
      loads:    {"+3.3V": "150m"}: DC current drawn from a net (stand-in for an IC)
      models:   {"U1": "XU1 {VIN} {VOUT} {GND} LDO_MODEL"}: your own element line; {pin number or
                pin name} becomes that pin's node. Put .model/.subckt text in `extra`."""
    p = _project(project)
    nl = Netlist.from_schematic(p.sch)
    deck = spice.build_deck(nl, analysis, refs=refs, sources=sources, loads=loads, models=models, extra=extra, title=p.name)
    used = {n for line in deck.text.splitlines() for n in line.split()[1:3]}
    node_map = {net: node for net, node in deck.node_map.items() if node in used}
    return _dump({"deck": deck.text, "node_map": node_map, **deck.coverage()})


@server.tool()
def spice_run(
    deck: str,
    simulator: str = "auto",
    checks: list[dict] | None = None,
    node_map: dict[str, str] | None = None,
) -> str:
    """Run a SPICE deck (yours or from spice_build_deck). simulator: auto (LTspice if installed,
    else ngspice), ltspice, ngspice, or both (runs both and cross-checks the results).
    checks: [{"expr": "V(+3.3V)", "stat": "final|max|min|avg|rms|pp|at", "at": 1e-3,
              "min": 3.2, "max": 3.4, "name": "..."}]. expr is V(net), V(net1,net2) or I(Vsource);
    nets can be KiCad net names when node_map is passed."""
    p = _state["project"]
    workdir = (p.state_dir / "spice") if p else None
    return _dump(spice.simulate(deck, simulator=simulator, checks=checks, node_map=node_map, workdir=workdir))


@server.tool()
def spice_check(
    analysis: str,
    checks: list[dict],
    project: str | None = None,
    refs: list[str] | None = None,
    sources: dict[str, float | str] | None = None,
    loads: dict[str, float | str] | None = None,
    models: dict[str, str] | None = None,
    extra: str = "",
    simulator: str = "auto",
) -> str:
    """Build a deck from the schematic and run it in one step, returning pass/fail for each check.
    Same arguments as spice_build_deck plus checks and simulator (see spice_run).
    Example: verify a divider keeps an ADC pin under 3.3 V at 25.2 V input:
      analysis=".op", refs=["R20","R23"], sources={"VM": 25.2},
      checks=[{"expr": "V(BEMF_A)", "max": 3.3}]"""
    p = _project(project)
    nl = Netlist.from_schematic(p.sch)
    deck = spice.build_deck(nl, analysis, refs=refs, sources=sources, loads=loads, models=models, extra=extra, title=p.name)
    result = spice.simulate(deck.text, simulator=simulator, checks=checks, node_map=deck.node_map,
                            workdir=p.state_dir / "spice")
    return _dump({"coverage": deck.coverage(), "results": result, "deck": deck.text})


# ================================================================== tidy schematic edits
def _sch_for(project: str | None, sheet: str | None):
    from .schedit import Schematic

    p = _project(project)
    return p, Schematic(p.dir / sheet if sheet else p.sch)


def _finish_sch(sch) -> None:
    from .sexpfmt import format_kicad

    sch.save()
    sch.path.write_text(format_kicad(sch.path.read_text(encoding="utf-8")), encoding="utf-8", newline="")


@server.tool()
def pin_positions(ref: str, project: str | None = None, sheet: str | None = None) -> str:
    """Exact schematic coordinates of every pin end of a placed symbol. Use these as wire endpoints."""
    from .schedit import pin_endpoints

    _, sch = _sch_for(project, sheet)
    return _dump({"symbol": sch.placed(ref), "pins": pin_endpoints(sch, ref)})


@server.tool()
def connect_power(ref: str, pin: str, power: str, project: str | None = None, sheet: str | None = None,
                  stub_dx: float = 0.0, stub_dy: float = 0.0) -> str:
    """Connect a pin to a rail with a real power symbol (power:GND, power:+3.3V, power:+5V, power:PWR_FLAG ...),
    the way hand-drawn KiCad schematics do -- not a text label. Optional straight stub (dx or dy, mm, on the
    1.27 grid) moves the symbol off the pin first: point GND stubs down (+dy) and supply stubs up (-dy).
    Run verify_edit afterwards."""
    from .schedit import add_power_symbol, add_wire, pin_endpoints

    _, sch = _sch_for(project, sheet)
    p = pin_endpoints(sch, ref)[pin]
    end = (round(p[0] + stub_dx, 4), round(p[1] + stub_dy, 4))
    if end != p:
        add_wire(sch, p, end)
    lib_id = power if ":" in power else f"power:{power}"
    made = add_power_symbol(sch, lib_id, end)   # KiCad power symbols already point the right way at 0 deg
    _finish_sch(sch)
    return _dump({"placed": made, "at": end})


@server.tool()
def draw_wire(points: list[list[float]], junctions: list[list[float]] | None = None,
              project: str | None = None, sheet: str | None = None) -> str:
    """Draw a straight wire path through points [[x,y], ...] (mm, 1.27 grid). Diagonal segments are refused:
    route with horizontal/vertical legs. Add junction dots where 3+ items meet."""
    from .schedit import add_junction, add_path

    _, sch = _sch_for(project, sheet)
    add_path(sch, [tuple(p) for p in points])
    for j in junctions or []:
        add_junction(sch, tuple(j))
    _finish_sch(sch)
    return _dump({"segments": len(points) - 1, "junctions": len(junctions or [])})


@server.tool()
def tidy_schematic(project: str | None = None, sheet: str | None = None, snap: bool = True,
                   declutter: bool = True) -> str:
    """Make a schematic look hand-drawn: snap symbols/wires/labels to the 1.27 mm grid (connectivity preserved),
    move Reference/Value text off wires, other parts and other text, and re-save in KiCad's exact file format.
    Reports remaining text overlaps. Run verify_edit afterwards (it should show no connectivity change)."""
    from .schedit import fix_field_overlaps, snap_to_grid, text_overlaps

    _, sch = _sch_for(project, sheet)
    out = {"overlaps_before": len(text_overlaps(sch))}
    if snap:
        out["snapped"] = snap_to_grid(sch)
    if declutter:
        for _ in range(3):
            r = fix_field_overlaps(sch)
            if not text_overlaps(sch):
                break
        out["fields_moved"] = r
    left = text_overlaps(sch)
    out["overlaps_after"] = [{"kind": p["kind"], "item": p["ref"] or p["text"], "hits": p["hits"][:3]} for p in left]
    _finish_sch(sch)
    return _dump(out)


# ================================================================== live view
@server.tool()
def start_live_view(project: str | None = None, port: int = 8765) -> str:
    """Start (or retarget) the browser live view for a project and return its URL. The page redraws
    within ~1 s of every save, highlights changed parts, and lists checkpoints, diffs and review findings.
    Tell the user to open the URL."""
    p = _project(project)
    from .viewer import ensure_viewer

    return _dump({"url": ensure_viewer(p, port)})


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()

# AI PCB Copilot — agent instructions

This repo lets an LLM read, review, simulate and edit KiCad projects. Two MCP servers are registered in `.mcp.json`:

- **`kicad`**: the editing engine (vendored [mixelpixx/KiCAD-MCP-Server](https://github.com/mixelpixx/KiCAD-MCP-Server) v2.8.2). It places symbols, wires, labels and footprints; routes; runs exports; searches JLCPCB parts.
- **`copilot`**: ours (`copilot/`). It provides checkpoints/undo, netlist diff, `verify_edit`, the measured design review, SPICE (LTspice + ngspice) and the browser live view.

## The edit loop (always)

1. `copilot.project_status(project=<folder>)`, once per session. Then `copilot.start_live_view` and give the user the URL.
2. `copilot.checkpoint(label="before: <what you're about to do>")`.
3. Edit with `kicad` tools. Prefer `batch_add_and_connect` / `connect_to_net` over hand-placed wires.
4. `copilot.verify_edit(intent=..., expected_refs=[...])`. Read the whole result:
   - `problems` not empty, or `erc_new` has errors → fix it or `copilot.undo`, then verify again.
   - Tell the user what changed (the `changes` text) and what ERC now says.
5. When the user is happy, `copilot.checkpoint(label="<done thing>")`.

### Schematic style (match what's already on the sheet)

- Rails (GND, +3.3V, +5V, VM...) connect with **power symbols** via `copilot.connect_power`, never with text labels. The engine's `connect_to_net` / `batch_add_and_connect` drop a text label on every pin and can draw diagonal wires; use them only for signal nets.
- Wires go through `copilot.draw_wire`: straight horizontal/vertical legs on the 1.27 mm grid, junction dots at T-joins. Get exact pin ends from `copilot.pin_positions`.
- Finish every schematic edit with `copilot.tidy_schematic`. It snaps to the grid, moves reference/value text off wires, parts and other text, and re-saves in KiCad's exact file format. Then `verify_edit`; it must show no connectivity change from tidying.
- Place new parts in genuinely free space near what they connect to, not on top of other symbols.
- Never reuse a reference designator for a different part. Use the next free number.

Never claim an edit worked from the engine's reply alone. The engine has reported success for `connect_to_net` while leaving the pin unconnected; only `verify_edit` (kicad-cli netlist + ERC) is ground truth.

## Engine quirks (learned on the sample boards)

- `list_schematic_nets` in the engine **misses power-symbol nets** (`+3.3V`, `GND`...). Use `copilot.get_netlist` for connectivity questions.
- Engine calls made in the first ~10 s after the server starts can fail with "Python process ... is not running". Wait and retry.
- Labels the engine places are local labels. They join a power net of the same name, but check with `verify_edit`.
- Engine edits are file edits. If KiCad has the schematic open, `KICAD_INTERACTIVE_SCHEMATIC=true` makes eeschema reload after each write. It never auto-confirms a "discard unsaved changes" dialog, so tell the user to save their own edits before asking you to edit.
- PCB edits go live over KiCad's IPC API only when the board is open in pcbnew with **Preferences > Plugins > Enable IPC API server** on. Otherwise they are file edits too.

## Review

- `copilot.review_design(run_drc=...)` gives measured findings. **Any error = FAIL**; don't argue a measured error away.
- DRC takes ~1 min on a 4-layer board. Only run it when asked or before fab.
- After the measured review, add your own judgement in a separate section and label it as opinion: topology, component choices, thermal, layout risks, anything datasheet-dependent.
- Findings carry `refs`. The live view's Review panel lets the user click a finding to jump to those parts.

## SPICE

- `copilot.spice_check` builds a deck from the schematic (R, C, L, ferrite, diodes, zeners, LEDs are automatic) and returns pass/fail per check.
- ICs are not modeled. Stand in for them with `loads` (current draw) and `sources` (rails), or write an element line via `models` plus `.model`/`.subckt` text in `extra`.
- Use `simulator="both"` when a result matters: it runs LTspice and ngspice and reports disagreements.
- Always state what was and wasn't modeled (`coverage` in the result).

## Repo layout

- `copilot/`: our MCP server (Python). `server.py` lists the tools; `review.py` holds the rules; `spice.py` handles SPICE; `viewer.py` + `static/` are the live view.
- `vendor/KiCAD-MCP-Server/`: the engine (git submodule, pinned). Don't edit it here; upstream fixes as PRs.
- `sample_boards/`: the user's originals. **Never edit these.** Copy into `workspace/` to experiment.
- `tests/`: run with `.venv/Scripts/python.exe -m pytest`.
- `fab-profiles/`: fab limits used by the review (`jlcpcb-2layer`, `jlcpcb-4layer`).

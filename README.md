# AI PCB Copilot

Prompt an LLM to **read, review, simulate and edit KiCad projects** while you watch the changes in a browser.

It has two parts, both MCP servers, so any MCP client can use them (Claude Code, Claude Desktop, Cursor, opencode + a local model):

| Server | What it does |
|---|---|
| `kicad` | The editing engine: [mixelpixx/KiCAD-MCP-Server](https://github.com/mixelpixx/KiCAD-MCP-Server) v2.8.2 (MIT). ~240 tools: place, wire and label symbols; footprints; routing; exports; JLCPCB parts. |
| `copilot` | This repo. Checkpoints and undo, pin-level netlist diff, `verify_edit`, measured design review, SPICE on **LTspice and ngspice**, and a live browser view. |

See [PLAN.md](PLAN.md) for the design and roadmap.

## Setup (Windows)

You need KiCad 9 or 10, Node 18+, Python 3.11+ and git. LTspice is optional; ngspice comes with KiCad.

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

The script clones the engine at its pinned tag, builds it against KiCad's Python, installs the copilot and runs the tests.

## Use it with Claude Code

```powershell
cd "AI PCB Generator"
claude          # approve the 'kicad' and 'copilot' servers the first time
```

Example prompts:

- *"Open workspace/fc and start the live view."*
- *"Review this board."* (uses the `review-schematic` skill)
- *"Add a 10k pull-up from nFAULT to 3.3V."* (uses `edit-schematic`: checkpoint → edit → verify → keep or undo)
- *"Simulate the BEMF dividers at 25.2 V and check the ADC pins stay under 3.3 V, on both LTspice and ngspice."* (uses `simulate`)
- *"Undo that."*

Work on copies. `sample_boards/` holds the originals; copy a board into `workspace/` and point the agent there.

### Seeing changes live

- **Browser (always):** `start_live_view` serves `http://127.0.0.1:8765`. It re-renders through `kicad-cli` within about a second of every save, circles the parts changed since the last checkpoint, and lists diffs, ERC counts, checkpoints and review findings. Clicking a finding jumps to its parts. The "Interactive" tab uses KiCanvas (alpha).
  - You can also run it without an agent: `.venv\Scripts\python -m copilot.viewer workspace\fc`
- **KiCad schematic editor:** KiCad 9/10 has no API for editing an open schematic, so the agent edits the file. `.mcp.json` turns on `KICAD_INTERACTIVE_SCHEMATIC`, which makes eeschema reload after each write. **Save your own edits before asking the agent to edit.** It will never auto-confirm a "discard changes" dialog.
- **KiCad PCB editor:** truly live (undoable) when the board is open in pcbnew with *Preferences → Plugins → Enable IPC API server* on.

## Copilot tools

| Tool | Purpose |
|---|---|
| `project_status`, `start_live_view` | Session setup |
| `checkpoint`, `list_checkpoints`, `undo`, `restore_checkpoint` | Snapshots in a shadow git repo at `<project>/.copilot/git`. This never touches your own git. |
| `netlist_diff`, `verify_edit` | Pin-level diff (name-independent), ERC new/resolved, flags parts touched outside the plan and unplanned net merges |
| `get_netlist` | Ground-truth connectivity from `kicad-cli` |
| `review_design` | ERC, optional DRC with schematic parity, netlist rules, fab-rule checks. Writes `.copilot/review.md` |
| `spice_check`, `spice_build_deck`, `spice_run`, `spice_simulators` | Decks from the schematic; LTspice / ngspice / both with a cross-check; pass/fail assertions |
| `pin_positions`, `connect_power`, `draw_wire`, `tidy_schematic` | Edits in hand-drawn style: power symbols instead of text labels; straight on-grid wires with junction dots; grid snap and text de-overlap; saves byte-identical to KiCad's own file format |

**Build + route the V2 ESC in one go** (repo root, PowerShell):

```powershell
.\autopcb_run                 # build, route, finish, stitch, clean up, DRC - live progress dashboard in this window
.\autopcb_run --skip-build    # keep the current placement, just re-route
.\autopcb_logs                # watch a run from a second window (stays open; Ctrl+C to close; --once / --log)
.\autopcb_run --docker        # same run inside the autopcb-kicad container: nothing opens on the desktop
```

Everything is also logged to `logs/autopcb.log`.

**Each run ends with design checks and a commit.** After DRC, the pipeline:
1. runs `tools/esc_checks.py`. This covers what DRC can't see:
   - switching-loop cap distance
   - charge-pump caps
   - gate-drive length
   - signals crossing a phase pour
   - vias in pads
   - ground return under the crystal/MCU
   - mounting holes
   - 3D models
2. renders every copper layer and the 3D top and bottom views into `workspace/esc_v2/review/`.
3. commits the KiCad project and the review folder to the `esc-v2` branch of [Autonomous-Flight-Team/ESC_PCB](https://github.com/Autonomous-Flight-Team/ESC_PCB), using `tools/publish_branch.py`. Pass `--no-publish` to skip the commit.

**No windows.**
- Every tool starts its child processes hidden (`tools/_quiet.py`).
- `--docker` runs the whole build and route in Linux. The image is `docker/Dockerfile`: KiCad 9.0.6, numpy, numba and Pillow. It is built on first use, and the Docker engine is started headless with `docker desktop start`.
- The image has no 3D model library, so the 3D views and the git push run on Windows afterwards, also hidden.
- KiCad paths come from `tools/_env.py`. Override them with `AUTOPCB_KPY`, `KICAD_CLI` and `KICAD_SHARE`.

**Board scripts** (`tools/`, run with KiCad's Python at `vendor/KiCAD-MCP-Server/venv/Scripts/python.exe`):

| Script | Purpose |
|---|---|
| `sync_board.py` | "Update PCB from Schematic", scripted: adds and moves footprints, reloads changed library footprints, renames nets on existing copper, locks existing routing |
| `route_board.py` | Freerouting autoroute. Plane nets are left to pours. `--keep` adds routes without touching existing ones. Picks a Java new enough for Freerouting. |
| `finish_route.py` + `maze_route.py` | Routes exactly what KiCad's ratsnest still reports missing, with a clearance-aware 2-layer maze router that respects edge and top-layer keep-outs |
| `prune_tracks.py`, `stitch_islands.py`, `silk_fix.py` | Remove dangling stubs and vias; stitch pour islands; move reference designators off pads and other silkscreen |
| `build_esc_v2.py` | Generates the 4-layer V2 ESC board: placement, stackup, zones, via fields, top-layer signal keep-out |
| `drc_summary.py`, `jlc_lookup.py` | Compact DRC report; JLCPCB/LCSC part lookup (basic/extended, stock, price) |

**Review rules:**
- Errors:
  - capacitors wired only to each other (a missing supply label)
  - missing decoupling on IC supply pins
  - USB-C CC pins without pull-downs
  - unannotated parts, missing footprints
  - schematic/PCB disagreement
  - tracks or vias below the fab's limits
- Warnings:
  - missing pull-ups on I2C / open-drain lines
  - crystals without load caps
  - LEDs without a resistor
  - look-alike net names that aren't connected
  - single-pin labelled nets
  - sheets pointing at missing files
  - symbols off the page
  - project DRC limits looser than the fab
- Fab profiles live in [fab-profiles/](fab-profiles/).

## Other clients

Point any MCP client at the same two commands. The paths are relative to this folder; use absolute paths if the client starts elsewhere.

- engine: `node vendor/KiCAD-MCP-Server/dist/index.js`
- copilot: `.venv/Scripts/python.exe -m copilot.server`

**Free / local:** [opencode](https://opencode.ai) with Ollama or LM Studio. Use a model with solid tool calling (Qwen3-Coder class or bigger); small models struggle with long edit chains. The engine ships `setup-windows-opencode.ps1` for its half.

## Tests

```powershell
.venv\Scripts\python -m pytest
```

The tests cover the netlist diff, value parsing, an RC transient on both simulators with a cross-check, and the review rules against netlists from the sample boards.

## Known limits

- **Schematic edits are file edits.** "Live" in eeschema means it reloads after each write.
- **The engine's own net listing misses power-symbol nets.** The copilot uses `kicad-cli` as ground truth.
- **The engine can report a connection as done when it isn't.** `verify_edit` catches this, which is why the edit loop always verifies.
- **SPICE models R/C/L/ferrite/diode/zener/LED automatically.** ICs need a `loads` stand-in or a `models` line.
- **DRC takes about 1 minute on a 4-layer board,** so it's opt-in.

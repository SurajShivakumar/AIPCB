# AI PCB Copilot — Plan

**Goal:** Prompt an LLM (Claude Code, Claude Desktop, Cursor, or a free/local model) to read a KiCad project, review it, and edit the schematic and PCB while you watch the changes happen. Later, generate new designs from a prompt and keep editing them the same way.

**Status (2026-10-07):** Phases 0–3 are built, plus SPICE (LTspice + ngspice), which was added at your request. Phases 4–5 are next. Setup and usage are in [README.md](README.md).

## 0. Build log

| Phase | State | Evidence |
|---|---|---|
| 0: Baseline | ✅ tasks 1–4 · ⏳ task 5 | Engine v2.8.2 built against KiCad 9.0.6's Python (venv with system site-packages). List, ERC, add-part and connect all work on the ESC copy. Task 5 (live PCB over IPC) needs you to open the board in pcbnew with the IPC API enabled. |
| 1: Safety layer | ✅ | `checkpoint` / `undo` / `restore` in a shadow git repo (`<project>/.copilot/git`). The pin-level diff reports an added pull-up as exactly `+ component R99, R99.1 joined ENABLE, R99.2 joined GND`. |
| 2: Live viewer | ✅ (needs your eyes) | Redraws in about 1–2 s via `kicad-cli` SVG, rendered from a private snapshot of the files. Circles parts changed since the last checkpoint. Sidebar shows diffs, ERC counts, review and checkpoints. KiCanvas is vendored as the "Interactive" tab (alpha). |
| 3: Review engine | ✅ | 14 measured rules plus grouped ERC/DRC/parity findings plus fab checks. Found real issues on both sample boards (below). |
| SPICE | ✅ | LTspice 26 in batch mode, and ngspice via KiCad's `ngspice.dll` (run in a subprocess). Decks are built from the netlist. Pass/fail checks run in Python; `both` cross-checks the two simulators. |
| 4: Generate | ⏳ next | |
| 5: Free route | ⏳ next | |

**Found on your boards (measured):**
- **flight_controller:**
  - C1, C2, C3, C4, C8, C10 and C13 were wired only to each other, decoupling nothing. This was also why the PCB disagreed with the schematic (7 `net_conflict` parity errors).
  - Fixed on the `workspace/fc` copy through the full loop: one `connect_to_net`, then `verify_edit` (OK; the intended merge was recognized), then `review_design`. The verdict went from FAIL to PASS and the parity errors cleared. Your original is untouched.
- **Both boards:** have sheet boxes pointing at a missing `untitled.kicad_sch`, have most symbols placed off the A4 page, and have project DRC limits looser than JLCPCB.
- **ESC:** has a 0.1 mm track, below JLCPCB's 2-layer minimum of 0.127 mm.
- **SPICE (ESC, LTspice and ngspice agree):** the BEMF divider puts 2.12 V on the ADC pin at 24 V phase, so the pin hits 3.3 V at about 37 V.

**Learned about the engine (now encoded in CLAUDE.md):**
- Its net listing misses power-symbol nets.
- It can report `connect_to_net` as successful while leaving a pin unconnected. This was seen once, intermittently; `verify_edit` caught it.
- Calls made in its first ~10 s fail: the transport goes live before the Python worker (an upstream race, worth a PR).

**What changed from the plan:**
- Rendering defaults to `kicad-cli` SVG (exact), with KiCanvas as an optional tab.
- The review is one module (`copilot/review.py`), not a package.
- The engine is cloned by `setup.ps1` at a pinned tag rather than added as a submodule.
- MCP SDK 2.x is used (`MCPServer`, formerly FastMCP).

---

## 1. TL;DR

- **We won't build a KiCad MCP from scratch.** We'll use [mixelpixx/KiCAD-MCP-Server](https://github.com/mixelpixx/KiCAD-MCP-Server) as the editing engine:
  - MIT licence, 244 tools.
  - About 2,300 tests, with CI running against KiCad 8, 9 and 10.
  - Supported on Windows.
  - It already handles schematic authoring, ERC/DRC, exports, JLCPCB parts and Freerouting.
- **We'll write a small companion MCP server of our own ("copilot") next to it.** It adds what the engine doesn't have:
  1. A **live view** in the browser that redraws on every change the agent makes.
  2. **Checkpoints and undo**, plus a **netlist diff** after each edit, so every AI change can be checked and reverted.
  3. A **design-review engine**. Hard, measured checks always override the LLM's opinion (an idea taken from the hackathon pipeline).
  4. **Prompt-to-design**, using the tscircuit pipeline from `pcb-ai`. It produces a KiCad project, which is then edited with the same tools.
- **It works with any LLM, because both parts are MCP servers.** Claude Code is the main client. A no-cost route exists through opencode or Continue with a local model (Ollama or LM Studio).

---

## 2. What "live" actually means

This limitation decides the whole design, so it comes first.

| Editor | Can an external program change the open design live? | How we get "live" |
|---|---|---|
| **PCB editor (pcbnew)** | **Yes.** KiCad 9/10 has an IPC API (`kicad-python`). Changes appear immediately and can be undone with Ctrl+Z. | mixelpixx's IPC backend (about 22 PCB operations live; the rest fall back to editing the file). |
| **Schematic editor (eeschema)** | **No.** KiCad 10's IPC API ships schematic types but **no schematic commands**. Work on adding them is a proof of concept in a fork, with no release date. | 1) The agent edits the `.kicad_sch` file. 2) Our browser viewer redraws instantly. 3) On Windows, mixelpixx's `KICAD_INTERACTIVE_SCHEMATIC=true` mode sends eeschema the "Revert" command, so it reloads from disk after each write. |

**What this means in practice:**
- PCB editing is truly live.
- Schematic editing follows a write-then-reload loop:
  - The browser viewer shows each change within about a second.
  - KiCad itself reloads automatically on Windows.
- **Rule: don't hand-edit a schematic in KiCad while the agent is writing to it.** Whoever saves last wins.
- When KiCad ships schematic IPC commands, we swap in a new backend. The tools the LLM sees stay the same.

---

## 3. What we found in the two codebases

### 3.1 mixelpixx/KiCAD-MCP-Server (v2.8.2, last commit 2026-10-01)

**Architecture:**
- A TypeScript MCP server starts and manages a long-running Python worker (`python/kicad_interface.py`).
- The worker reaches KiCad in four ways:
  - **SWIG `pcbnew`:** edits PCB files.
  - **IPC API:** live PCB editing, experimental.
  - **Direct S-expression text editing, plus `kicad-skip` for reading:** all schematic edits.
  - **`kicad-cli`:** ERC, DRC, netlists, exports and renders.

**Strengths:**
- **Schematic tools (65):**
  - Add, edit and move symbols; wires; labels; no-connects.
  - `batch_add_and_connect`, which places and wires a whole subcircuit in one call.
  - Hierarchical sheets.
  - Pin locations and net queries.
  - Lint checks: floating labels, orphaned wires, off-grid items, overlaps.
  - Rendering the schematic to PNG/SVG so the LLM can look at it.
- **Other tools:** ERC/DRC via `kicad-cli`, with JSON output. JLCPCB catalogue (2.5M parts, local SQLite), Digi-Key and LCSC datasheets. Freerouting. Every export format.
- **Build quality:** good tests and CI, an active maintainer, and Windows setup scripts (`setup-windows.ps1`).

**Weaknesses:**
- **Schematic writes are custom text surgery.** The changelog shows a history of file-corruption bugs, all since fixed. This is the riskiest part of the code, which is why we add checkpoints and a netlist diff (Section 5.2).
- **No real design-review engine.** It only has prompt templates. The maintainer's design-review audits are in **Konnect**, a separate product (see 3.3).
- The IPC backend covers only part of the PCB tools. DRC runs on the *saved* file. Filling zones via SWIG crashes (filling via IPC works).

### 3.2 pcb-ai (your hackathon pipeline, `nvidia-spark-hacks/pcb-ai`)

**What it is:**
- A LangGraph loop in TypeScript: prompt → parts plan → **tscircuit TSX** → compile to Circuit JSON → measured checks → 3 LLM reviewers in parallel → "chief" verdict → SEARCH/REPLACE revisions.
- It writes KiCad files through `circuit-json-to-kicad` (`src/exports.ts`). That export only runs in the HTTP service, not in the CLI's `finalize` step.
- **Microsoft SchGen was never integrated.** It was blocked because it needs KiCad 8 and an Azure/OpenRouter key. Only its netlist F1 metric was adopted.

**Reusable pieces** (best first; ✅ = we'll bring it over):

| Module | Use in the new project |
|---|---|
| ✅ "Measured vs judged" architecture (`chief.ts`): a measured hard failure forces `pass:false` | The core rule of our review engine |
| ✅ `src/bench/netlist-similarity.ts`: pin-level Jaccard/F1 that ignores net names | Netlist diff after each edit ("did this edit change only what it said it would?") |
| ✅ Review/Finding/Verdict/WorkOrder Zod schemas + reviewer prompts | Structured review output |
| ✅ `src/kicad/drc.ts` (`kicad-cli` wrapper) + `src/dfm/profile.ts` (fab rules read from `.kicad_pro`) | DFM checks against your fab's rules |
| ✅ `src/placement/constraints.ts`: closed grammar for placement rules | Machine-checkable layout assertions ("USB-C at board edge") |
| ✅ Whole generate path: parts agent → TSX → `circuit-json-to-kicad` | Phase 4, prompt-to-design |
| `src/spice/*` (DC operating-point deck, coverage accounting) | Later: an SPICE sanity check on a KiCad netlist |
| `cad-generation/.../sourcing/providers.py` (Nexar, Digi-Key, Mouser, LCSC) | Optional: more parts sources besides JLCPCB |

**What we leave behind:**
- Code tied to the DGX Spark: vLLM/NVFP4 endpoints, aarch64 vendor scripts, hard-coded ports.
- tscircuit's generic footprints with no real part numbers.
- The custom PCG physics solver (niche).

**Known gaps:**
- It never completed a full loop end to end with a real model.
- KiCad DRC and tscircuit DRC disagreed (46 errors vs 0).
- The default via stack fails DFM.
- All of these get fixed or avoided once KiCad becomes the source of truth.

### 3.3 Other projects checked (October 2026)

| Project | Notes | Verdict |
|---|---|---|
| **Konnect** ([mixelpixx/Konnect](https://github.com/mixelpixx/Konnect), also published as obhox/kicad-mcp) | Rust rewrite, KiCad 10 plugin, 171–185 tools, includes design-review audits. **AGPL-3.0; free for non-commercial use only, paid commercial licence.** Schematic is still edited through files. | Good for hobby use. **If this is for Numurus work, the licence rules it out**, unless you buy a licence. Kept as a reference. |
| [paul356/KiCad-AI-Assistant](https://github.com/paul356/KiCad-AI-Assistant) | MIT. A chat panel *inside* KiCad 10 with 80+ tools. Same schematic limitation (needs File → Reload). | Possible later alternative to an external client (Phase 6). |
| [circuit-synth/kicad-sch-api](https://github.com/circuit-synth/kicad-sch-api) (+ its MCP) | MIT. Python schematic read/write that matches KiCad's own formatting byte for byte; net tracing. Officially KiCad 7/8. | **Candidate parser for our companion's read-only analysis.** Must be checked against KiCad 9/10 files. |
| [Seeed-Studio/kicad-mcp-server](https://github.com/Seeed-Studio/kicad-mcp-server) | MIT. Strong pin-level tracing based on the netlist; editing is experimental. | Reference for review logic. |
| ProductOfAmerica/mcp-server-kicad, kicad-mcp-pro | Other broad MCP servers. | Not needed; mixelpixx covers this ground and is more mature. |
| **atopile**, circuit-synth | Design described in code, compiled to KiCad. | Alternative generation route (see 4.2). |
| [KiCanvas](https://github.com/theacodes/kicanvas) | Embeddable browser viewer for `.kicad_sch` / `.kicad_pcb`. Alpha. | **Our live viewer.** Must be checked against KiCad 9/10 files. |

---

## 4. Approaches considered

### 4.1 Recommended: "KiCad is the source of truth" (mixelpixx + copilot companion)

```
 ┌───────────────────────┐   MCP (stdio)   ┌──────────────────────────────┐
 │ LLM client            │◄───────────────►│ kicad-mcp (mixelpixx, as-is) │──► .kicad_sch / .kicad_pcb
 │ Claude Code / Desktop │                 │ edit · ERC/DRC · parts · ... │──► KiCad IPC (live PCB)
 │ Cursor / opencode+    │   MCP (stdio)   ├──────────────────────────────┤
 │ Ollama (free)         │◄───────────────►│ copilot (ours, Python)       │
 └───────────────────────┘                 │ checkpoint/undo · net diff   │
                                           │ review engine · generate     │
                                           │ live-view server (SSE)       │
                                           └──────────────┬───────────────┘
                                                          │ file watcher
                                                          ▼
                                            Browser: KiCanvas live view
                                            (redraws on every save, highlights changes)
```

**Why this approach:**
- It works on your existing hand-drawn KiCad designs, which is the main thing you asked for.
- It reuses about 57k lines of tested code instead of rewriting it.
- Everything stays MIT.

### 4.2 Alternative: "Code is the source of truth" (tscircuit or atopile)

- The LLM edits a `.tsx` or `.ato` file, and a compiler produces the KiCad project.
- **Pros:**
  - Diffs are ordinary text and easy to review in git.
  - LLMs are very good at editing code.
  - The pcb-ai loop already exists.
- **Cons:**
  - It **can't edit an existing KiCad schematic**; it's one-way only.
  - Auto-placed schematics are hard to read.
  - Generic footprints.
- **Decision:** use this only for the *generate-from-scratch* step (Phase 4). Its output goes into the recommended setup for refinement.

### 4.3 Rejected

- **Building a new full KiCad MCP:** it duplicates mixelpixx.
- **Konnect as the base:** AGPL/commercial licensing.
- **Waiting for the eeschema IPC API:** no release date.
- **SchGen:** needs KiCad 8 and paid API keys; the fine-tuned model isn't worth hosting.

---

## 5. Components in detail

### 5.1 Editing engine: mixelpixx KiCAD-MCP-Server (unmodified)

- Pin a release tag. Install with `setup-windows.ps1`.
- Settings:
  - `KICAD_BACKEND=auto`: IPC when the board is open in KiCad, SWIG otherwise.
  - `KICAD_INTERACTIVE_SCHEMATIC=true`: eeschema reloads automatically after each write.
- Fixes we need go upstream as PRs instead of living in a fork.

### 5.2 Safety layer (copilot): checkpoints, undo, netlist diff

- `checkpoint(label)`: a git commit of the project folder, in a hidden `.copilot/` repo or the project's own repo.
- `undo(n)` / `restore(checkpoint)`. `list_checkpoints()`.
- `netlist_diff(from, to)`:
  - Runs `kicad-cli sch export netlist` for both versions.
  - Compares pin-level connectivity (ported from `netlist-similarity.ts`).
  - Reports added/removed connections, renamed nets and new or deleted parts.
- `verify_edit(intent)`: compares the diff with what the agent said it would do and flags anything it didn't mention.
- **Workflow rule (in CLAUDE.md / skill):** checkpoint → edit → ERC + netlist diff → show the user → keep or undo.

### 5.3 Live viewer (copilot)

- A small local web server, `http://localhost:8765`.
- The page embeds KiCanvas, served locally, plus a sidebar with:
  - a list of checkpoints;
  - the latest netlist diff;
  - the latest review findings.
- A file watcher on `*.kicad_sch` / `*.kicad_pcb` pushes changes over Server-Sent Events. The view redraws and **highlights the components and nets that changed**.
- Fallback if KiCanvas can't read KiCad 10 files: render with `kicad-cli sch export svg` and show the SVG. Slower, but always correct.

### 5.4 Review engine (copilot)

**Two layers:**
- **Measured layer:** gives a pass/fail result and can block.
- **Judged layer:** LLM findings.
- Measured failures always override the LLM's verdict (as in pcb-ai `chief.ts`).

**Measured checks** (Python, on the `kicad-cli` netlist plus the parsed schematic):
- ERC and DRC via `kicad-cli` (JSON).
- DFM against the fab rules in a `.kicad_pro` (ported from `dfm/profile.ts`). We ship a JLCPCB default profile.
- Rules on the netlist:
  - every IC power pin has a decoupling cap on the same net, within N parts;
  - I2C has pull-ups, and only one set per bus;
  - no floating inputs; unused pins have no-connects;
  - PWR_FLAG and power-net consistency;
  - net names suggest the same rail but aren't connected (e.g. `3V3` vs `+3.3V`);
  - polarized parts and the orientation of reverse-protection parts;
  - connectors have ESD/TVS parts on external I/O (warning only);
  - every part has a footprint, an MPN/LCSC number, and a value.
- BOM health: missing fields, parts at JLCPCB that are out of stock or "extended" (via mixelpixx's JLCPCB tools).

**Judged checks:**
- The LLM gets:
  - a **text digest** of the measured results (the pcb-ai `layout-digest` idea: numbers, not just images);
  - schematic/PCB renders;
  - relevant datasheet excerpts.
- It returns findings in the `Finding` schema: `severity`, `refs`, `evidence`, `suggested_fix`.

**Output:**
- `review_design()` returns a structured report.
- It also writes `review.md` into the project and shows the findings in the viewer sidebar.
- Each finding can turn into a work order the agent can apply. Every applied fix still goes through checkpoint + diff.

### 5.5 Generate from a prompt (copilot, Phase 4)

**Pipeline:**
1. Port the pcb-ai `parts` → `design` → compile loop.
2. **Ground parts in real part numbers** using the JLCPCB catalogue: each part gets an LCSC number and a real KiCad footprint, not a generic tscircuit string.
3. Export with `circuit-json-to-kicad`.
4. Open the result in KiCad. From here it's an ordinary project: review it and edit it with 5.1–5.4.

- This step runs as a Node subprocess that copilot calls. pcb-ai is TypeScript, and porting the tscircuit parts makes no sense.
- **Alternative to evaluate:** have the LLM build the schematic directly with mixelpixx's `batch_add_and_connect`, one subcircuit at a time (power, MCU, connectors). This skips tscircuit completely and gives cleaner, hand-placed schematics. **Spike both in Phase 4 and keep whichever produces the more readable result.**

### 5.6 LLM clients and running at no cost

| Client | Cost | Notes |
|---|---|---|
| **Claude Code** (CLI / VS Code) | Your existing Claude plan | Main client. Add a `CLAUDE.md`, **skills** (e.g. `/review-schematic`, `/add-subcircuit`) and a **design-reviewer subagent**. |
| Claude Desktop | Same plan | Same MCP configuration. |
| **opencode** or **Continue** + **Ollama / LM Studio** | Free, runs locally | mixelpixx already ships `setup-windows-opencode.ps1`. A good tool-calling model is needed (Qwen3-Coder class or larger). Expect weaker results on long edits. |
| Cursor, VS Code Copilot agent mode, Gemini CLI | Free tiers exist, with limits that change | All three speak MCP; same configuration. |

- **Copilot's own LLM calls** (judged review, generation) go through one provider-agnostic interface: OpenAI-compatible, Anthropic or Ollama.
- **Default: the copilot makes no LLM calls of its own.** It returns the digest, and the *client's* LLM does the judging, so no API key is needed.

---

## 6. Repository layout (proposed)

```
AI PCB Generator/
├── PLAN.md                     ← this file
├── CLAUDE.md                   ← agent workflow rules (checkpoint → edit → verify)
├── .mcp.json                   ← registers kicad-mcp + copilot for Claude Code
├── vendor/KiCAD-MCP-Server/    ← git submodule, pinned tag
├── copilot/                    ← our MCP server (Python, FastMCP)
│   ├── server.py               ← MCP tool definitions
│   ├── checkpoints.py          ← git snapshots / undo
│   ├── netlist.py              ← kicad-cli netlist parse + pin-level diff
│   ├── review/
│   │   ├── measured.py         ← ERC/DRC/DFM wrappers + netlist rules
│   │   ├── rules/              ← one file per rule (decoupling, i2c, ...)
│   │   ├── digest.py           ← LLM-friendly text summary
│   │   └── schemas.py          ← Finding / Verdict / WorkOrder (pydantic)
│   ├── viewer/                 ← web server + SSE + static page (KiCanvas)
│   └── generate/               ← calls into gen/ (Phase 4)
├── gen/                        ← ported pcb-ai TS (parts → tscircuit → KiCad)
├── .claude/
│   ├── skills/                 ← review-schematic, add-subcircuit, prep-for-fab
│   └── agents/design-reviewer.md
├── fab-profiles/jlcpcb-2layer.kicad_pro
└── examples/                   ← test projects + golden netlists for regression tests
```

---

## 7. Phases

Each phase ends with something you can use and a pass/fail demo.

**Phase 0: Baseline (½–1 day)**
- Install mixelpixx on Windows against your KiCad. Register it in Claude Code through `.mcp.json`.
- Run 5 scripted tasks on a sample project:
  1. "list components";
  2. "add 10k pull-up from SDA to 3V3";
  3. "add decoupling caps to U1";
  4. "run ERC and fix the errors";
  5. "move U1 on the PCB" (IPC, live).
- Write down every failure.
- ✅ **Done when:** tasks 1–4 succeed with the file still opening in KiCad, and task 5 shows up live in pcbnew.

**Phase 1: Safety layer (1–2 days)**
- Copilot skeleton (FastMCP) with `checkpoint`, `undo`, `list_checkpoints`, `netlist_diff`, `verify_edit`.
- Write `CLAUDE.md` with the edit workflow.
- ✅ **Done when:** an unwanted edit can be undone in one prompt, and the diff correctly reports an added pull-up as exactly "+R?: SDA↔3V3".

**Phase 2: Live viewer (1–2 days)**
- KiCanvas page + file watcher + SSE + highlighting of changes + checkpoint/diff sidebar.
- Check that KiCanvas renders KiCad 9/10 files; switch to `kicad-cli` SVG if it doesn't.
- ✅ **Done when:** the browser redraws less than 2 s after the agent edits, with the changed parts highlighted.

**Phase 3: Review engine (3–5 days)**
- Measured checks: ERC/DRC/DFM + the first 6 netlist rules + BOM health.
- Digest. `review_design` tool. `/review-schematic` skill + design-reviewer subagent.
- Test on 2–3 real boards: seed them with known bugs and measure how many are caught.
- ✅ **Done when:** at least 80% of the seeded bugs are caught, with no measured false positives on a clean board.

**Phase 4: Generate from a prompt (3–5 days)**
- Spike A: port pcb-ai to tscircuit → KiCad, with JLCPCB part grounding.
- Spike B: direct `batch_add_and_connect` construction, one subcircuit at a time.
- Keep the better one.
- ✅ **Done when:** "ESP32-S3 board with USB-C, LDO, 2 LEDs, I2C header" produces a KiCad project that passes ERC with zero errors and that you'd call readable.

**Phase 5: Model-agnostic + free route (1–2 days)**
- opencode + Ollama configuration and notes on which local models handle the tool set.
- Rerun the Phase 0 tasks on a local model and record the success rate.
- ✅ **Done when:** documented results for at least one free route.

**Phase 6: Stretch**
- PCB review (placement grammar from pcb-ai, critical-net length checks).
- ~~SPICE sanity check~~ (done early, with LTspice + ngspice). Next step: an IC model library (LDO/buck behavioral templates) so regulators simulate without hand-written models.
- In-KiCad chat panel (paul356-style).
- Schematic IPC backend once KiCad ships it.
- Mouser/Digi-Key sourcing via the F2 providers.

---

## 8. Risks

| Risk | Mitigation |
|---|---|
| Schematic text edits corrupt a file | Checkpoint before every edit. Open the file with `kicad-cli` after each write. Netlist diff. One-step undo. |
| The user and the agent edit the same schematic at once | Documented rule. Copilot warns when the file's modification time changes outside the agent's own writes. |
| KiCanvas doesn't support the KiCad 9/10 format | `kicad-cli` SVG fallback. |
| mixelpixx moves its effort to Konnect and the MIT version goes stale | Pin a tag. Our layer only talks to it through MCP, so the engine can be swapped (e.g. for kicad-sch-api). |
| Local models are weak at long tool chains | Skills that split work into small steps. Batch tools. Clear notes on expected quality. |
| LLM reviews are noisy | Measured checks override the LLM. Every finding needs `evidence` (refs and nets). Track false positives on the test boards. |

---

## 9. Decisions

**Answered:**
- **Personal use.** Licensing isn't a constraint, though everything used so far is MIT anyway.
- **Test boards:** `sample_boards/` (ESC PCB V1, flight_controller), copied into `workspace/` for experiments.
- **SPICE:** add LTspice. Done, with ngspice as a second, always-available simulator.

**Defaults taken, which you can change:**
1. **KiCad 9.0.6 stays**; nothing was upgraded. Everything also supports 10.x if you upgrade.
2. **Client:** Claude Code first. The opencode + Ollama free route is Phase 5.
3. **Fab:** JLCPCB profiles (2-layer and 4-layer, picked automatically from the board's layer count).
4. **Priority:** live editing and review are done. Generation (Phase 4) is next.

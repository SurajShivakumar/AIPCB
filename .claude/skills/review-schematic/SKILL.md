---
name: review-schematic
description: Full design review of a KiCad project — measured checks (ERC, netlist rules, fab rules, optional DRC), targeted SPICE checks, then an engineering opinion section. Use when the user asks to review, audit, sanity-check or "look over" a schematic or board.
---

# Review a KiCad design

1. `copilot.project_status(project=...)`, then `copilot.start_live_view` and give the user the URL.
2. `copilot.review_design()`. Add `run_drc=true` if the user mentions layout, fab or ordering, or the board is near done.
3. `copilot.get_netlist()` to understand the circuit: find the rails, ICs, connectors and interfaces.
4. Pick 1–3 things worth simulating and run `copilot.spice_check` with `simulator="both"`. Typical picks:
   - voltage dividers into ADC pins (max input voltage vs. the pin's limit)
   - LED currents
   - regulator feedback dividers (compute Vout)
   - RC filters (cut-off frequency)
5. Report in this order:
   - **Verdict**: PASS/FAIL from the measured review (errors force FAIL).
   - **Measured findings**: errors first, then warnings. Each one gets refs, nets and the suggested fix. Merge duplicates; skip `info` noise unless it matters.
   - **Simulation**: what was modeled, the checks and their numbers, and whether LTspice and ngspice agreed.
   - **Engineering opinion** (label it as opinion): datasheet-level concerns, topology, protection, thermal, layout risks.
6. Offer to fix specific findings. Don't edit anything without the user agreeing. Edits follow the edit loop in CLAUDE.md.

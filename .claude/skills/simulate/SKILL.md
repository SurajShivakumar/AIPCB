---
name: simulate
description: Simulate part of a KiCad schematic with LTspice and/or ngspice to double-check a circuit (dividers, filters, LED currents, regulator outputs, transients). Use when the user asks to simulate, verify a value, check a voltage/current, or "run SPICE/LTspice".
---

# Simulate part of a schematic

1. Find the subcircuit: `copilot.get_netlist(refs=[...])` or `copilot.get_netlist(nets=[...])`.
2. Decide what drives it and what loads it:
   - `sources`: `{"VBAT": 16.8}`, or a waveform like `{"/IN": "PULSE(0 3.3 0 1n 1n 5u 10u)"}`.
   - `loads`: `{"+3.3V": "150m"}` stands in for ICs drawing current.
   - Things that aren't modeled automatically (ICs, transistors) go in `models`, with `.model`/`.subckt` text in `extra`. A behavioral source is often enough, e.g. an LDO: `{"U1": "B_U1 {VOUT} 0 V=min(V({VIN})-0.3, 3.3)"}`.
3. Choose the analysis: `.op` for DC levels, `.tran <step> <stop>` for timing and ripple, `.ac dec 50 <f1> <f2>` for filters.
4. Write the checks as numbers with limits, e.g. `{"expr": "V(BEMF_A)", "stat": "max", "max": 3.3}`. Stats are `final`, `max`, `min`, `avg`, `rms`, `pp`, and `at` (with `"at": t`).
5. `copilot.spice_check(..., simulator="both")`. If the two simulators disagree, say so and investigate. It usually means a model or convergence difference.
6. Report:
   - the deck's coverage (modeled / not modeled)
   - every check with its value and pass/fail
   - what this proves and what it doesn't (e.g. "ICs replaced by a 150 mA load")

To tweak a deck by hand: `copilot.spice_build_deck`, edit the text, then `copilot.spice_run(deck, checks, node_map)`.

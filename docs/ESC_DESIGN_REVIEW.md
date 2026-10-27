# ESC PCB: design review, V1 fixes and V2 (4-layer) proposal

_AFC hardware team · generated with AI PCB Copilot · October 2026_

Every number here comes from one of three sources:
- a measurement on the board files (kicad-cli DRC/ERC/netlist);
- a datasheet value, with the source given;
- a SPICE run on both LTspice and ngspice.

Anything that is opinion is labelled as opinion.

---

## 0. Status

| | V1 (2-layer, your board, fixed) | V2 (4-layer, double-sided, new) |
|---|---|---|
| Location | `workspace/esc/ESC PCB V1.*` | `workspace/esc_v2/ESC PCB V2.*` |
| Schematic ERC | **0 errors, 0 warnings** (was 0 / 102) | **0 / 0** |
| Schematic text overlaps | **0** (was 14) | **0** (was 36) |
| PCB DRC | **0 violations, 0 unconnected** | **0 violations, 0 unconnected** (see §2.4) |
| Schematic ↔ PCB parity | **clean** | **clean** |
| Copilot review | **PASS, 0 errors / 0 warnings / 0 notes** | — |

### What changed on V1

**Schematic**
- **Signal input:** R19 changed from 100k to 220 Ω, and a 100k pull-down (R29) was added. With 100k, the PB4 pin only reached 1.52 V by the end of a DShot600 "0" bit, and peaked at 1.89 V; it needs at least 2.31 V. This was simulated on LTspice and ngspice, and they agree.
- **Overcurrent:** the DRV8320H VDS pin was floating, which selects a 0.6 V trip. That trips at about 150–220 A on these FETs, which is no protection. **R30 (75k to AGND) gives 0.26 V**, so the trip is about 96 A cold and about 65 A hot.
- **3.3 V supply:** an AMS1117-3.3 LDO now runs from the PDB 5 V, through a new **5V** wire pad. The 3V3 pad is now an output only.
- **Cleanup:**
  - Removed three empty sheet boxes that pointed at a missing `untitled.kicad_sch`.
  - Moved to an A1 sheet, because 32 symbols were outside the A4 border.
  - Snapped 7 symbols and 75 wires to the grid.
  - Gave the custom MOSFET and driver symbols proper pin types.
  - Used power symbols (not text labels) for every new rail connection.
  - Repositioned overlapping reference and value text.
- **BOM:** every purchasable part now has an LCSC number, MPN and manufacturer.
  - "5Ω" became **5.1 Ω**, because 5 Ω isn't a stocked E-series value.
  - D1 is now **Red** (V_f ≈ 2.0 V). With a green LED (V_f ≈ 3.1 V) from 3.3 V through 1k, it would barely light.
  - Y1 is set to **8 MHz with C_L = 10 pF** (load caps: 2 × (10 − ~4 pF stray) ≈ 12 pF ✓). **Check this matches your firmware's HSE setting.**
- **J1:** the 1.00 mm through-hole socket isn't stocked at JLCPCB or LCSC, so it's now a **JST-SH SM04B-SRSS-TB** (C160404). Same pinout.

**Libraries**
- The missing **3D models** from the team repo are now in `3dmodels/` and linked to their footprints.
- The custom footprints and symbols now live in project libraries under their **original library names**, so nothing needed relinking.
- **Thermal vias:** the DRV8320 thermal-pad vias went from **0.2 mm to 0.3 mm** drill, which is JLCPCB's standard minimum.

**PCB**
- Design rules raised from 0 to JLCPCB minimums: 0.127 mm track and clearance, 0.3 mm drill, 0.13 mm annular ring.
- Widened a 0.1 mm track.
- Turned on island removal.
- The 6 new parts are placed and routed.
- 72 GND stitching vias added.
- Silkscreen cleaned up.

---

## 1. Board size (Q1)

**V1 as received:** 63.05 × 71.55 mm, which is **4,511 mm²**, on 2 layers with every part on the top. The footprints themselves cover only about **1,445 mm² (32 %)**; the rest is routing and pours.

**V2 as built:** **48 × 58 mm = 2,784 mm²**. That is **38 % less area**, and it keeps all four M3 mounting holes and the same 5 × 10 mm battery and motor wire pads.

**How much smaller it could go (opinion, with the numbers that drive it):**

| Change | Area |
|---|---|
| V2 as built | 2,784 mm² |
| Drop the 4 M3 holes (zip-tie or heat-shrink mount, which is common for ESCs) | ~44 × 54 = 2,376 mm² (−47 % vs V1) |
| Also move the 10 mm electrolytic off-board onto the battery leads (see §4) | ~42 × 48 = 2,016 mm² (−55 %) |

The hard floor is the power stage: six 5 × 6 mm FETs, three 5 × 10 mm motor pads and two 5 × 10 mm battery pads. Below roughly 40 × 45 mm, you'd need smaller wire pads or through-hole wire connections.

---

## 2. 4-layer, double-sided layout (Q2)

### 2.1 Stackup (JLCPCB standard 4-layer, 1.6 mm)

| Layer | Use |
|---|---|
| **F.Cu** | The power stage: 6 FETs as 3 half-bridges under the 3 motor pads. Phase pours run from the FET pair to its motor pad. VM islands sit under the high-side drains; GND islands sit under the low-side sources. Also: bulk caps, TVS, battery pads, wire pads, the SWD connector. |
| **In1.Cu** | **Solid GND plane** across the whole board, as the return for everything. |
| **In2.Cu** | **VM power plane** under the power half, plus a **+3.3 V plane** under the logic half (MCU, LDO). |
| **B.Cu** | The logic: DRV8320, STM32, LDO, every gate resistor and passive. Phase pours are duplicated under the FETs to carry current in parallel with F.Cu; GND fill everywhere else. |

### 2.2 Placement logic

- **Half-bridges are side-by-side pairs.** The low-side FET sits at 90° and the high-side at 270°. That puts both phase terminals on the **top edge** of the pair, facing the motor pad, so the phase current loop is a few millimetres of solid copper on one layer.
- **The DRV8320 sits on the bottom, centred under the FET row.** Each gate resistor and its pull-down are placed **beside their own FET's gate pin** (script-placed from the actual pad positions), so the gate loops are short.
- **The MCU sits on the bottom over the +3.3 V plane**, with the decoupling caps on both sides and the BEMF dividers in two rows below it.

### 2.3 Copper and vias

| What | How |
|---|---|
| Phases | F and B pours in parallel, joined by **31 vias per phase** |
| High-side drain (VM) | **12–15 vias per FET** into In2 |
| Low-side source (GND) | **15 vias per FET** into In1 |
| VM pour ↔ VM plane | a separate tie field |
| GND | stitching vias around the edge |
| Vias | all **0.6 / 0.3 mm**, JLCPCB standard, no extra cost |

Pours connect to pads **solid** (no thermal spokes) on the power nets, so current isn't squeezed through spokes.

**Recommended at order time:** **2 oz outer copper**. It's a JLCPCB option. With 1 oz, a 40 A phase needs roughly 15 mm of pour width for about a 20 °C rise (IPC-2221). The V2 phase pours are about 12 mm wide on each of two layers, so 1 oz is borderline at sustained 40 A and 2 oz gives margin.

### 2.4 Routing status (V2)

**Fully routed. KiCad DRC: 0 violations, 0 unconnected, schematic parity clean** (checkpoint `5967bc7696`).

One command builds and routes the board from scratch: `.\autopcb_run` (watch it with `.\autopcb_logs`; about 20 min).

**How it's routed:**
- **Power nets** are pours and via fields, as above. They're never routed as tracks.
- **Signals** use a compiled A* maze router (`tools/maze_route.py`) rather than Freerouting. On this board Freerouting left 80–100 connections and took 30+ min, because KiCad keep-outs block its vias.
- **Routing layers:** signals run on B.Cu and In2. Signals use F.Cu only inside two windows: the ground area under the MCU and the wire-pad/J1 area. The phase and VM pours on top are never cut.
- **Net classes:**
  - **Default:** 0.15 mm track, 0.15 mm clearance.
  - **Gate drive:** 0.25 mm.
  - Board minimum: 0.09 mm (JLCPCB 4-layer).
- **Router rules:**
  - Escape lanes in front of every 0.5 mm-pitch pin.
  - A turn penalty, so tracks come out as long straight runs.
  - Rip-up-and-reroute for any connection that gets blocked.
  - A close-out loop that ties stray ground islands to the plane.
- **Totals:** 1.1 m of track (B 676 mm, In2 325 mm, F 112 mm) and 103 routed vias, plus 261 power/fan-out vias.

**Layout changes made so it routes cleanly** (all in `tools/build_esc_v2.py`):
- **Half-bridges are now A, B, C from left to right** (V1 had C, B, A). The DRV8320 is on the bottom, so its pinout is mirrored. With this order every gate and phase-sense line runs straight to its FETs.
  - Motor phase order is arbitrary on a BLDC ESC. If the motor spins the wrong way, swap any two motor wires or set reverse in the firmware.
- **Column B is mirrored** (low side on the left). Its gate pins then face the same way as the DRV8320's.
- **High-side gate resistors** sit on the bottom under their FET, outside the phase via field.
- **BEMF 20 kΩ high-side resistors (R20–R22)** sit inside the bottom phase pour under each low-side FET. The switching node never leaves the power stage; only the divided signal runs to the MCU.
- **5 V input pad moved to the right edge**, beside the LDO. The LDO is turned to face it. **Wire the PDB's 5 V to the right-edge pad.**
- **MCU moved 1.5 mm down.** The BEMF low sides and the VDDA filter now form one row along the bottom edge.
- **Parts kept out of the U5–MCU gap:** R11 and R30 moved right of the DRV8320, and the MCU's right-side decoupling caps moved out by 1.1 mm. This leaves room for the six PWM lines.
- **No part sits over an IC on the other side.** The build checks for this and for same-side overlaps.

**Power-stage review round** (after an outside review flagged what DRC can't see):
- **Switching loop.**
  - Added **C23 and C24** (10 µF 50 V X5R 1210, the same part as C12), one per half-bridge.
  - Each sits 1.0 mm (pad to pad) from its high-side drain and its low-side source.
  - Before this, only column B had a local ceramic; A and C closed their hot loop through C11 and the battery pads, 26–27 mm away.
- **Battery pads moved.**
  - **BAT− is on the left edge, BAT+ on the right edge**, level with the FET rows. Low-side return current goes straight from the FETs to BAT− and never crosses the MCU or crystal.
  - **C11 sits beside BAT+.**
  - Each pad has its own via field beside it, never in it.
  - **The SIG / GND / 3V3 wire pads moved to the bottom-left.** The 5 V pad is on the right edge below BAT+.
- **Charge pump.** C14 (VCP) and C16 (flying cap) are within 3 mm of their DRV8320 pins. They were up to 8 mm away.
- **Gate drive.**
  - Gate-resistor pairs are oriented so the gate pads face the FET gate, and the driver pad faces away from the gate link.
  - The low-side pairs stand under their gate via.
  - Every driver-to-gate path is under 30 mm.
- **Phase pours.** No foreign signal enters another phase's pour on any layer. The router enforces this.
- **Vias in pads.** None: the router never puts a via in an SMD pad.
  - Order U5's 0.3 mm thermal vias **plugged / epoxy-filled** at JLCPCB.
- **Already fine:**
  - **Bootstrap caps:** the DRV8320 uses a charge pump, so none are needed.
  - **Power-up:** INHx / INLx / ENABLE have internal pull-downs, and every FET gate has a 10 k pull-down.
  - **Mounting holes:** plain NPTH, on no net.
- **Current sensing** (added): one bus shunt in the BAT− return, which measures all battery current.

  | Ref | Part | LCSC | Role |
  |---|---|---|---|
  | R31 | 0.5 mΩ 1 % 3 W 2512, RLM25FEGMR50M | C710260 | shunt between BAT− (net BATN) and GND |
  | U10 | INA181A3IDBVR, gain 100 | C2652255 | current-sense amplifier |
  | R32, R33 + C25 | 10 Ω + 10 Ω, 100 nF | C22859 / C14663 | differential input filter, ~80 kHz |
  | C26 | 100 nF | C14663 | U10 supply decoupling |
  | R34 + C27 | 1 kΩ + 1 nF | C21190 / C1588 | output filter |

  - **Output:** 50 mV/A into MCU **PA3** (pin 11; ADC1 or the internal OPAMP1). 0 A reads 0 V, 40 A reads 2.0 V, and 3.3 V is about 66 A.
  - **Dissipation:** 0.8 W at 40 A.
  - **Kelvin sensing.** The shunt uses a Kelvin net-tie footprint (`libs/Current Sense.pretty/R_Shunt_2512_Kelvin`). Its sense pads sit at the inner corners of the force pads, so the copper pour's resistance doesn't add error. At 0.5 mΩ that error would be large.
  - **Return path:** 28 GND vias near the shunt carry the return current up to it. BAT− connects to the shunt only, through its own pour.
  - **Checks every run:** the design checks confirm the sense traces leave from the Kelvin pads and count the GND vias at the shunt.

**Every pipeline run checks all of this.**
- `tools/esc_checks.py` measures it on the board.
- Each run renders the layers and pushes to the `esc-v2` branch.
- **Current result (`2409d72`, with current sensing):** DRC 0 / 0 / parity 0; design checks 20 pass, 0 fail, 0 warn, 1 note (via plugging).

**Review:** an autorouted power board should still get a human look before ordering. Check the gate-drive loops (U5 → gate resistor → FET), and that the phase pours on F.Cu look solid in KiCad's 3D view.

---

## 3. Every part: why it's there, does the math work, is there something cheaper (Q3)

Prices are LCSC at 100 pcs, via the jlcsearch API, checked October 2026. "Basic" parts carry no JLCPCB setup fee; each *extended* part adds about $3 per order.

### Power stage

| Ref | Part | Job, and the math | LCSC · price | Verdict |
|---|---|---|---|---|
| U1–U3, U6–U8 | **CSD18532NQ5B** (TI), 60 V | 60 V on a 6S pack (25.2 V max, TVS clamp 45.4 V) gives margin. R_DS(on) is **2.7 mΩ typ at 10 V** (TI datasheet). Conduction loss at 40 A, 50 % duty: 40² × 2.7 mΩ × 0.5 ≈ **2.2 W per FET** (hot: ~3.1 W). Qg is 49 nC, Qgd 7.9 nC. | TI original: check; pin-compatible VBsemi copy C20755622 ≈ $1.08 | **Correct choice**, but it's ~55 % of the BOM ($6.50 for six). Cheaper DFN5×6 60 V parts from domestic makers run about $0.3–0.6. Pick one with R_DS(on) ≤ 3.5 mΩ and Qg ≤ 60 nC, check its datasheet, and bench-test it (opinion). |
| U5 | **DRV8320H** gate driver | Gives 100 % duty (charge pump), VDS overcurrent protection, fixed 100 ns dead time, and 6× PWM (MODE = GND). | C701782 · **$0.99** | **Keep.** The cheaper FD6288Q (~$0.3) needs a separate 10–12 V gate rail, which costs about the same in parts and has no OCP. |
| R2,4,7,12,14,16 | Gate resistor **5.1 Ω (V1) / 4.7 Ω (V2)** | IDRIVE is current-limited to 120 / 240 mA, so the resistor only damps ringing. Dissipation: Qg × V × f = 49 nC × 11 V × 24 kHz ≈ 13 mW → 0603 is fine. | V2: C23164 (basic, 0603) · $0.002 | V2 also shrinks these from 0805 to 0603. |
| R1,5,8,13,15,17 | Gate pull-down **10k** | Holds the FET off while the driver is unpowered. 10 V / 10k = 1 mA while on, which is negligible. | C25804 (basic) | V2: 0805 → 0603. |
| R30 | **75k** VDS level | 0.26 V trip (DRV8320H table, SLVSDJ3D). | C23242 (basic) | New. |
| IDRIVE | left **Hi-Z on purpose** | 120 mA source / 240 mA sink. Qgd / I ≈ **66 ns rise, 33 ns fall**. That's sensible for 24 kHz PWM and keeps EMI down. | — | Documented on the schematic so nobody "fixes" it. |
| C14 | VCP **1 µF** | DRV8320 datasheet: 1 µF between VCP and VM, rated ≥ 16 V. | C15849 (50 V, basic) | ✓ |
| C16 | CPH–CPL **47 nF 50 V** | Datasheet: 47 nF rated for VM. | C1622 (basic) | ✓ |
| C18 | DVDD **1 µF** | Datasheet: 1 µF, 6.3 V. | C15849 | ✓ |
| D2 | **SMBJ28CA** TVS | Stand-off 28 V ≥ 25.2 V; clamp 45.4 V < 60 V FET/driver rating. | C19077583 (preferred) · $0.04 | ✓ |

### Input / bulk (see §4)

| Ref | Part | Math | LCSC | Verdict |
|---|---|---|---|---|
| C11 | 100 µF 50 V aluminium electrolytic (Panasonic FK, size G, 10 mm) | It sees about **28 A RMS** ripple current in simulation (§4). An FK-series 100 µF part is rated **well under 1 A RMS**. | MPN EEE-FK1H101P | **Undersized for ripple.** See §4. |
| C12 | 10 µF 50 V 1210 X5R (Murata GRM32) | X5R at 25 V bias keeps only about 40–50 % of its rating. | C412253 · $0.21 | ✓ Keep, and add more (§4). |
| C13 | 100 nF 50 V | High-frequency decoupling. | C14663 (basic) | ✓ |

### Logic

| Ref | Part | Math | LCSC | Verdict |
|---|---|---|---|---|
| U4 | **STM32G431CBT6** | 170 MHz, comparators and op-amps; AM32 supports it. | C529355 · **$2.49** | The second-biggest cost. AM32 also runs on the **STM32G071** and **AT32F421**, both much cheaper. Switch only if the firmware doesn't need G4 features (opinion). |
| U9 | **AMS1117-3.3** (5 V → 3.3 V) | (5 − 3.3) V × ~50 mA ≈ **85 mW**. Accepts up to 15 V on the input, so it survives PDB 5 V spikes. | C6186 (basic) · $0.16 | ⚠ **Output cap:** the manufacturer's datasheet asks for **10 µF tantalum / 50 µF aluminium, ESR ≤ 0.5 Ω**. C22 is a low-ESR ceramic, and some AMS1117s oscillate with ceramics alone. **Decide:** (A) make C22 a 22 µF tantalum, or (B) use **MCP1703T-3302E/CB** (C53352, 16 V input, ~$0.56), which is designed for ceramic output caps. |
| C21 / C22 | 10 µF 25 V in / 22 µF 25 V out | — | C96446 / C45783 (basic) | See U9. |
| C1–C5, C20, C8 | MCU decoupling: 100 nF per VDD pin, plus 4.7 µF / 10 µF bulk | Follows ST's application note pattern. | basics | ✓ |
| FB1, C6/C7/C9/C10 | VDDA filter: 120 Ω @ 100 MHz bead, 100 nF + 1 µF | ✓ | C14709 (basic) | ✓ |
| R20–R28 | BEMF dividers **20k / 2.2k**, star resistors **10k** | Gain 2.2 / 22.2 = 0.099. **At 25.2 V the ADC pin sees 2.22 V** (with the neutral loading). At the 45.4 V TVS clamp it would see 4.0 V; R20 limits the current into the pin's protection diode to about 0.3 mA, which is safe. | basics | ✓ Simulated on both simulators. |
| R19 / R29 | 220 Ω series + 100k pull-down on SIG | RC with ~10 pF pin capacitance: 220 Ω × 10 pF = 2.2 ns, versus a 625 ns DShot600 bit. The input floats low when unplugged. | basics | ✓ Fixed. |
| R18 / D1 | 1k + red LED | (3.3 − 2.0) / 1k = **1.3 mA**, which is visible. | C21190 / C2286 (basic) | ✓ Fixed (was an unspecified "LED"). |
| R3 / C15 | NRST 10k + 100 nF | Matches ST's reset circuit. | basics | ✓ |
| R10 | BOOT0 10k pull-down | Boots from flash. | basic | ✓ |
| R6 / R9 | SWDIO pull-up / SWCLK pull-down 10k | ✓ | basics | ✓ |
| R11 | nFAULT 10k pull-up | Open-drain output; 0.33 mA. | basic | ✓ |
| Y1, C17/C19 | 8 MHz, C_L = 10 pF; 12 pF caps | 2 × (10 − 4) = 12 pF ✓ | C400090 · $0.12 / C38523 | **Opinion:** AM32 runs on the internal oscillator. If your firmware does too, deleting Y1 + C17 + C19 saves about $0.15, three placements and one extended-part fee. |
| J1 | JST-SH 4-pin SWD | The V1 1 mm through-hole socket isn't stocked. | C160404 · $0.18 | An XY clone (C51940130, $0.03) also exists. |

### Rough BOM (parts only, 100 pcs)

- **Total:** about **$11.5 per board**. FETs ~$6.5, MCU $2.5, driver $1.0, rest ~$1.5.
- **Extended parts:** about 9, roughly $27 per order in JLCPCB setup fees.
- **Biggest levers (opinion):**
  1. Cheaper 60 V FETs: about −$3 to −$4.
  2. STM32G071 / AT32F421: about −$1.5 to −$2.
  3. Drop the crystal: about −$0.15 and one fee.

---

## 4. Smoothing the battery voltage without stepping it down (Q4)

That's the **input capacitor bank**. It doesn't change the DC voltage; it supplies the FET bridge's chopped current so the 6S pack (and its long leads) don't have to.

**What drives the sizing:** the bridge draws full motor current in pulses at the PWM frequency. The capacitors have to source the **AC ripple current**, which is roughly I_phase × √(D(1−D)): about **20 A RMS at 40 A and 50 % duty**. Ripple current rating and ESR matter far more than total µF.

**Simulation setup:**
- 6S pack: 25.2 V source, 15 mΩ.
- 150 nH of battery lead.
- 40 A load chopped at 24 kHz, 50 % duty.
- Run on LTspice **and** ngspice; the deck is in `workspace/esc_v2/.copilot/spice_bank_*`.

| Bank | VM ripple (p-p) | VM minimum | Notes |
|---|---|---|---|
| **Current V1/V2:** 100 µF electrolytic (ESR ~0.3 Ω) + 10 µF MLCC + 100 nF | **6.75 V** | **21.5 V** | The electrolytic's ESR makes it nearly useless at 24 kHz; the single 10 µF MLCC does all the work. |
| 220 µF polymer (15 mΩ) + 4 × 10 µF/50 V MLCC + 100 nF | 1.35 V | 24.2 V | 5× better. |
| **2 × 220 µF polymer + 6 × 10 µF/50 V MLCC** | **0.90 V** | **24.4 V** | ✓ Under 1 V p-p. |

LTspice and ngspice agree within 3 % on all three.

**Recommendation:**
- **On the board:** put **4–6 × 10 µF 50 V X7R 1210 MLCCs** right at the high-side drains. That fits in V2's VM via field area.
- **On the battery leads:** solder **one or two low-ESR 220–470 µF 35–50 V capacitors**, polymer or low-ESR electrolytic, at the board end. That's standard practice on commercial ESCs, and it moves the tallest part off the PCB.
- **For logic:** the 5 V and 3.3 V rails already get their own filtering (FB1 + caps for VDDA).

---

## 5. "A big component that isn't a vertical problem" (Q5)

I've read this two ways; both are covered.

**1. The tall part on the board is C11**, the 10 mm aluminium can, about 10.5 mm high. Low-profile replacements that also fix §4:
- **MLCC bank:** 1210 MLCCs are about 2.5 mm tall. Six of them = 60 µF nominal (~25 µF at 25 V bias), with ESR of a few mΩ each.
- **Polymer caps** (SMD "V-chip" or chip-polymer) are 3–6 mm tall with 10–20 mΩ ESR.
- Or put the big can **on the battery leads**, as above.

**2. A current-sense shunt** (a physically big resistor that's flat). The board has **no current sensing** today. Its only overcurrent protection is the DRV8320's VDS monitor, now set to about 65–96 A. If you want real current limiting or telemetry:
- Use a **2512 (6.3 × 3.2 mm, about 0.6 mm tall) 0.5–1 mΩ shunt** in the low-side GND return.
- At 40 A, 0.5 mΩ dissipates 0.8 W (a 3 W 2512 metal-element part is fine) and gives 20 mV.
- Amplify with the STM32G431's internal op-amps; this is one reason to keep the G4.
- The DRV8323 has three current-sense amplifiers built in, if you'd rather change the driver.

---

## 6. Decisions for you

1. **LDO output cap:** use a tantalum on C22 (A), or switch to the MCP1703 (B). See §3.
2. **Bulk caps:** adopt the §4 bank. This adds about 6 × 1210 on V2 and moves C11 to the leads.
3. **Current sensing:** add the shunt + op-amp (§5), yes or no.
4. **Cost levers:** cheaper FETs, MCU, and/or dropping the crystal.
5. **Crystal frequency:** confirm 8 MHz matches the firmware's HSE setting.
6. **Copper weight:** order V2 with 2 oz outer copper.

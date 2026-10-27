# Design checks: ESC PCB V2.kicad_pcb

**0 fail, 0 warn, 1 info, 20 pass.** Measured on the board by `tools/esc_checks.py`. DRC/ERC are reported separately.

| Status | Check | Finding |
|---|---|---|
| INFO | via in pad | exposed-pad thermal vias in U5: 0.3 mm drills, specify 'via plugging / epoxy filled' at JLCPCB or accept slight solder wicking |
| PASS | 3D models | every part with a body has a 3D model that loads |
| PASS | charge pump | C14 on Net-(U5-VCP): 2.7 mm from U5 pin (limit 3.0 mm) |
| PASS | charge pump | C16 on Net-(U5-CPH): 2.7 mm from U5 pin (limit 3.0 mm) |
| PASS | charge pump | C16 on Net-(U5-CPL): 2.4 mm from U5 pin (limit 3.0 mm) |
| PASS | charge pump | C18 on Net-(U5-DVDD): 2.6 mm from U5 pin (limit 3.0 mm) |
| PASS | current sense | R31: Kelvin sense routed from its own sense pads 2/3 |
| PASS | current sense | 30 GND vias within 8 mm of R31's GND pad carry the return current to it (ok) |
| PASS | gate drive | Net-(U5-GHA) -> R2 -> Net-(U1-G): 27.2 mm of track, 1 vias |
| PASS | gate drive | Net-(U5-GLA) -> R4 -> Net-(U2-G): 15.4 mm of track, 3 vias |
| PASS | gate drive | GLB -> R12 -> Net-(U6-G): 10.9 mm of track, 1 vias |
| PASS | gate drive | GHB -> R7 -> Net-(U3-G): 19.0 mm of track, 1 vias |
| PASS | gate drive | GHC -> R14 -> Net-(U7-G): 23.2 mm of track, 3 vias |
| PASS | gate drive | GLC -> R16 -> Net-(U8-G): 26.5 mm of track, 1 vias |
| PASS | ground return | no low-side return path to Bat-1 passes within 4.0 mm of the crystal or MCU |
| PASS | mounting holes | mounting holes are plain NPTH, not on any net |
| PASS | phase crossings | no foreign signal runs inside a phase pour on any layer |
| PASS | switching loop | /Phase_A: C23 sits 1.0 mm from U1's drain and 1.0 mm from U2's source (pad to pad) |
| PASS | switching loop | /Phase_B: C12 sits 1.0 mm from U3's drain and 1.1 mm from U6's source (pad to pad) |
| PASS | switching loop | /Phase_C: C24 sits 1.0 mm from U7's drain and 1.0 mm from U8's source (pad to pad) |
| PASS | via in pad | no routed or stitching via sits inside an SMD pad |

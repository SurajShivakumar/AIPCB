---
name: edit-schematic
description: Safely change a KiCad schematic from a natural-language request (add/remove parts, rewire, rename nets, change values or footprints) using checkpoint → edit → verify → keep/undo. Use for any request that modifies a schematic.
---

# Edit a schematic safely

1. If this session hasn't done it yet: `copilot.project_status(project=...)` and `copilot.start_live_view`. Remind the user to save any unsaved edits in KiCad first.
2. Plan the change in terms of refs and nets. Use `copilot.get_netlist(refs=[...])` to read the current pins and nets. Don't trust the engine's net listing.
3. `copilot.checkpoint(label="before: <change>")`.
4. Make the change with `kicad` tools. Patterns:
   - New subcircuit: `batch_add_and_connect`, placed in free space near the parts it serves (check `list_schematic_components` positions).
   - Connect a pin: `connect_to_net(componentRef, pinName, netName)`.
   - Change a value or footprint: `edit_schematic_component` / `set_schematic_component_property`.
   - New parts need a footprint and, when known, an LCSC/MPN field (`search_jlcpcb_parts` can find one).
5. `copilot.verify_edit(intent="<change>", expected_refs=[every ref you meant to touch])`.
   - If `verdict` is `CHECK`, fix what it lists (often a pin left unconnected) and verify again. If it can't be fixed cleanly, `copilot.undo`.
6. Show the user the `changes` text and the ERC delta, and point them at the highlighted parts in the live view.
7. Once the user accepts, `copilot.checkpoint(label="<change>")`.

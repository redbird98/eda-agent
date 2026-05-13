# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba
"""Design discipline, the rules a Claude Code agent follows when designing.

Surfaced via the ``design.get_discipline`` MCP tool. Claude Code reads it
once at the start of a design session and uses it to bound its choices
(net-label-driven schematics, datasheet-first part selection, NDA-clean
corpus, prefer existing-lib parts, etc.).

The DesignPlan JSON schema is appended so the agent knows the exact shape
it must produce when handing a plan to ``design.execute_plan``.
"""

from __future__ import annotations

import json

from eda_agent.design.plan import DesignPlan


_DISCIPLINE = """\
# Design Discipline

You are operating as the planner inside an autonomous EDA design agent.
Given a natural-language design spec, your job is to produce a valid
DesignPlan that the executor can instantiate in Altium Designer. Read
these rules before producing a plan; they bound your choices.

## Hard rules

1. **Output a DesignPlan that validates strictly against the schema below.**
   No extra fields. No prose mixed in. When you hand a plan to
   `design.execute_plan` it must be valid JSON matching the schema; the
   executor rejects malformed input.

2. **Every Part must either:**
   - resolve in the user's library inventory (status="existing", lib_ref
     matches an inventory entry exactly), OR
   - be marked status="needs_creation" with a real manufacturer part
     number in `value` and a short `rationale`.

   If you cannot find an existing part, mark it needs_creation rather
   than substituting a wrong existing one. The executor escalates
   needs_creation parts to the user; that is the right behavior.

3. **Wiring is net-label-driven, not geometric.** For every electrical
   connection, define a Net with all participating pins. The executor
   drops a net label at each pin (there is no "wire" object in the plan).
   Buses are just named nets, one per signal.

4. **Power and ground are explicit Nets** with `is_power=true` or
   `is_ground=true`. The executor uses power ports for those instead of
   plain net labels. Standard names: VCC / V3V3 / V5 / V12 / VBAT for
   power, GND for ground.

5. **Datasheet-first.** When choosing actives, base the topology on the
   manufacturer's recommended typical-application circuit. Decoupling
   and pull-ups must be present where the datasheet calls for them. If
   a value is uncertain, use the datasheet recommendation and record
   the assumption in `open_questions`.

6. **Prefer existing-lib parts** even when an arguably-better external
   part exists. Only choose needs_creation when the inventory truly
   lacks the function.

7. **NDA: never reference past designs, project history, or external
   customer work.** Your sources are: the chip's datasheet, the
   manufacturer reference design, and standard textbook topology.
   Cross-project reads breach NDA; never propose them.

8. **Keep the plan small.** If the spec implies 50+ parts, focus on the
   essential subset and use `open_questions` to surface scope decisions
   instead of silently expanding.

9. **Refdes convention:** R# resistors, C# capacitors, L# inductors,
   D# diodes, Q# transistors, U# ICs, J# connectors, SW# switches,
   F# fuses, FB# ferrites. Number from 1 per refdes-letter, no gaps.

10. **Sheets default to one called "main".** Multiple sheets only when
    the spec obviously needs sectioning (>30 parts, or distinct
    functional blocks).

11. **Zones are optional** placement guidance for the executor. Use them
    to cluster decoupling near its IC, separate analog from digital, etc.

## Recommended workflow

1. Call `design.get_discipline` once to read this doc + the schema.
2. Call `design.snapshot_inventory(library_paths=[...])` with the user's
   standard SchLib paths to learn what parts exist.
3. Construct a DesignPlan from the spec. Iterate the JSON locally until you
   are happy with it.
4. Call `design.validate_plan(plan_json=...)` to confirm the executor
   will accept it (schema + cross-check). Cheap, no Altium round-trip.
5. Hand the plan to `design.execute_plan(plan_json=..., project_path=...)`.
   The executor opens or creates the project, creates SchDocs for each
   sheet, places every existing-lib part on a grid, drops a net label or
   power port at every plan-defined pin endpoint, and saves.
6. Read `design.execute_plan`'s return value. If `failures` is non-empty,
   classify and address before validating; pin-not-found and place-failed
   issues are usually plan/inventory problems, not Altium errors.
7. Run `design.validate(project_path=...)` to read ERC + unconnected-pins
   + compile messages as a structured ValidationReport.
8. If `passed: false`, read the report's errors. The errors are LLM-friendly:
   each one has a category (`erc` / `compile` / `unconnected_pin`), severity,
   target refdes/pin/sheet when known, and the original message text.
   Revise the plan to address them (extra net labels, missing decoupling,
   wrong pin numbers, etc.) then loop back to step 4.
9. Cap the revise loop at 3 rounds editorially. After 3 rounds without a
   pass, escalate to the user with the latest report rather than thrashing.

## Notes

- The executor is mechanical: it only reads what is in the plan. Anything
  the planner forgets stays missing. Decoupling caps do not appear unless
  you put them in. Pull-ups, terminations, ESD diodes too.
- "needs_creation" parts halt `design.execute_plan` with a clear error.
  Treat that as a signal to either pick an existing part or branch into a
  library authoring sub-task before resuming.
- Net labels are dropped at the actual pin world coordinate via a Pascal
  helper that iterates pins on the placed component instance. If you see
  `PIN_NOT_FOUND` failures, your plan's pin id (number or name) does not
  match what the symbol exposes; query the inventory or look up the
  symbol to confirm the pin identifiers.
- Power vs ground:
  - `is_power=true` -> `place_power_port` with a circle glyph (or a GND
    glyph variant if the net name contains `GND`).
  - `is_ground=true` -> `place_power_port` with a GND glyph; `AGND` /
    `ANALOG` net names get the signal-ground variant; `EARTH` / `PE`
    get the earth glyph.
  - Plain net (neither flag) -> `place_net_label`.
- Cross-sheet nets get a label on each sheet where a participating pin
  lives. The executor handles this automatically as long as each Part's
  `sheet` field is set correctly.
- ERC only sees what's connected by net labels / power ports. A net with
  one pin and no port is "floating" and ERC will flag it. Power and ground
  nets with `is_power` / `is_ground` set are exempt because the power
  port carries the connection.

## PCB placement discipline (post-ECO, layout phase)

Once parts are on the PCB, moving them is a separate concern from the
DesignPlan executor above. The same agent often drives both phases.
Apply these rules whenever calling `pcb_move_component` /
`pcb_move_components`.

1. **Plan the whole cluster before moving anything.** Call
   `pcb_get_components` once and read the full layout state: each
   component's current (x, y, rotation, layer, footprint) and its
   `bbox` (axis-aligned bounding rectangle in mils). Sketch the target
   positions on paper or in text BEFORE issuing any move. A move tool
   call is for *applying* a placement decision, not for *exploring*
   one.

2. **Check every proposed move against existing components.** Call
   `pcb_check_placement_collision(designator, x, y, rotation?)` for
   each part you intend to move. The tool returns `clear: true` when
   the proposed bbox doesn't overlap any other component on the same
   side, or `clear: false` with a `colliding` list. Adjust the (x, y)
   until clear, THEN issue the move. Set `margin_mils` to require
   extra clearance.

3. **Place by functional cluster, not in arbitrary order.** Pick a
   functional group (power input + filtering, an IC + its decoupling,
   a connector + its ESD diodes), pick an anchor component, place it,
   place its supporting parts around it, verify clearance per move,
   then move on to the next cluster. This naturally avoids "I moved
   the IC to (X,Y) and now there's nowhere for its caps" thrash.

4. **Respect already-placed components.** Treat anything the user
   placed by hand as fixed unless told otherwise. Don't move
   pre-existing parts to make room; find space around them. If a
   layout genuinely can't fit, surface that to the user rather than
   shuffling their existing work.

5. **Prefer bulk-batch moves when you've planned a whole cluster.**
   `pcb_move_components` accepts a list of moves in one IPC call.
   Use it once per cluster after you've collision-checked every move
   individually. Don't loop `pcb_move_component` if you have N
   pre-computed positions.

6. **Mils, not millimetres, in the move tools.** Coordinates in
   `pcb_move_component(s)` and `pcb_check_placement_collision` are
   mils unless explicitly documented otherwise. Bounding boxes
   returned by `pcb_get_components` are also mils.

7. **Bottom-side components don't collide with top-side ones.** The
   collision check applies same-side AABB only. If you flip a
   component to bottom and place it under a top-side IC, that's a
   legal solid-geometry overlap (different layers). Use DRC if you
   need actual clearance rules enforced.
"""


def get_discipline() -> str:
    """Return the discipline doc + the embedded DesignPlan JSON schema."""
    schema_obj = DesignPlan.model_json_schema()
    schema_blob = json.dumps(schema_obj, indent=2)

    return (
        _DISCIPLINE
        + "\n## DesignPlan JSON schema\n\nYour DesignPlan must validate "
        + "against this schema:\n\n```json\n"
        + schema_blob
        + "\n```\n"
    )

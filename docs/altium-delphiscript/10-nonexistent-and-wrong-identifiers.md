# 10. Non-existent & Wrong Identifiers

A reference catalogue of identifiers that resemble valid binding members but do
not exist in the DelphiScript binding, paired with the correct name. These fail
as compile-time "Undeclared identifier" errors, which are uncatchable. The
exception is the wrong process strings, which fail silently because `RunProcess`
accepts any string and no-ops on a typo.

---

## 10.1 Enum constants that don't exist

| Wrong | Correct |
|-------|---------|
| `eElectricBiDir` | `eElectricIO` (bidirectional pin electrical type) |
| `eOffSheetConnector` | `eCrossSheetConnector` |
| `eSchDoc`, `eSchDocument` | No such ObjectId — Nil-guard the document, or test `eSchLib` for the library case |
| `ePcbDoc` | No such constant — verify a board with `PCBServer.GetCurrentPCBBoard <> Nil` |
| `ePolygonPourOver_Same` | `ePolygonPourOver_SameNet` (or `_None` / `_SameNetPolygon`) |
| `eHatchStyleNone` | `ePolySolid` or `ePolyNoHatch` |
| `eHatchStyle45Degree` | `ePolyHatch45` |
| `eHatchStyle90Degree` | `ePolyHatch90` |
| `eHatchStyleHorizontal` / `eHatchStyleVertical` | No such value — `TPolyHatchStyle` is only Solid / NoHatch / Hatch45 / Hatch90 |

---

## 10.2 Methods / functions that don't exist (or are misspelled)

| Wrong | Correct |
|-------|---------|
| `GetState_VertexCount` | `GetState_VerticesCount` (plural) |
| `Client.GetDocumentCount`, `Client.GetDocument(I)` | No numeric enumerator — walk `…DM_LogicalDocuments(j)` + `Client.GetDocumentByPath` |
| `Client.ProcessMessages` | `Application.ProcessMessages` |
| `SchLib.ComponentCount`, `SchLib.SetState_CurrentComponentIndex` | Enumerate via `CreateLibCompInfoReader` ([7.3](07-schematic-api.md#73-library-symbols-use-compinforeader-not-schiterator)) |
| `SchLib.LibraryIterator_Create` | `SchLibIterator_Create` (but prefer `CompInfoReader`) |
| `PCBServer.GetPCBBoardByPath` | Undeclared — open+show the doc, then `GetCurrentPCBBoard` ([8.1](08-pcb-api.md#81-resolving-the-board--getcurrentpcbboard-is-focus-dependent)) |
| `ISch_Document.ClearAllSelection` / `ClearSelection` | Run process `Sch:DeSelect` (Scope=All). (`IPCB_Board.ClearSelection` *is* real) |
| `IPCB_Polygon.GeometricPolygon` | Undeclared here — use `.AreaSize` + `.BoundingRectangle` |
| `CoordToMms` | `CoordToMM` (no trailing `s`) |
| `GetAsyncKeyState` and other Win32 calls | Not exposed by the sandbox |

The `RobotManager.SendMessage` broadcast pattern without an object address does
not compile; use `ProcessControl.PreProcess/PostProcess` plus
`RobotManager.SendMessage(addr, c_BroadCast, SCHM_*, c_NoEventData)`
([7.4](07-schematic-api.md#74-modifying-objects-the-processcontrol-transaction)).
The constants `CYCSCHM_BeginModify`, `CYCSCHM_EndModify` do not all resolve.

---

## 10.3 Properties that are wrong on a given type

| Wrong | Correct |
|-------|---------|
| `Pad.XSize`, `Pad.YSize` | `Pad.TopXSize` / `Pad.TopYSize` (or the per-layer Mid/Bot variants) |
| `IPCB_Polygon.HatchStyle` | `IPCB_Polygon.PolyHatchStyle` |
| `ISch_Document.UseMetricUnit` | `ISch_Document.UnitSystem` (`TUnitSystem`, e.g. `eMetric`) |
| `Obj.NetName` on `ISch_GraphicalObject` | Net name is not a base-primitive property — `.Text` on `eNetLabel`/`ePowerObject`/`ePort`, `.Name` on `eSheetEntry`; pin-level via `Pin.DM_FlattenedNetName` |
| `Obj.Corner` on `ISch_GraphicalObject` | Only on `ISch_Rectangle`/`ISch_Line` — narrow first |
| `Pin.DM_PinDesignator` | `Pin.DM_PinNumber` |
| `Comp.Name` read as a string (PCB) | `Comp.Name.Text` (`Name` is an `IPCB_Text`) |
| `Doc.DM_NetCount` | No such property — derive nets from pins |
| `BoardOutline.xv[I]` / `yv[I]` | `BoardOutline.Segments[I].vx` / `.vy` |
| Writing `IPCB_Rule.Priority` | Read-only function — reorder in the UI |
| Writing `ISch_Component.CurrentFootprintModelName` | Read-only — edit `Comp.Implementations` → `ISch_Implementation.ModelName` |
| Writing `ISch_Probe.Text` / `.NetName` | Not settable — the probe auto-adopts its net |

---

## 10.4 `RunProcess` server-process strings (silent no-op on typo)

`RunProcess` accepts any string and does nothing on an unrecognised one — no
error, no exception. Verify process names against the Server Process Reference
(TR0124).

| Wrong | Correct |
|-------|---------|
| `PCB:RunDRC` | `PCB:DesignRuleCheck` |
| `PCB:UpdatePCBFromProject` | Not a real process — use `WorkspaceManager:Compare` (ObjectKind=Project, Action=UpdateOther) ([8.8](08-pcb-api.md#88-eco-schematic--pcb-update-is-not-reliably-scriptable)) |

When a process call appears to do nothing, suspect the process name before the
parameters.

---

## 10.5 How to extend this list safely

Each entry was added after a script failed at runtime with "Undeclared
identifier: X" or a silent no-op. On encountering a new case:

1. Record the exact wrong identifier and the working replacement.
2. Add it to the table above it belongs in.
3. If a static linter covers the scripts, add the wrong name to its deny-list so
   the next occurrence is caught before a recompile cycle. This class of bug is
   invisible at edit time and surfaces only after the reopen-and-run loop, so a
   pre-run grep catches it earlier.

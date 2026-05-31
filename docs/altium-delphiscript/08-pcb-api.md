# 8. PCB API (`PCBServer` / `IPCB_*`)

Reached through the global `PCBServer`. Narrowing ([chapter 6](06-interfaces-and-type-narrowing.md))
applies to every `IPCB_Primitive` returned from an iterator.

---

## 8.1 Resolving the board — `GetCurrentPCBBoard` is focus-dependent

`PCBServer.GetCurrentPCBBoard` returns `Nil` whenever the focused tab is **not**
the PCB — i.e. the user is viewing a schematic, even though the board is loaded.

`PCBServer.GetPCBBoardByPath` is **undeclared** on current builds, so the board
cannot be path-targeted directly. The working fallback: walk the focused
project's logical documents, find the `.PcbDoc`, open+show it to obtain the
board pointer, then restore the prior view.

```pascal
Function GetBoardAnywhere : IPCB_Board;
Var Prj : IProject; Doc : IDocument; SrvDoc : IServerDocument; I : Integer;
Begin
    Result := PCBServer.GetCurrentPCBBoard;        // fast path
    If Result <> Nil Then Exit;

    Prj := GetWorkspace.DM_FocusedProject;
    If Prj = Nil Then Exit;
    For I := 0 To Prj.DM_LogicalDocumentCount - 1 Do
    Begin
        Doc := Prj.DM_LogicalDocuments(I);
        If Doc.DM_DocumentKind = 'PCB' Then
        Begin
            SrvDoc := Client.OpenDocument('PCB', Doc.DM_FullPath);
            Client.ShowDocument(SrvDoc);
            Result := PCBServer.GetCurrentPCBBoard;
            Exit;
        End;
    End;
End;
```

Wrap this once and call it from every PCB handler. When several `.PcbDoc`s are
open and a *specific* one is required, resolve by explicit path (open+show that
document); fall back to the focus-independent lookup when no path is given.

---

## 8.2 Board iteration — the full filter triple is mandatory

A `BoardIterator` requires **all three** filters: object-set, layer-set
(`AllLayers`), and method (`eProcessAll`). Destroy it in `Finally`.

```pascal
Iter := Board.BoardIterator_Create;
Try
    Iter.AddFilter_ObjectSet(MkSet(eTrackObject, eViaObject, ePadObject));
    Iter.AddFilter_LayerSet(AllLayers);
    Iter.AddFilter_Method(eProcessAll);
    Obj := Iter.FirstPCBObject;
    While Obj <> Nil Do
    Begin
        ...                                        // narrow per ObjectId
        Obj := Iter.NextPCBObject;
    End;
Finally
    Board.BoardIterator_Destroy(Iter);
End;
```

### Never filter a single `eConnectionObject` type

A board iterator filtered to only `eConnectionObject` triggers a full
ratsnest rebuild and hangs. Pass a multi-type object set and branch on
`ObjectId` inside the loop:

```pascal
Iter.AddFilter_ObjectSet(MkSet(eTrackObject, eViaObject, ePadObject,
    eComponentObject, eFillObject, eTextObject, ePolyObject, eConnectionObject));
...
If Obj.ObjectId = eConnectionObject Then ...
```

### Spatial (area-bounded) iteration

```pascal
SIter := Board.SpatialIterator_Create;
Try
    SIter.AddFilter_ObjectSet(MkSet(eTrackObject, eArcObject, ePadObject, eViaObject));
    SIter.AddFilter_LayerSet(MkSet(Layer, eMultiLayer));
    SIter.AddFilter_Area(X1, Y1, X2, Y2);          // internal coords
    Hit := SIter.FirstPCBObject;
    While Hit <> Nil Do Hit := SIter.NextPCBObject;
Finally
    Board.SpatialIterator_Destroy(SIter);
End;
```

### Pads of one component — `GroupIterator`

```pascal
GIter := Comp.GroupIterator_Create;
Try
    GIter.AddFilter_ObjectSet(MkSet(ePadObject));
    Pad := GIter.FirstPCBObject;
    While Pad <> Nil Do Pad := GIter.NextPCBObject;
Finally
    Comp.GroupIterator_Destroy(GIter);
End;
```

---

## 8.3 Modifying / deleting — `PreProcess`/`PostProcess` (no document arg)

The PCB transaction wrappers take **no** arguments (unlike the schematic ones):

```pascal
PCBServer.PreProcess;
Try
    ...                                            // mutate primitives
Finally
    PCBServer.PostProcess;
End;
```

Removal notifies robots, then removes:

```pascal
PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
    PCBM_BoardRegisteration, Obj.I_ObjectAddress);
Board.RemovePCBObject(Obj);
```

---

## 8.4 Property traps

- **A component's `Name` is an `IPCB_Text`, not a string.** Reading
  `Comp.Name` where a string is expected raises the Dispatch→OleStr modal. Use
  `Comp.Name.Text` (and `Comp.Comment.Text`).
- **Pads have per-layer sizes**, not a bare `XSize`/`YSize`:
  `TopXSize`/`TopYSize` (top), `MidXSize`/… (inner), `BotXSize`/… (bottom).
- **`IPCB_Rule.Priority` is read-only** — assigning it crashes the engine;
  reorder rules in the UI.
- **Constraint-value writes** (`MinWidth`, `Gap`, hole limits) must use the
  iterator-return narrowing, not a cast through `IPCB_Rule`
  ([6.3](06-interfaces-and-type-narrowing.md#63-narrowing-for-reads-works-locally-constraint-property-writes-need-iterator-return)).
- **Track endpoints** `X1/Y1/X2/Y2`, **arc** `XCenter/YCenter/Radius/StartAngle/EndAngle`
  and `StartX/StartY/EndX/EndY`, **pad** `HoleSize/TopXSize/…`, **via** `Size/HoleSize`,
  **component** `Pattern/SourceDesignator` — all subtype-only; narrow first.

---

## 8.5 Polygons and regions

- `IPCB_Polygon` has **no `SetOutlineContour`** — that is region-only. Build a
  polygon outline through its **Segments** API.
- A local `TPolySegment` record must be **instantiated** before its fields are
  written, or `Seg.Kind := …` raises `Undeclared identifier: Kind`:
  ```pascal
  Var Seg : TPolySegment;
  Begin
      Seg := TPolySegment;        // instantiate the record first
      Seg.Kind := ePolySegmentLine;
      Seg.vx := X; Seg.vy := Y;
  End;
  ```
- The polygon hatch-style property is **`PolyHatchStyle`** (not `HatchStyle`);
  valid values are `ePolySolid / ePolyNoHatch / ePolyHatch45 / ePolyHatch90`.
  The `ePolygonPourOver_*` pour mode is `_None / _SameNet / _SameNetPolygon`
  (there is no `_Same`).
- `IPCB_Polygon.GeometricPolygon` is undeclared in the script binding despite
  appearing in the API docs — use `.AreaSize` + `.BoundingRectangle` instead of
  `GeometricPolygon.GetState_Area`.

---

## 8.6 The board outline is read via segments

`Board.BoardOutline.GetState_PointCount` and per-segment access via
`BoardOutline.Segments[I].vx` / `.vy` give the outline geometry. There is no
`BoardOutline.xv[I]` / `yv[I]` accessor.

---

## 8.7 Coordinates & layers

Internal unit = 1/10000 mil = 10⁻⁷ mm. Convert at the boundary:

```pascal
Function MilsToCoord(Mils : Integer) : TCoord; Begin Result := Mils * 10000; End;
Function CoordToMils(C : TCoord) : Integer;    Begin Result := Round(C / 10000); End;
Function MMToCoord(MM : Double) : TCoord;       Begin Result := Round(MM * 10000000 / 25.4); End;
Function CoordToMM(C : TCoord) : Double;         Begin Result := C * 25.4 / 10000000; End;
```

Layer string↔enum mapping: a **string** subject `Case` is valid for
name→`eXxx`, but the inverse (enum→name) must be an `If/Else If` chain — `Case`
cannot switch on `eXxx` identifiers ([chapter 1](01-language-and-parser.md#12-case-works-on-strings-and-integer-literals-only--never-on-enums)).

---

## 8.8 ECO (schematic → PCB update) is not reliably scriptable

There is no silent, scriptable "Update PCB Document". The launcher is the
process `WorkspaceManager:Compare` with `ObjectKind=Project` and
`Action=UpdateOther` (after `WorkspaceManager:Compile`) — but it raises the
**non-suppressible** Engineering Change Order dialog that blocks until a human
dismisses it. (`PCB:UpdatePCBFromProject` is not a real process id and
silently no-ops.)

Placing a footprint directly onto the board (`IPCB_Component.LoadFromLibrary` +
`AddPCBObject`) puts geometry down but leaves the project **unsynced** (no
sch↔pcb link, no nets) unless `SourceUniqueId` / `SourceDesignator` are also
stamped and nets created/assigned manually — it is not an ECO substitute on its
own.

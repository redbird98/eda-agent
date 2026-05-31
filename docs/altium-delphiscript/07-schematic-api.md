# 7. Schematic API (`SchServer` / `ISch_*`)

The schematic object model is reached through the global `SchServer`. This
chapter covers the working access patterns and the property-level constraints.
[Chapter 6](06-interfaces-and-type-narrowing.md) covers narrowing, which applies
throughout.

---

## 7.1 Getting the current document

```pascal
Var SchDoc : ISch_Document;
Begin
    SchDoc := SchServer.GetCurrentSchDocument;     // SchDoc or SchLib, or Nil
    If SchDoc = Nil Then Exit;
End;
```

`GetCurrentSchDocument` returns either a schematic sheet **or** a schematic
library, or `Nil`. There is no `eSchDoc` / `eSchDocument` ObjectId constant to
distinguish them. A `Nil` guard is usually sufficient; the library case can be
tested specifically (see 7.6).

---

## 7.2 Iterating placed objects on a sheet

Create the iterator from the document, filter by object set, walk
`FirstSchObject`/`NextSchObject`, and destroy the iterator in `Finally`.

```pascal
Var Iter : ISch_Iterator; Obj : ISch_GraphicalObject;
Begin
    Iter := SchDoc.SchIterator_Create;
    Try
        Iter.AddFilter_ObjectSet(MkSet(eSchComponent));
        Obj := Iter.FirstSchObject;
        While Obj <> Nil Do
        Begin
            ...                                   // narrow per ObjectId as needed
            Obj := Iter.NextSchObject;
        End;
    Finally
        SchDoc.SchIterator_Destroy(Iter);
    End;
End;
```

`MkSet(...)` is the DelphiScript set constructor for the `eXxx` ordinals.

### Nested iteration — pins and parameters of a component

A component exposes its own `SchIterator_Create` to walk its children:

```pascal
PIter := Component.SchIterator_Create;
Try
    PIter.AddFilter_ObjectSet(MkSet(ePin));       // or eParameter
    Pin := PIter.FirstSchObject;
    While Pin <> Nil Do
    Begin
        ...
        Pin := PIter.NextSchObject;
    End;
Finally
    Component.SchIterator_Destroy(PIter);
End;
```

### Deleting during iteration

Removing an object invalidates a live iterator. Delete by re-creating the
iterator, finding one match, destroying the iterator, removing the object,
and looping with a max-iterations guard.

---

## 7.3 Library symbols use `CompInfoReader`, not `SchIterator`

A schematic **library** (`.SchLib`) does not enumerate its symbol entries with
`SchIterator` — that walks *placed* objects on a sheet. Use the dedicated
metadata reader:

```pascal
Var Reader : ISch_LibCompInfoReader; I, N : Integer; Info : ISch_ComponentInfo;
Begin
    Reader := SchServer.CreateLibCompInfoReader(LibFullPath);
    Reader.ReadAllComponentInfo;
    N := Reader.NumComponentInfos;
    For I := 0 To N - 1 Do
    Begin
        Info := Reader.ComponentInfos[I];
        Name := Info.CompName;          // also .AliasName, .PartCount, .Description
    End;
    SchServer.DestroyCompInfoReader(Reader);
End;
```

To obtain the live symbol (for parameters/pins), fetch by lib-ref:

```pascal
Component := SchLib.GetState_SchComponentByLibRef(Name);   // read-only fetch
```

`GetState_SchComponentByLibRef` does not change the editor's active component.
To switch the current symbol, set
`SchLib.CurrentSchComponent := Component`.

> There is no `SchLib.ComponentCount`, `SchLib.SetState_CurrentComponentIndex`,
> or `SchLib.LibraryIterator_Create`. The iterator factory is
> `SchLibIterator_Create`, but for enumeration use the `CompInfoReader` above.

---

## 7.4 Modifying objects: the `ProcessControl` transaction

Wrap edits in a server transaction so the UI re-renders and the document is
marked dirty. The second argument to `PreProcess`/`PostProcess` **must be a
string** (often `''`), never an object.

```pascal
SchServer.ProcessControl.PreProcess(SchDoc, '');
Try
    Obj.SetState_...;                 // your edits, optionally per-object bracketed
Finally
    SchServer.ProcessControl.PostProcess(SchDoc, '');
End;
```

Bracket each object's change with begin/end-modify notifications so the editor
picks it up:

```pascal
SchServer.RobotManager.SendMessage(Obj.I_ObjectAddress, c_BroadCast,
    SCHM_BeginModify, c_NoEventData);
...                                   // mutate Obj
SchServer.RobotManager.SendMessage(Obj.I_ObjectAddress, c_BroadCast,
    SCHM_EndModify, c_NoEventData);
```

Without the begin/end-modify bracket, a property change can update in memory but
never repaint, and a subsequent save may skip the document.

> Use the `ProcessControl.PreProcess/PostProcess` + `RobotManager.SendMessage`
> pattern. The `CYCSCHM_BeginModify` / `CYCSCHM_EndModify` /
> `c_BroadCast`+`c_NoEventData` constants referenced in some older examples do
> not all resolve; the `RobotManager.SendMessage(addr, c_BroadCast, SCHM_*, c_NoEventData)`
> form with the integer `SCHM_BeginModify`/`SCHM_EndModify` message IDs is the
> reliable one. A bare `RobotManager.SendMessage` broadcast without the
> object address does not compile.

---

## 7.5 Pin geometry — `Pin.Location` is the electrical end

For a **placed** component, `ISch_Pin.Location` returns the **electrical
terminal** — the point where a wire connects — not the body-side root of the
pin. Do not add the pin length to `Location` to find the stub start: the stub
runs from `Location` outward by the stub length only.

```pascal
// Location is already the wire-connection end:
WireEnd := Pin.Location;
// A stub starts AT the electrical end and extends outward; do not offset by PinLength.
```

Other pin notes:
- Designator/name via the on-canvas object are `Pin.Designator` / `Pin.Name`;
  the flattened net is read on the DM side
  (`Pin.DM_FlattenedNetName`, [chapter 9](09-document-model-api.md)).
- A pin's font/colour are not exposed — styling helpers belong on
  `ISch_Label`, not on `ISch_Pin`.

---

## 7.6 Parameter buckets: `DM_Parameters` vs the on-canvas `eParameter` iterator

A schematic component has **two distinct parameter collections** that do not
sync automatically:

- **Title-block / special-string** parameters are read from the document model:
  `IDocument.DM_Parameters`.
- **On-canvas** parameters are a separate bucket, walked with a
  `SchIterator` filtered to `eParameter` on the component.

Writing one does not update the other. Determine which bucket a given parameter
lives in and read/write that one consistently.

---

## 7.7 Programmatic component placement

To place a symbol from a library, do not use `SchDoc.PlaceSchComponent`
(it raises modal pickers and leaves position state inconsistent). Use the
load-add-move-orient sequence:

```pascal
Component := SchServer.LoadComponentFromLibrary(LibRef, LibPath, SchDoc);
SchDoc.AddSchObject(Component);
Component.MoveToXY(MilsToCoord(X), MilsToCoord(Y));
Component.SetState_Orientation(eRotate0);    // or eRotate90/180/270
```

Wrap the placement in the `ProcessControl` transaction (7.4) so the sheet
repaints and is marked dirty.

---

## 7.8 Drawing wires / buses / polylines: insert each vertex explicitly

A 2-vertex wire (or bus, or polygon outline) is not created by setting
`Location` plus a single vertex — that yields an invisible zero-vertex object.
`InsertVertex` and `SetState_Vertex` must be called for each vertex in turn:

```pascal
Wire := SchServer.SchObjectFactory(eWire, eCreate_Default);
Wire.InsertVertex := 1;  Wire.SetState_Vertex(1, StartPoint);
Wire.InsertVertex := 2;  Wire.SetState_Vertex(2, EndPoint);
SchDoc.AddSchObject(Wire);
```

For multi-segment shapes, the vertex-count getter is plural:
`GetState_VerticesCount` (not `GetState_VertexCount`); read each with
`GetState_Vertex(I)`.

---

## 7.9 Enum vocabulary

- **Bidirectional pin electrical type** is `eElectricIO`; there is no
  `eElectricBiDir`.
- The **cross-sheet connector** is `eCrossSheetConnector`; there is no
  `eOffSheetConnector`.
- `ISch_Document` exposes **`UnitSystem`** (a `TUnitSystem`, e.g. `eMetric`);
  there is no `UseMetricUnit` property.
- To clear a schematic selection, run the process `Sch:DeSelect` (Scope=All).
  `ISch_Document.ClearAllSelection` / `ClearSelection` are undeclared on the
  schematic side (the PCB sibling `IPCB_Board.ClearSelection` is real).

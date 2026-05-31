# 6. Interfaces & Type Narrowing

Altium's SCH and PCB object models are interface hierarchies with a common base
(`IPCB_Primitive` for PCB, `ISch_GraphicalObject` for schematic) and many
specialised descendants (`IPCB_Track`, `IPCB_Arc`, `IPCB_Pad`, `ISch_Rectangle`,
`ISch_Line`, …). This chapter documents the DelphiScript-specific behaviour of
moving between base and descendant interfaces.

---

## 6.1 A subtype-only member read off a base interface is a *compile* error

The compiler resolves members against the **declared** type of the variable.
`IPCB_Primitive` does not declare `X1` (that lives on `IPCB_Track`), so reading
`Obj.X1` when `Obj : IPCB_Primitive` is `Undeclared identifier: X1` — a compile
error, uncatchable by `Try/Except`.

Because of lazy compilation ([chapter 3](03-functions-scope-and-compilation.md)),
this error surfaces only when the function is first called.

**Incorrect**
```pascal
Function MidX(Obj : IPCB_Primitive) : Integer;
Begin
    Result := (Obj.X1 + Obj.X2) Div 2;    // Undeclared identifier: X1
End;
```

Base members available directly on `IPCB_Primitive` include `ObjectId`,
`Layer`, `Net`, `Selected`, `Descriptor`, and the primitive location `x`/`y`.
Members specific to a shape — track endpoints, arc geometry, pad sizes,
component pattern — are not.

---

## 6.2 The narrowing pattern: kind-check, then assign into a typed local

There is no inline cast (`IPCB_Track(Obj)` is rejected — see
[chapter 1](01-language-and-parser.md#18-no-expression-typecasts)). Narrowing is
performed by declaring a typed subtype local and assigning the base value into it
after checking the object's kind. The compiler then resolves the member against
the local's declared (descendant) type.

**Correct**
```pascal
Function MidX(Obj : IPCB_Primitive) : Integer;
Var
    Track : IPCB_Track;
Begin
    Result := 0;
    If Obj.ObjectId = eTrackObject Then
    Begin
        Track := Obj;                          // narrowing by assignment
        Result := (Track.X1 + Track.X2) Div 2; // resolves: IPCB_Track has X1/X2
    End;
End;
```

Multi-type dispatch uses one typed local per descendant and an
`If Obj.ObjectId = … Then Begin Local := Obj; … End` arm per kind:

```pascal
Var
    Track : IPCB_Track;
    Arc   : IPCB_Arc;
    Pad   : IPCB_Pad;
Begin
    If Obj.ObjectId = eTrackObject Then
    Begin
        Track := Obj;
        DX := Track.X2 - Track.X1; DY := Track.Y2 - Track.Y1;
    End
    Else If Obj.ObjectId = eArcObject Then
    Begin
        Arc := Obj;
        R := Arc.Radius; SA := Arc.StartAngle;
    End
    Else If Obj.ObjectId = ePadObject Then
    Begin
        Pad := Obj;
        XS := Pad.TopXSize; H := Pad.HoleSize;
    End;
End;
```

The same rule applies on the schematic side. `Corner` exists only on
`ISch_Rectangle`/`ISch_Line`, not on `ISch_GraphicalObject`; reading
`Obj.Corner` on the base is undeclared. Narrow first:

```pascal
Var R : ISch_Rectangle; L : ISch_Line;
Begin
    If Obj.ObjectId = eRectangle Then Begin R := Obj; C := R.Corner; End
    Else If Obj.ObjectId = eLine  Then Begin L := Obj; C := L.Corner; End;
End;
```

---

## 6.3 Narrowing for *reads* works locally; constraint-property *writes* need iterator-return

The narrowing idiom behaves differently for reads and for constraint writes:

- **Reads** (and ordinary geometry writes) work with the
  `Local := BaseLocal` narrowing shown above. The local points at the same
  object; member access dispatches correctly when the object is that subtype.

- **Constraint-property writes** on the `IPCB_*Constraint` family
  (`IPCB_MaxMinWidthConstraint.MinWidth`,
  `IPCB_ClearanceConstraint.Gap`, `IPCB_MaxMinHoleSizeConstraint.MinLimit`, …)
  do not survive a local-to-local assignment. Assigning
  `TypedConstraint := SomeBaseRuleLocal` and then writing the constraint
  property crashes the script engine at runtime. The `Try/Except`
  around the assignment catches nothing, because the assignment itself does not
  raise; the subsequent write does.

  For these, the typed variable must be assigned directly from a board
  iterator's return value, where the narrowing takes effect:

  **Incorrect (constraint write)**
  ```pascal
  Rule := PCB_FindRuleByName(Board, Name);   // returns IPCB_Rule
  Try RuleWidth := Rule; Except End;         // local-to-local: does NOT narrow for writes
  RuleWidth.MinWidth(eTopLayer) := MilsToCoord(Value);   // crashes the engine
  ```

  **Correct (constraint write)** — re-iterate and take the typed value from the
  iterator:
  ```pascal
  Iter := Board.BoardIterator_Create;
  Try
      Iter.AddFilter_ObjectSet(MkSet(eRuleObject));
      Iter.AddFilter_LayerSet(AllLayers);
      Iter.AddFilter_Method(eProcessAll);
      RuleWidth := Iter.FirstPCBObject;      // narrowing happens at iterator return
      While RuleWidth <> Nil Do
      Begin
          If (RuleWidth.RuleKind = eRule_MaxMinWidth)
             And (RuleWidth.Name = Name) Then
          Begin
              RuleWidth.MinWidth(eTopLayer) := MilsToCoord(Value);   // works
              Break;
          End;
          RuleWidth := Iter.NextPCBObject;
      End;
  Finally
      Board.BoardIterator_Destroy(Iter);
  End;
  ```

Summary:
- Geometry/identity **reads** and ordinary writes: local-to-local narrowing
  applies.
- `IPCB_*Constraint` **value writes**: take the typed reference directly from
  the iterator, never via an intermediate `IPCB_Rule`.
- A rule's **metadata** (Name, Enabled, Scope, Comment) is declared on the base
  `IPCB_Rule` and may be written through the base reference.

---

## 6.4 Some "obvious" subtype properties are read-only or simply absent

Narrowing yields a typed reference, but not every property is writable, and a
few expected properties do not exist:

- `IPCB_Rule.Priority` is a **read-only function**, not a settable property.
  Assigning `Rule.Priority := N` crashes the engine. Reorder rule priority in
  the Altium UI instead.
- `ISch_Component.CurrentFootprintModelName` is **read-only**; assigning it is
  `Undeclared identifier`. To change a placed component's footprint, walk
  `Comp.Implementations` and edit `ISch_Implementation.ModelName`.
- `ISch_Probe.Text` / `ISch_Probe.NetName` are **not settable** — a probe
  auto-adopts the net at its location.
- A pad has **per-layer** sizes (`TopXSize`/`TopYSize`, `MidXSize`/…,
  `BotXSize`/…), not a bare `XSize`/`YSize`.
- Font/colour styling is declared on `ISch_Label`, not on
  `ISch_GraphicalObject` or `ISch_Pin`; a pin's font/colour is not exposed.

See [chapters 7](07-schematic-api.md), [8](08-pcb-api.md), and
[10](10-nonexistent-and-wrong-identifiers.md) for the full property lists.

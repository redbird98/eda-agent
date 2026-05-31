# 9. Document Model (DM) API

The `DM_*` API is the compiled, flattened view of a design: the netlist as the
compiler sees it across the whole project hierarchy. It is the source for
connectivity (which net a pin sits on) and for cross-document queries. Two
constraints govern correctness: the project must be compiled first, and the
property names are not the obvious ones.

---

## 9.1 Compile before reading flattened data

Flattened net names exist only after a `DM_Compile`. Reading
`DM_FlattenedNetName` on a stale model returns empty or wrong values.

```pascal
Project.DM_Compile;          // required before flattened reads
```

A reuse cache around `DM_Compile` is worthwhile when firing several DM queries
back-to-back: each compile can take seconds on a real design, so recompiling per
query is the dominant avoidable cost in a multi-read pass. Reuse a prior result
only when it was the same project compiled within a short TTL.

---

## 9.2 The traversal: project → documents → components → pins

```pascal
For I := 0 To Project.DM_LogicalDocumentCount - 1 Do
Begin
    Doc := Project.DM_LogicalDocuments(I);            // Doc.DM_ComponentCount
    For J := 0 To Doc.DM_ComponentCount - 1 Do
    Begin
        Comp := Doc.DM_Components(J);                 // Comp.DM_PinCount
        Desig := Comp.DM_PhysicalDesignator;
        For K := 0 To Comp.DM_PinCount - 1 Do
        Begin
            Pin := Comp.DM_Pins(K);
            Num  := Pin.DM_PinNumber;                 // designator on the pin
            Name := Pin.DM_PinName;
            Net  := Pin.DM_FlattenedNetName;          // valid only after DM_Compile
        End;
    End;
End;
```

---

## 9.3 Correct property names (the ones that actually exist)

| Use | Not |
|-----|-----|
| `Pin.DM_PinNumber` | ~~`Pin.DM_PinDesignator`~~ (undeclared) |
| `Pin.DM_PinName` | — |
| `Pin.DM_FlattenedNetName` | — |
| `Comp.DM_PhysicalDesignator` | — |
| `Comp.DM_PinCount`, `Comp.DM_Pins(I)` | — |
| `Doc.DM_ComponentCount`, `Doc.DM_Components(I)` | — |
| `Project.DM_LogicalDocumentCount`, `…DM_LogicalDocuments(I)` | — |

There is no `DM_NetCount` on a document. Derive nets from pins, or use the
connectivity surface.

---

## 9.4 Logical vs physical documents

- `DM_LogicalDocuments` are the source sheets (one per `.SchDoc`).
- `DM_PhysicalDocuments` are the flattened/expanded instances; multi-channel
  designs expand here. The fully flattened pin nets live on the physical
  documents. For a flat (single-channel) design the logical traversal is
  sufficient; for hierarchical/multi-channel designs read the physical docs.

---

## 9.5 Resolving a document for save / modified checks

`IClient` exposes no numeric document enumerator: `Client.GetDocumentCount` and
`Client.GetDocument(I)` are undeclared. To obtain an `IServerDocument` (for
`Modified` state or saving), resolve by path:

```pascal
SrvDoc := Client.GetDocumentByPath(Doc.DM_FullPath);
If SrvDoc.Modified Then Client.SaveDocument(SrvDoc);
```

To enumerate open documents, walk the DM project tree
(`Workspace.DM_Projects(i).DM_LogicalDocuments(j)`) and resolve each via
`GetDocumentByPath`. There is no flat `Client`-level list.

> `DM_FileName` may return only the file name rather than the full path on some
> objects. Prefer `DM_FullPath` (and pass explicit paths) when resolving server
> documents.

# Appendix — Environment, Recovery & Reference

Material outside DelphiScript syntax that nonetheless affects scripts in
practice: Altium's config-file encodings, recovery from a startup crash a script
can cause, and the units/coordinate reference.

---

## A. Config-file encodings

Altium config files use different fixed encodings; writing the wrong one corrupts
them silently.

| File | Encoding |
|------|----------|
| `ExtensionsRegistry.xml` | UTF-8 |
| `DXP.RCS` | CP1252 (Windows-1252) |

A tool that rewrites either file must preserve its native encoding. Auto-detect
save as UTF-16 corrupts the file and can prevent Altium from starting.

---

## B. Startup crash recovery (Runtime error 217 at `CreateClient`)

A malformed extension registry or a zombie process can leave Altium unable to
start, typically as Runtime error 217 during `CreateClient`. Recovery:

1. Kill any zombie `X2.EXE` still resident in Task Manager.
2. Validate `ExtensionsRegistry.xml` is well-formed UTF-8 (a partial write from a
   crashed script is the usual cause).
3. Check `DXP.RCS` is intact CP1252.
4. Restart Altium; if it still fails, temporarily move the extension/registry
   file aside to isolate the bad entry.

A script that writes into Altium's config/registry area can prevent startup.
Write such files atomically (temp + rename) and in the correct encoding, or do
not touch them.

---

## C. Units & coordinates

| Quantity | Relationship |
|----------|--------------|
| Internal unit (`TCoord`) | 1 unit = 1/10000 mil = 10⁻⁷ mm |
| 1 mil | 10000 internal units |
| 1 mm | 10000000 / 25.4 ≈ 393700.787 internal units |

```pascal
Function MilsToCoord(Mils : Integer) : TCoord; Begin Result := Mils * 10000; End;
Function CoordToMils(C : TCoord) : Integer;    Begin Result := Round(C / 10000); End;
Function MMToCoord(MM : Double) : TCoord;       Begin Result := Round(MM * 10000000 / 25.4); End;
Function CoordToMM(C : TCoord) : Double;         Begin Result := C * 25.4 / 10000000; End;
```

Convert at the boundary (reading user input or emitting output); keep internal
math in `TCoord`.

---

## D. Angles

- `IPCB_Arc.StartAngle` / `EndAngle` are in degrees (a full circle is
  `0 → 360`). A full-circle arc has no meaningful single endpoint.
- Component / pad rotation is in degrees; schematic orientation uses the
  `eRotate0 / eRotate90 / eRotate180 / eRotate270` enum, set with
  `SetState_Orientation`.

---

## E. Reading a running script's identity

Because compiled units are cached ([chapter 3](03-functions-scope-and-compilation.md#33-compiled-units-are-cached--edits-need-a-reopen)),
the most common cause of a change not taking effect is a stale compiled build. To
detect it, embed a `SCRIPT_VERSION` constant (`YYYY.MM.DD.N`) and expose it
through a status/ping path. A host comparing the reported version against the
on-disk source string can determine that Altium is running a prior compile and
needs a project reopen.

---

## F. The DelphiScript review checklist (consolidated)

Normative summary. The following conditions must hold before a script is
trusted.

**Parser** — No `{` or `}` inside `{ }` comments. No `eXxx` identifiers in
`Case`. No reserved words as identifier names. `Begin…End` around `If…Then Try`
and around compound `Else If` branches. Hex literals are 1–4 or 8 digits. No
`Inc(arr[i])`. No expression typecasts.

**Types** — No typed constants or initialised `Var`. No fixed-size array local
in a `Function`. `TStringList` is function-local only, with no `.Clear`,
`.Insert`, or `''` argument. No open-array parameters. `MaxInt` and related
constants are declared, not assumed. Interface-valued properties are read via
`.Text`.

**Scope** — Every callee is declared above its caller. Helpers live in
early-compiling units. No `{$I}`. `Result` is assigned from a local last.
Reopen the project after edits.

**Runtime** — No `Reset`/`ReadLn` on shared files. `Application.ProcessMessages`
in long loops. `Try/Except` is not used to guard undeclared identifiers.

**Interfaces** — Narrow via `ObjectId` plus a typed local before accessing
subtype members. Constraint writes come from the iterator. No inline casts.

**API names** — Every `eXxx`, `.Method`, `Var.Property`, and `RunProcess` string
is checked against [chapter 10](10-nonexistent-and-wrong-identifiers.md).

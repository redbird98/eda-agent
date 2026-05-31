# Altium DelphiScript — A Practical API Reference

DelphiScript is the Object-Pascal-flavoured scripting language hosted inside
Altium Designer's Scripting Engine. It is a separate interpreter with its own
parser, a reduced type system, and a large, lightly-documented binding to
Altium's SCH / PCB / Workspace object model. The points where its behaviour
diverges from Delphi / Free Pascal are where scripts fail — typically with a
modal error reported against the wrong line, or with silent data corruption
that raises no error.

This reference documents those divergences and the API patterns that work in
practice. Each entry follows a **Problem → Incorrect → Correct → Why**
structure.

> Scope: Altium's DelphiScript — the language behaviour and the SCH/PCB/DM
> scripting API. It is not tied to any one project.

---

## Execution model

| Fact | Consequence |
|------|-------------|
| It is an interpreter with a compile pass, not native Delphi. | Several valid Delphi constructs (typed constants, expression typecasts, open-array params, `Forward`) are rejected or behave differently. |
| Identifiers resolve strictly top-to-bottom within a unit; there is no `Forward;`. | A callee must be defined above its caller in the same file. |
| Functions are compiled lazily, on first call. | A latent compile error in an uncalled function stays hidden until something calls it, then surfaces mid-run. |
| Compiled units are cached. | Editing a `.pas` file has no effect until the script project (`.PrjScr`) is reopened or Altium is restarted. |
| Low-level RTL I/O errors surface as a modal dialog raised by the engine before `Try/Except` runs. | File reads with `Reset`/`ReadLn` can stall the engine on a sharing violation. Use `TStringList` instead. |
| `Try/Except` catches runtime exceptions only. | It cannot catch "Undeclared identifier", which is a compile error and aborts the unit regardless of any `Try`. |
| The scripting engine is single-threaded and shares Altium's UI thread. | A long loop freezes the UI unless it periodically calls `Application.ProcessMessages`. |

### Running a script

1. **DXP → Preferences → Scripting System → Global Projects → Install from
   file** → select the `.PrjScr`.
2. Run an entry procedure via **File → Run Script…**, select the procedure, **Run**.
3. After editing sources, close and reopen the `.PrjScr` (or restart Altium)
   so the engine recompiles from disk; the cache otherwise runs the previous
   code.

### Stopping a running script

- **Run → Stop** = `Ctrl+F3` in the Script IDE.
- A script in an infinite loop: `Ctrl+Pause/Break`.

(Verified against altium.com documentation.)

---

## Normative summary

1. Define every function before it is called in the same file. No `Forward;`.
2. No `{` or `}` inside a `{ … }` comment.
3. `Case` accepts strings and integer literals only — never `eXxx` enum
   identifiers. Use `If / Else If`.
4. No typed constants and no `Var X : T = value` initialisers.
5. No expression typecasts (`Integer(x)`, `Cardinal(x)`, `IPCB_Track(obj)`).
6. A fixed-size local array inside a `Function` silently corrupts the return
   value. Use a `TStringList`.
7. `TStringList` is reliable only as a function-local; it has no `.Clear` or
   `.Insert`.
8. Narrow an interface by assigning a base-typed value into a typed subtype
   local after an `ObjectId`/kind check. There is no syntactic cast, and a
   subtype-only member read off a base interface is "Undeclared identifier".
9. Read/write files with `TStringList.LoadFromFile`/`SaveToFile`, never
   `Reset`/`Rewrite`/`ReadLn` (the EInOutError modal).
10. Many Delphi RTL identifiers are not predefined (`MaxInt`, …) and many
    expected Altium identifiers do not exist. Verify against the API docs.

---

## Chapters

| # | File | Covers |
|---|------|--------|
| — | [`README.md`](README.md) | This overview, execution model, normative summary. |
| 1 | [`01-language-and-parser.md`](01-language-and-parser.md) | Comments, reserved words, `Case`, `If…Then…Try`, `Else If`, hex literals, `Inc`/`Dec`, no typecasts. |
| 2 | [`02-types-and-data-structures.md`](02-types-and-data-structures.md) | Typed constants, `Var` initialisers, fixed arrays in functions, `TStringList` limits, open arrays, `MaxInt`, Variant→OleStr. |
| 3 | [`03-functions-scope-and-compilation.md`](03-functions-scope-and-compilation.md) | No `Forward;`, top-down resolution, lazy compile, `{$I}` ignored, return-value clobbering, the unit cache. |
| 4 | [`04-strings-and-text.md`](04-strings-and-text.md) | Encoding (Latin-1/CP1252), JSON escaping, char-by-char unescape, locale-safe float formatting. |
| 5 | [`05-error-handling-and-runtime.md`](05-error-handling-and-runtime.md) | What `Try/Except` can and cannot catch, the EInOutError file-I/O modal, `Application.ProcessMessages`. |
| 6 | [`06-interfaces-and-type-narrowing.md`](06-interfaces-and-type-narrowing.md) | Narrowing at iterator-return vs typed-local, no inline casts, subtype-on-base, constraint-property writes. |
| 7 | [`07-schematic-api.md`](07-schematic-api.md) | `SchServer`, iterators vs `CompInfoReader`, modify transactions, pin geometry, parameter buckets, placement, labels/fonts, probes, footprint model name. |
| 8 | [`08-pcb-api.md`](08-pcb-api.md) | `PCBServer`, board/spatial/group iterators, board resolution, rules, polygons, vias, coordinates, layers, the ECO limitation. |
| 9 | [`09-document-model-api.md`](09-document-model-api.md) | The `DM_*` flattened-netlist API, correct property names, compile requirement, logical vs physical docs. |
| 10 | [`10-nonexistent-and-wrong-identifiers.md`](10-nonexistent-and-wrong-identifiers.md) | The deny-list: enums, methods, properties, and process strings that don't exist, with the correct names. |
| A | [`appendix-environment-and-recovery.md`](appendix-environment-and-recovery.md) | Config-file encodings, startup crash recovery, units & coordinate reference. |

---

## Conventions used in this reference

- **Incorrect** blocks are code that appears correct but fails (compile error,
  silent corruption, or engine crash).
- **Correct** blocks are the working idiom.
- "Uncatchable" means `Try/Except` does not apply — the failure is a
  compile-time error or an engine-level modal, not a runtime exception.
- Coordinates are in Altium internal units (1 unit = 1/10000 mil = 10⁻⁷ mm)
  unless stated otherwise.

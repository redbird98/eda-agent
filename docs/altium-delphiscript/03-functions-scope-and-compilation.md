# 3. Functions, Scope & Compilation

DelphiScript's compilation model produces a class of defects that surface
intermittently: a script that compiled previously fails after an unrelated edit,
or an error appears only when a particular code path executes. The sections
below document the model and its consequences.

---

## 3.1 No `Forward;` — define every routine before it is called

DelphiScript resolves identifiers strictly top-to-bottom within a unit and
provides no forward declaration mechanism. A `Function`/`Procedure` called on a
line above its definition resolves to "Undeclared identifier" at the call site.

**Incorrect**
```pascal
Function MakeRow(N : Integer) : String;
Begin
    Result := Escape(IntToStr(N));   // Escape not yet defined -> Undeclared identifier: Escape
End;

Function Escape(S : String) : String;
Begin
    ...
End;
```

**Correct** — define the callee first:
```pascal
Function Escape(S : String) : String;
Begin
    ...
End;

Function MakeRow(N : Integer) : String;
Begin
    Result := Escape(IntToStr(N));   // OK: Escape is already defined
End;
```

There is no `Forward;` directive to relax the ordering requirement. The same
rule governs mutual recursion (two routines that call each other cannot both be
defined above their caller — restructure to remove the cycle) and cross-unit
order: a unit may call only routines from units that appear earlier in the
project's compile order, so shared helpers (string/JSON/maths utilities) belong
in an early-compiling unit.

**Exception — built-ins.** A call placed above a local redefinition of an RTL
routine (e.g. a user-defined `StrToIntDef`) binds to the built-in, not the
not-yet-declared local. This is not a forward-reference error.

---

## 3.2 Functions compile lazily — latent errors surface on first call

The engine compiles each routine the first time it is called, not at project
load. A compile error inside a routine that is never invoked stays dormant: the
project loads, the entry point runs, and the error surfaces only when some path
calls the broken routine.

**Implication for testing:** a successful load proves nothing about routines
that did not run. A script is validated only over its exercised call graph.
After adding or editing a routine, exercise it (or compile the whole project as
one unit) before relying on it.

**Implication for debugging:** an error that appears partway through a session is
typically a latent compile error in a routine called for the first time, not a
runtime data problem.

---

## 3.3 Compiled units are cached — edits need a reopen

After a `.pas` source is edited, the engine continues to run the previously
compiled version until the script project is reopened.

**To pick up changes:**
1. Close the script project (`.PrjScr`) in the Projects panel.
2. Reopen it (it recompiles from disk).
3. If a stale error persists, restart Altium — the cache is sticky.

Embed a version string constant in the script and expose it through a
status/ping path, so a host can detect a stale build by comparing the reported
version against the on-disk source. The format `YYYY.MM.DD.N` (bump `N` per
build within a day) makes the mismatch explicit.

---

## 3.4 `{$I file.pas}` include directives are silently ignored

DelphiScript does not honour `{$I}` (include) directives. The parent file
compiles, but the symbols from the included file never enter scope, producing
blank "Undeclared identifier" errors at runtime for everything the include was
expected to provide.

**Incorrect**
```pascal
{$I helpers.pas}          // silently ignored
...
DoHelperThing;            // Undeclared identifier: DoHelperThing
```

**Correct** — add the file to the script project (`.PrjScr`) as a unit, in the
correct compile order, instead of including it.

---

## 3.5 The return value can be clobbered by the last `String` argument

In some patterns, assigning a long `String` to `Result` and then returning
causes the caller to receive a different argument's value instead of the
intended result. The reliable workaround is to compute the value into a local
and assign it to `Result` as the last statement before the function returns.

**Incorrect**
```pascal
Function ReadAll(Path : String) : String;
Begin
    ...
    Result := SL.Text;     // before an Exit / further work -> caller may get `Path`
    If Done Then Exit;
    ...
End;
```

**Correct**
```pascal
Function ReadAll(Path : String) : String;
Var
    Content : String;
Begin
    Result := '';
    ...
    Content := SL.Text;    // stash in a local
    ...
    Result := Content;     // assign Result last
End;
```

---

## 3.6 Don't change the arity of procedures exported to the Run Script dialog

Altium's Run Script surface and several core dialogs (Find Similar Objects, and
others) share the scripting namespace. Adding dummy parameters to an exported
procedure — for example to declutter the Run Script picker — changes its arity
and breaks that shared dispatch, surfacing as "Bad parameters count" in
unrelated core dialogs.

Keep exported entry-point procedures parameterless, or with the exact signature
the host expects; do not add throwaway parameters.

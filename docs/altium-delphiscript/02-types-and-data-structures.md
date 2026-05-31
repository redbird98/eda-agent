# 2. Types & Data Structures

DelphiScript's type system is a subset of Delphi's. Several constructs that
compile in Delphi either fail outright or corrupt data silently with no error.
The silent cases are the hardest to diagnose.

---

## 2.1 No typed constants, no initialised `Var`

DelphiScript supports untyped constants and uninitialised variable
declarations only.

**Incorrect**
```pascal
Const cPi : Double = 3.14159;     // typed constant
Var   X   : Integer = 5;          // initialised Var
```
Both raise "Typed constants aren't supported", with no line number pointing at
the declaration.

**Correct**
```pascal
Const cPi = 3.14159;              // untyped constant (type inferred)
Var
    X : Integer;
Begin
    X := 5;                       // initialise in the body
End;
```

---

## 2.2 Common Delphi RTL constants are **not predefined**

DelphiScript does not predefine `MaxInt`, `MinInt`, `MaxLongInt`,
`MinLongInt`. Using one raises "Undeclared identifier" — uncatchable by
`Try/Except`, and (because of lazy compilation) it may only surface when the
enclosing function is first called.

**Incorrect**
```pascal
MinSeen := MaxInt;                // Undeclared identifier: MaxInt
```

**Correct**
```pascal
Const MAX_INT = 2147483647;       // declare it once (untyped const)
...
MinSeen := MAX_INT;
```
…or use the literal `2147483647` at the site.

**Why** — Treat any well-known Delphi global as unavailable until verified. The
same applies to RTL routines assumed to exist.

---

## 2.3 A fixed-size local array inside a `Function` corrupts the return value

Any fixed-size local array (`Array[0..N] Of T`, for any element type — String,
Integer, Double, an interface, …) declared inside a `Function` corrupts that
function's `Result`. The function returns garbage: typically either an echo of
an input argument or an empty/`{}` value. No error is raised.

**Incorrect**
```pascal
Function BuildRow(Vals : String) : String;
Var
    Cells : Array[0..7] Of String;   // <-- corrupts Result, silently
    I : Integer;
Begin
    ...
    Result := Joined;                // caller receives garbage
End;
```

**Correct**
```pascal
Function BuildRow(Vals : String) : String;
Var
    Cells : TStringList;
Begin
    Cells := TStringList.Create;
    Try
        ...
    Finally
        Cells.Free;
    End;
    Result := Joined;
End;
```

**Scope of the bug**
- `Function` with a fixed-size local array → corrupts `Result`.
- `Procedure`s are unaffected (no return value to clobber).
- Module-level fixed arrays are unaffected.

Use a `TStringList` for the dynamic collection inside functions, move the array
to module scope, or restructure as a procedure with a `Var` out-parameter.

---

## 2.4 `TStringList` — only reliable as a function-local; no `.Clear`, no `.Insert`

`TStringList` is the dynamic container of choice, subject to the following
constraints.

**a) It only works as a function-local.**
A `TStringList` declared at module scope, or returned from a Function, raises
"Undeclared identifier: Count" (and similar) when its methods are called.

**Incorrect**
```pascal
Var
    GBuffer : TStringList;        // module scope -> methods undeclared
...
Function MakeList : TStringList;  // returning one -> same failure
```

**Correct** — keep it local, or share state another way:
```pascal
Procedure DoWork;
Var
    Buf : TStringList;            // function-local: fine
Begin
    Buf := TStringList.Create;
    Try ... Finally Buf.Free; End;
End;
```
To share an accumulating collection across helpers, pass a function-local list
by `Var` into the helpers, or use a pipe-delimited `String` as the carrier (no
class methods to resolve).

**b) `TStringList.Clear` is undeclared.**
`TStrings.Clear` exists only via a VCL property (e.g. `TMemo.Lines.Clear`);
plain `TStringList.Clear` is not exposed.

**Incorrect**
```pascal
L.Clear;
```
**Correct**
```pascal
While L.Count > 0 Do L.Delete(0);
```

**c) `TStringList.Insert` is undeclared.**
Same family as `.Clear`. To prepend, shift manually or rebuild the list.

**d) Do not pass `''` directly to `.Add` / `.SetText` / load-save methods.**
An empty-string literal as a call argument to these mutators fails. Route it
through a `String` variable first.

**Incorrect**
```pascal
L.Add('');
```
**Correct**
```pascal
Var Empty : String;
Begin
    Empty := '';
    L.Add(Empty);
End;
```

---

## 2.5 No open-array parameters (`Var X : Array of T`)

An open-array parameter fails the call with "wrong number of params": the
engine expands the parameter into a hidden `(base_ptr, high_index)` pair that
fixed-size arrays do not satisfy.

**Incorrect**
```pascal
Procedure ApplyAll(Var Items : Array of String);
```

**Correct** — pass a delimited string and walk it, or use a cursor function:
```pascal
// Caller packs items as 'a|b|c'; callee splits on '|'.
Function NextToken(Var Remaining : String) : String;
Var P : Integer;
Begin
    P := Pos('|', Remaining);
    If P > 0 Then
    Begin
        Result := Copy(Remaining, 1, P - 1);
        Remaining := Copy(Remaining, P + 1, Length(Remaining));
    End
    Else Begin Result := Remaining; Remaining := ''; End;
End;
```

---

## 2.6 Variant → OleStr conversion failures (the Dispatch-interface trap)

DelphiScript lets a Variant flow into a parameter declared `String`. If the
value is a compound interface (an Altium SDK object that returns an interface
where text was expected), the implicit `Dispatch → OleStr` conversion fails at
runtime with "Could not convert variant of type (Dispatch) into type
(OleStr)". On some paths this raises a modal that escapes a surrounding
`Try/Except`.

The typical case is a property assumed to be a string that is actually an
interface. A PCB component's `Name` is an `IPCB_Text` object, not a string;
reading it where a string is expected triggers the conversion error.

**Incorrect**
```pascal
S := Comp.Name;          // Comp.Name is IPCB_Text, not String -> Dispatch->OleStr
```

**Correct**
```pascal
S := Comp.Name.Text;     // take the .Text of the IPCB_Text
```

**Defensive pattern** — when a function takes a `String` that might receive an
interface from a careless caller, copy through a local inside a `Try/Except` so
a bad value yields `''` instead of crashing:
```pascal
Function SafeStr(S : String) : String;
Var Tmp : String;
Begin
    Result := '';
    Try
        Tmp := S;        // forces the conversion here, guarded
    Except
        Exit;
    End;
    Result := Tmp;
End;
```

See also [chapter 7](07-schematic-api.md) and [chapter 8](08-pcb-api.md) for
the specific properties that return interfaces rather than strings.

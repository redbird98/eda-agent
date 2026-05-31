# 1. Language & Parser Quirks

These are parse-time issues. They fail at compile time, so `Try/Except` does
not apply, and the reported error frequently points at a different line than
the cause.

---

## 1.1 Never put `{` or `}` inside a `{ … }` comment

A literal `}` inside a `{ … }` comment closes the comment at that point;
everything after it on the line is parsed as code. DelphiScript comments do not
nest, so this also applies to a `{` opened inside an existing `{ … }` comment.

**Incorrect**
```pascal
{ Returns an empty JSON object like {} when nothing matches }
Result := '';
```
The `{` then `}` inside the comment terminate it at `{}`, and `when nothing
matches }` becomes code.

**Correct**
```pascal
{ Returns an empty JSON object (no braces in this comment) }
Result := '';
```

**Why** — The resulting compile error usually surfaces as "Unterminated
string" or an undeclared identifier on a line that looks correct. To show
braces in a comment, paraphrase or use a `(* … *)` comment with braces kept
out of it.

---

## 1.2 `Case` works on strings and integer literals only — never on enums

The `Case` selector accepts string values and integer literals. It does not
accept Altium `eXxx` enumeration identifiers as case labels; the enum form
raises a compile error.

**Incorrect**
```pascal
Case Obj.ObjectId Of
    eTrackObject: Result := 'track';
    ePadObject:   Result := 'pad';
End;
```

**Correct**
```pascal
If Obj.ObjectId = eTrackObject Then Result := 'track'
Else If Obj.ObjectId = ePadObject Then Result := 'pad'
Else Result := 'other';
```

A string subject is valid:
```pascal
Case Category Of
    'application': ...;
    'pcb':         ...;
End;
```

**Why** — Use an `If / Else If` chain for any `eXxx` dispatch.

---

## 1.3 Reserved keywords cannot be used as identifiers

A Delphi keyword used as a parameter or local-variable name throws "Expression
expected but `<Word>` found", and the error points at the function header, not
the declaration.

Reserved words most often misused as names:

```
Label  Type  Class  Object  Record  Array  Set  String  File  Unit
Function  Procedure  Const  Var  End  Begin  If  Then  Else
Goto  With  In  Is  As  Of  Out
```

**Incorrect**
```pascal
Procedure SetNetLabel(Label : String);   // 'Label' is reserved
Var
    File : String;                        // 'File' is reserved
```

**Correct**
```pascal
Procedure SetNetLabel(LabelText : String);
Var
    FilePath : String;
```

---

## 1.4 `If … Then Try …` needs an explicit `Begin … End`

An inline multi-statement `Try` directly inside an unwrapped `If … Then` body
breaks the parser.

**Incorrect**
```pascal
If Found Then
    Try
        DoStepOne;
        DoStepTwo;
    Except
    End;
```

**Correct**
```pascal
If Found Then
Begin
    Try
        DoStepOne;
        DoStepTwo;
    Except
    End;
End;
```

A single-statement inline form (`If X Then Try DoOne; Except End;`) compiles,
but wrapping in `Begin … End` is the reliable form.

---

## 1.5 Chained `Else If` branches need `Begin … End` on compound bodies

Mirror of 1.4. A multi-statement body on an `Else If` branch without
`Begin … End` throws `; expected` at the inner statement.

**Incorrect**
```pascal
If A Then DoA
Else If B Then
    X := 1;
    Y := 2;        // parser error: this is outside the Else If
Else If C Then DoC;
```

**Correct**
```pascal
If A Then DoA
Else If B Then
Begin
    X := 1;
    Y := 2;
End
Else If C Then DoC;
```

No semicolon before `Else`: `End` (no `;`) then `Else` continues the chain.

---

## 1.6 Hex literals must be 1–4 digits or exactly 8

A hex constant of 5 to 7 digits silently aborts the entire unit. The error
does not appear on the literal; it surfaces as "Undeclared identifier" on a
later line that is itself correct.

**Incorrect**
```pascal
Const Mask = $0020212;     // 7 digits -> unit silently fails to compile
```

**Correct**
```pascal
Const Mask = $00202120;    // exactly 8 digits
Const Flag = $20;          // 1-4 digits OK
```

**Why** — The parser accepts short literals (Byte/Word range, 1–4 hex digits)
and full 32-bit literals (exactly 8), but mis-handles the in-between widths.
When diagnosing an unexplained "Undeclared identifier", grep the file for
`\$[0-9A-Fa-f]+` and confirm every literal is 1–4 or 8 digits.

---

## 1.7 `Inc` / `Dec` do not accept an array or record element

`Inc(x)` works on a plain variable, but `Inc(arr[i])` raises `) expected`; the
parser supports only the identifier form.

**Incorrect**
```pascal
Inc(Counts[I]);
Dec(Buffer[K]);
```

**Correct**
```pascal
Counts[I] := Counts[I] + 1;
Buffer[K] := Buffer[K] - 1;
```

---

## 1.8 No expression typecasts

DelphiScript rejects `TypeName(value)` as an expression cast, even though the
type names are valid in declarations. This applies to numeric casts and to
interface casts alike.

**Incorrect**
```pascal
N := Integer(SomeValue);          // expression typecast - rejected
W := Cardinal(Flags);
Track := IPCB_Track(Obj);         // inline interface cast - rejected
S := ISch_NetLabel(Obj).Text;     // inline interface cast - rejected
```

**Correct**
```pascal
Var
    N : Integer;
    Track : IPCB_Track;
Begin
    N := SomeValue;               // implicit conversion via a typed local
    ...
    If Obj.ObjectId = eTrackObject Then
    Begin
        Track := Obj;             // narrowing by assignment, after a kind check
        ... Track.X1 ...
    End;
    ... Obj.Text ...              // for late-bound access, call the member directly
End;
```

**Why** — For numbers, declare a typed local and assign into it (implicit
conversion). For interfaces, see
[chapter 6](06-interfaces-and-type-narrowing.md): assign the base into a typed
subtype local after a kind check; never write `IPCB_Track(Obj)`.

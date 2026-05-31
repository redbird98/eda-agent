# 5. Error Handling & Runtime Behaviour

The failures most worth guarding against in DelphiScript are not runtime
exceptions, so `Try/Except` cannot catch them — the construct must be avoided
rather than wrapped. The list of uncatchable failures below is the relevant set.

---

## 5.1 Failures `Try/Except` will NOT save you from

- **"Undeclared identifier"** — a compile error. The whole unit fails to
  compile, so the `Try` never runs because nothing executes. A typo'd enum, a
  non-existent method, a missing forward definition, or a subtype-only member
  read off a base interface (`Obj.X1` where `Obj : IPCB_Primitive`,
  [chapter 6](06-interfaces-and-type-narrowing.md)) all land here, and none are
  caught by any `Try`.
- **The EInOutError file-I/O modal** raised by `Reset`/`ReadLn` — the engine
  raises a modal before control reaches the `Except` (5.2).
- **Some Dispatch->OleStr conversion failures** raise a modal rather than an
  exception ([chapter 2](02-types-and-data-structures.md#26-variant--olestr-conversion-failures-the-dispatch-interface-trap)).

```pascal
// Correct use - a genuine runtime risk (a bad object mid-iteration):
Try PadName := Pad.Name.Text; Except PadName := ''; End;

// Useless - this is a COMPILE error if Obj is the base type; the Try is dead code:
Try X := Obj.X1; Except End;
```

If the risk is whether an identifier or member exists on a type, `Try/Except`
has no effect — verify the name or narrow the type. Reserve `Try/Except` for nil
objects and properties that throw at runtime.

---

## 5.2 The EInOutError file-I/O modal trap

Classic RTL file I/O — `AssignFile`/`Reset`/`Rewrite`/`ReadLn`/`WriteLn` — raises
a low-level `EInOutError` (I/O error 32, sharing violation) when the file is
briefly locked (antivirus scan, another process writing it). The Altium
scripting engine intercepts that error with a modal dialog raised before the
surrounding `Try/Except` executes, which stalls the single-threaded engine until
a human dismisses it.

**Incorrect**
```pascal
AssignFile(F, Path);
Try
    Reset(F);
    ReadLn(F, Line);     // EInOutError here pops an engine modal, freezing the loop
Except
End;
CloseFile(F);
```

**Correct** — read and write through `TStringList`, which goes via a
`TFileStream` and raises an ordinary VCL exception that `Try/Except` catches. Add
a short retry loop for transient locks:

```pascal
Function ReadFileSafe(Path : String; Var Content : String) : Boolean;
Var SL : TStringList; Attempt : Integer;
Begin
    Result := False;
    For Attempt := 1 To 10 Do
    Begin
        SL := TStringList.Create;
        Try
            Try
                SL.LoadFromFile(Path);
                Content := SL.Text;     // stash in OUT param, not the function Result directly
                Result := True;
            Except
                Result := False;
            End;
        Finally
            SL.Free;
        End;
        If Result Then Break;
        Sleep(15);                       // brief backoff for a transient lock
    End;
End;
```

**Rules that follow from this:**
- Never use `Reset`/`Rewrite`/`ReadLn`/`WriteLn` for files another process may
  touch. Use `TStringList.LoadFromFile` / `SaveToFile`.
- If a script consumes files produced by another program, that program must write
  them atomically (write to a temp file, then rename/replace) so the script only
  ever opens a fully-closed file. A reader cannot defend against a half-written
  file by itself.
- For output, build the full content in memory and `SaveToFile` once, rather than
  appending line by line.

---

## 5.3 Keep the UI alive in long loops

The scripting engine runs on Altium's UI thread. A long loop with no yield
freezes the application, and built-in toolbar buttons that themselves depend on
scripting appear to hang. Pump the message queue periodically:

```pascal
For I := 0 To Count - 1 Do
Begin
    DoWork(I);
    If (I Mod 200) = 0 Then Application.ProcessMessages;
End;
```

Notes:
- The call is `Application.ProcessMessages`, not `Client.ProcessMessages` (the
  latter does not exist — see
  [chapter 10](10-nonexistent-and-wrong-identifiers.md)).
- Do not call it every iteration on a tight loop; the overhead dominates. Every
  few hundred iterations is sufficient.

---

## 5.4 Don't assume `Win32`/system APIs are available

The DelphiScript sandbox does not expose arbitrary Win32 calls. `GetAsyncKeyState`
and similar user32/kernel32 functions are not available. OS-level behaviour
(timing, key state, process control) must come from outside the script, not from
a `Windows` unit import.

`Sleep` is available and is the correct way to back off in a retry loop.

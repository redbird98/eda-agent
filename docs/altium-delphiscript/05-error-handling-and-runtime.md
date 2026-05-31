# 5. Runtime Behaviour

Three characteristics of the scripting host that are not visible from the
language: a low-level I/O error surfaces as a blocking modal, the engine is
single-threaded on the UI, and the Win32 surface is sandboxed.

---

## 5.1 The EInOutError file-I/O modal

RTL file I/O (`AssignFile`/`Reset`/`Rewrite`/`ReadLn`/`WriteLn`) raises
`EInOutError` (I/O error 32, sharing violation) when the file is briefly locked —
an antivirus scan or another process writing it. The scripting host intercepts
that error with a **modal dialog, raised before the surrounding `Try/Except`
runs**, which stalls the single-threaded engine until the dialog is dismissed by
hand. `TStringList.LoadFromFile`/`SaveToFile` go through a `TFileStream` and
raise an ordinary, catchable VCL exception instead.

**Incorrect**
```pascal
AssignFile(F, Path);
Reset(F);
ReadLn(F, Line);          // a transient lock here pops an engine modal, freezing the loop
CloseFile(F);
```

**Correct** — `TStringList`, with a short retry for transient locks:
```pascal
Function ReadFileSafe(Path : String; Var Content : String) : Boolean;
Var SL : TStringList; Attempt : Integer;
Begin
    Result := False;
    For Attempt := 1 To 10 Do
    Begin
        SL := TStringList.Create;
        Try
            Try SL.LoadFromFile(Path); Content := SL.Text; Result := True;
            Except Result := False; End;
        Finally SL.Free; End;
        If Result Then Break;
        Sleep(15);
    End;
End;
```

A producer feeding files to a script should write them atomically (temp file,
then rename/replace); a reader cannot defend against a half-written file.

---

## 5.2 Single-threaded engine

The engine runs on Altium's UI thread, so a long loop with no yield freezes the
application — including toolbar actions that themselves run script. Call
`Application.ProcessMessages` periodically. The call is **`Application.ProcessMessages`**,
not `Client.ProcessMessages`, which does not exist
([chapter 10](10-nonexistent-and-wrong-identifiers.md)).

```pascal
If (I Mod 200) = 0 Then Application.ProcessMessages;
```

---

## 5.3 The Win32 surface is sandboxed

`GetAsyncKeyState` and other user32/kernel32 calls are not exposed; OS-level
behaviour must come from outside the script. `Sleep` is available.

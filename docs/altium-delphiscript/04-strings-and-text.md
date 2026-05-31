# 4. Strings & Text

DelphiScript strings are single-byte (ANSI/Latin-1 era) and the engine runs in
the host's locale. Two concerns dominate when a script emits structured text
(JSON, CSV, netlists) for another program to parse: encoding (non-ASCII and
control characters) and locale (decimal separators).

---

## 4.1 Strings are single-byte; treat non-ASCII as Latin-1 / CP1252

A `String` character is one byte. Bytes >= 128 are interpreted in the host's
single-byte code page (effectively Latin-1 / CP1252). Text that will be read as
UTF-8 elsewhere must have its non-ASCII bytes escaped or transcoded; emitting the
raw byte produces mojibake or a parse error on the reader side.

The robust convention for machine-readable output is to emit pure ASCII and
escape everything else, so the consumer never has to infer the encoding.

---

## 4.2 JSON string escaping — do it once, escape every control byte

When building JSON by hand, every key and every string value must be escaped
exactly once. A correct escaper handles `\`, `"`, the C0 control characters
(`\b \t \n \f \r`), and any byte >= 128 (as `\u00XX`).

A two-path escaper — a fast `StringReplace` path for the common pure-ASCII case,
and a char-by-char slow path that emits `\u00XX` for any non-ASCII or control
byte — keeps output pure ASCII:

```pascal
Function EscapeJson(S : String) : String;
Var
    Tmp, Ch : String;
    I, O : Integer;
    NeedsSlow : Boolean;
Begin
    Result := '';
    Tmp := S;
    NeedsSlow := False;
    For I := 1 To Length(Tmp) Do
    Begin
        O := Ord(Tmp[I]);
        If (O >= 128) Or ((O < 32) And (O <> 9) And (O <> 10) And (O <> 13)) Then
        Begin NeedsSlow := True; Break; End;
    End;

    If Not NeedsSlow Then
    Begin
        Tmp := StringReplace(Tmp, '\', '\\', -1);   // backslash FIRST
        Tmp := StringReplace(Tmp, '"', '\"', -1);
        Tmp := StringReplace(Tmp, #13, '\r', -1);
        Tmp := StringReplace(Tmp, #10, '\n', -1);
        Tmp := StringReplace(Tmp, #9,  '\t', -1);
        Result := Tmp;
        Exit;
    End;

    For I := 1 To Length(Tmp) Do
    Begin
        Ch := Copy(Tmp, I, 1);
        O := Ord(Ch[1]);
        If O >= 128 Then Result := Result + '\u00' + IntToHex(O, 2)
        Else If O = Ord('\') Then Result := Result + '\\'
        Else If O = Ord('"') Then Result := Result + '\"'
        Else If O = 13 Then Result := Result + '\r'
        Else If O = 10 Then Result := Result + '\n'
        Else If O = 9  Then Result := Result + '\t'
        Else If O < 32 Then Result := Result + '\u00' + IntToHex(O, 2)
        Else Result := Result + Ch;
    End;
End;
```

**Order matters:** escape the backslash before the other characters in the
`StringReplace` path, otherwise the backslashes just introduced are
double-escaped.

---

## 4.3 Unescaping JSON must be char-by-char, not a `StringReplace` cascade

The inverse — turning `\n`, `\"`, `\\`, `\uXXXX` back into bytes — cannot be done
with a sequence of `StringReplace` calls. Left-to-right replacement collapses
`\\` to `\` before it evaluates the following character, so a string like `\\nlc`
(a literal backslash followed by `nlc`) is mis-decoded into a newline + `lc`.
Real-world identifiers exhibit this: active-low signal names (`\RESET`,
`\WD_OUT`) and Windows path segments (`\nlc_480`, `\tmp`, `\reports`) all contain
backslash sequences that a cascade mangles.

**Correct** — walk the string once, handling each escape in place:
```pascal
Function UnescapeJson(S : String) : String;
Var I, O, Code : Integer; Ch : String;
Begin
    Result := '';
    I := 1;
    While I <= Length(S) Do
    Begin
        Ch := Copy(S, I, 1);
        If (Ch = '\') And (I < Length(S)) Then
        Begin
            Inc(I);
            Ch := Copy(S, I, 1);
            If Ch = 'n' Then Result := Result + #10
            Else If Ch = 'r' Then Result := Result + #13
            Else If Ch = 't' Then Result := Result + #9
            Else If Ch = '"' Then Result := Result + '"'
            Else If Ch = '\' Then Result := Result + '\'
            Else If Ch = '/' Then Result := Result + '/'
            Else If Ch = 'u' Then
            Begin
                Code := StrToIntDef('$' + Copy(S, I + 1, 4), 0);
                If Code <= 255 Then Result := Result + Chr(Code)
                Else Result := Result + '?';     // outside single-byte range
                Inc(I, 4);
            End
            Else Result := Result + Ch;
        End
        Else Result := Result + Ch;
        Inc(I);
    End;
End;
```

`\uXXXX` values <= 255 map to a single ANSI byte via `Chr`; values above the
single-byte range have no representation in a `String` and are replaced with a
placeholder.

---

## 4.4 Locale-safe float formatting

`FloatToStr` uses the host's `DecimalSeparator`. On a comma-locale machine it
emits `90,0`, which is invalid JSON and breaks CSV columns. Force a `.` for the
duration of the call:

```pascal
Function FloatToJson(Value : Double) : String;
Var OldSep : Char;
Begin
    OldSep := DecimalSeparator;
    DecimalSeparator := '.';
    Try
        Result := FloatToStr(Value);
    Finally
        DecimalSeparator := OldSep;
    End;
End;
```

Apply the same guard to any `StrToFloat`/`FloatToStrF` that must be
locale-independent.

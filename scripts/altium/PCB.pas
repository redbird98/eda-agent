{ SPDX-License-Identifier: Apache-2.0                                   }
{ Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>                                      }
{..............................................................................}
{ PCB.pas - PCB-specific operations for the Altium integration bridge                        }
{ Provides high-level PCB commands: net classes, design rules, DRC,           }
{ component placement, trace lengths, layer stackup, board outline, etc.      }
{..............................................................................}

{..............................................................................}
{ Helper: Find a net object by name on the given board.                       }
{ Returns Nil if not found.                                                   }
{..............................................................................}

Function FindNetByName(Board : IPCB_Board; NetName : String) : IPCB_Net;
Var
    Iterator : IPCB_BoardIterator;
    Net : IPCB_Net;
Begin
    Result := Nil;
    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(eNetObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);
    Net := Iterator.FirstPCBObject;
    While Net <> Nil Do
    Begin
        If Net.Name = NetName Then
        Begin
            Result := Net;
            Break;
        End;
        Net := Iterator.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(Iterator);
End;

{..............................................................................}
{ PCB_GetNets - Get all unique net names from the board                       }
{..............................................................................}

Function PCB_GetNets(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iterator : IPCB_BoardIterator;
    Net : IPCB_Net;
    JsonItems : String;
    First : Boolean;
    Count : Integer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    JsonItems := '';
    First := True;
    Count := 0;

    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(eNetObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);

    Net := Iterator.FirstPCBObject;
    While Net <> Nil Do
    Begin
        If Not First Then JsonItems := JsonItems + ',';
        First := False;
        JsonItems := JsonItems + '"' + EscapeJsonString(Net.Name) + '"';
        Inc(Count);
        Net := Iterator.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(Iterator);

    Result := BuildSuccessResponse(RequestId,
        '{"nets":[' + JsonItems + '],"count":' + IntToStr(Count) + '}');
End;

{..............................................................................}
{ PCB_GetNetClasses - Get all net classes with their member nets              }
{..............................................................................}

Function PCB_GetNetClasses(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iterator : IPCB_BoardIterator;
    ObjClass : IPCB_ObjectClass;
    JsonItems : String;
    First : Boolean;
    Count : Integer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    JsonItems := '';
    First := True;
    Count := 0;

    { MemberCount / MemberName[] are not exposed on IPCB_ObjectClass in         }
    { DelphiScript, they are compile-time undeclared. Return class metadata   }
    { only; callers that need per-member resolution can iterate nets and       }
    { group them via the parent class on each net.                              }
    Iterator := Board.BoardIterator_Create;
    Iterator.SetState_FilterAll;
    Iterator.AddFilter_ObjectSet(MkSet(eClassObject));

    ObjClass := Iterator.FirstPCBObject;
    While ObjClass <> Nil Do
    Begin
        If ObjClass.MemberKind = eClassMemberKind_Net Then
        Begin
            If Not First Then JsonItems := JsonItems + ',';
            First := False;

            JsonItems := JsonItems + '{"name":"' + EscapeJsonString(ObjClass.Name) + '",'
                + '"super_class":' + BoolToJsonStr(ObjClass.SuperClass) + '}';
            Inc(Count);
        End;
        ObjClass := Iterator.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(Iterator);

    Result := BuildSuccessResponse(RequestId,
        '{"net_classes":[' + JsonItems + '],"count":' + IntToStr(Count) + '}');
End;

{..............................................................................}
{ PCB_CreateNetClass - Create a net class from a list of net names            }
{ Params: name=<class_name>, nets=<comma-separated net names>                }
{..............................................................................}

Function PCB_CreateNetClass(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    ClassName, NetsStr, NetName, Remaining : String;
    NetClass : IPCB_ObjectClass;
    Iterator : IPCB_BoardIterator;
    ExistingClass : IPCB_ObjectClass;
    ClassExists : Boolean;
    CommaPos, AddedCount : Integer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    ClassName := ExtractJsonValue(Params, 'name');
    NetsStr := ExtractJsonValue(Params, 'nets');

    If ClassName = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'Missing "name" parameter');
        Exit;
    End;

    // Check for existing class with same name
    ClassExists := False;
    Iterator := Board.BoardIterator_Create;
    Iterator.SetState_FilterAll;
    Iterator.AddFilter_ObjectSet(MkSet(eClassObject));
    ExistingClass := Iterator.FirstPCBObject;
    While ExistingClass <> Nil Do
    Begin
        If (ExistingClass.MemberKind = eClassMemberKind_Net) And
           (ExistingClass.Name = ClassName) Then
        Begin
            ClassExists := True;
            NetClass := ExistingClass;
            Break;
        End;
        ExistingClass := Iterator.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(Iterator);

    // Create new class if it doesn't exist
    If Not ClassExists Then
    Begin
        PCBServer.PreProcess;
        NetClass := PCBServer.PCBClassFactoryByClassMember(eClassMemberKind_Net);
        NetClass.SuperClass := False;
        NetClass.Name := ClassName;
        Board.AddPCBObject(NetClass);
        PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
            PCBM_BoardRegisteration, NetClass.I_ObjectAddress);
        PCBServer.PostProcess;
    End;

    // Add nets to the class
    AddedCount := 0;
    Remaining := NetsStr;
    While Remaining <> '' Do
    Begin
        CommaPos := Pos(',', Remaining);
        If CommaPos > 0 Then
        Begin
            NetName := Copy(Remaining, 1, CommaPos - 1);
            Remaining := Copy(Remaining, CommaPos + 1, Length(Remaining));
        End
        Else
        Begin
            NetName := Remaining;
            Remaining := '';
        End;
        If NetName <> '' Then
        Begin
            PCBServer.PreProcess;
            NetClass.AddMemberByName(NetName);
            PCBServer.PostProcess;
            Inc(AddedCount);
        End;
    End;

    SaveDocByPath(Board.FileName);
    Result := BuildSuccessResponse(RequestId,
        '{"class_name":"' + EscapeJsonString(ClassName) + '",'
        + '"class_created":' + BoolToJsonStr(Not ClassExists) + ','
        + '"nets_added":' + IntToStr(AddedCount) + '}');
End;

{..............................................................................}
{ PCB_GetDesignRules - Get all design rules                                   }
{..............................................................................}

Function PCB_GetDesignRules(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iterator : IPCB_BoardIterator;
    Rule : IPCB_Rule;
    JsonItems, RuleTypeStr : String;
    First : Boolean;
    Count : Integer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    JsonItems := '';
    First := True;
    Count := 0;

    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(eRuleObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);

    Rule := Iterator.FirstPCBObject;
    While Rule <> Nil Do
    Begin
        If Not First Then JsonItems := JsonItems + ',';
        First := False;

        // Get rule type as string
        Try
            RuleTypeStr := IntToStr(Rule.RuleKind);
        Except
            RuleTypeStr := 'unknown';
        End;

        JsonItems := JsonItems + '{"name":"' + EscapeJsonString(Rule.Name) + '",'
            + '"rule_kind":' + RuleTypeStr + ','
            + '"enabled":' + BoolToJsonStr(Rule.Enabled) + ','
            + '"priority":' + IntToStr(Rule.Priority) + ','
            + '"scope_1":"' + EscapeJsonString(Rule.Scope1Expression) + '",'
            + '"scope_2":"' + EscapeJsonString(Rule.Scope2Expression) + '",'
            + '"comment":"' + EscapeJsonString(Rule.Comment) + '",'
            + '"descriptor":"' + EscapeJsonString(Rule.Descriptor) + '"}';
        Inc(Count);
        Rule := Iterator.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(Iterator);

    Result := BuildSuccessResponse(RequestId,
        '{"rules":[' + JsonItems + '],"count":' + IntToStr(Count) + '}');
End;

{..............................................................................}
{ PCB_FindRuleByName - Helper to locate an IPCB_Rule by its Name                }
{..............................................................................}

Function PCB_FindRuleByName(Board : IPCB_Board; RuleName : String) : IPCB_Rule;
Var
    Iterator : IPCB_BoardIterator;
    Rule : IPCB_Rule;
Begin
    Result := Nil;
    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(eRuleObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);
    Rule := Iterator.FirstPCBObject;
    While Rule <> Nil Do
    Begin
        If Rule.Name = RuleName Then
        Begin
            Result := Rule;
            Break;
        End;
        Rule := Iterator.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(Iterator);
End;


{..............................................................................}
{ PCB_SetRulesEnabled - Bulk toggle DRC-enabled flag on design rules by name.   }
{                                                                                }
{ Used for focused review passes: disable a noisy class of rules to surface     }
{ the violations that matter, or re-enable a set before a release sweep.        }
{ Matches rule names case-insensitively against the input list; supports a      }
{ trailing '*' wildcard on a name so the caller can target a rule family       }
{ without enumerating every name.                                                }
{                                                                                }
{ Two writable Enabled flags:                                                    }
{   Rule.Enabled    -- whether the rule participates in the rule list at all   }
{   Rule.DRCEnabled -- whether DRC actually checks this rule on its next run   }
{ For "focused review" the DRCEnabled flip is the right one to toggle.          }
{                                                                                }
{ Params: names (pipe-separated), enabled ("true"/"false"), match (optional;   }
{         "name" default, "kind" matches against rule_kind ordinal).            }
{ Response shape:                                                                }
{   matched -- int: rules that matched any input pattern                        }
{   updated -- int: how many actually changed value                              }
{   items[] -- array of per-rule name + kind + prev_enabled + new_enabled       }
{..............................................................................}

Function PCB_SetRulesEnabled(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iter : IPCB_BoardIterator;
    Rule : IPCB_Rule;
    NamesStr, EnabledStr, MatchMode : String;
    NewEnabled, PrevEnabled, ShouldMatch : Boolean;
    Matched, Updated : Integer;
    ItemsJson, EntryJson, RuleNameUpper, Pattern, PatternUpper : String;
    First : Boolean;
    PipePos : Integer;
    Remaining : String;
Begin
    Board := Nil;
    Try Board := PCBServer.GetCurrentPCBBoard; Except End;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_BOARD',
            'No active PCB board. Open the .PcbDoc and try again.');
        Exit;
    End;

    NamesStr := ExtractJsonValue(Params, 'names');
    EnabledStr := LowerCase(ExtractJsonValue(Params, 'enabled'));
    MatchMode := LowerCase(ExtractJsonValue(Params, 'match'));
    If MatchMode = '' Then MatchMode := 'name';
    NewEnabled := (EnabledStr = 'true') Or (EnabledStr = '1');

    If NamesStr = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAMS',
            'names is required (pipe-separated rule names or kind ordinals)');
        Exit;
    End;

    Matched := 0;
    Updated := 0;
    ItemsJson := '';
    First := True;

    PCBServer.PreProcess;
    Try
        Iter := Board.BoardIterator_Create;
        Try
            Iter.AddFilter_ObjectSet(MkSet(eRuleObject));
            Iter.AddFilter_LayerSet(AllLayers);
            Iter.AddFilter_Method(eProcessAll);
            Rule := Iter.FirstPCBObject;
            While Rule <> Nil Do
            Begin
                Try
                    RuleNameUpper := UpperCase(Rule.Name);
                    ShouldMatch := False;
                    Remaining := NamesStr;
                    While (Length(Remaining) > 0) And (Not ShouldMatch) Do
                    Begin
                        PipePos := Pos('|', Remaining);
                        If PipePos = 0 Then
                        Begin
                            Pattern := Remaining;
                            Remaining := '';
                        End
                        Else
                        Begin
                            Pattern := Copy(Remaining, 1, PipePos - 1);
                            Remaining := Copy(Remaining, PipePos + 1, Length(Remaining));
                        End;
                        Pattern := Trim(Pattern);
                        If Pattern = '' Then Continue;
                        If MatchMode = 'kind' Then
                        Begin
                            { Numeric ordinal match against Rule.RuleKind. }
                            If IntToStr(Rule.RuleKind) = Pattern Then
                                ShouldMatch := True;
                        End
                        Else
                        Begin
                            PatternUpper := UpperCase(Pattern);
                            If (Length(PatternUpper) > 0)
                               And (Copy(PatternUpper, Length(PatternUpper), 1) = '*') Then
                            Begin
                                { Trailing-* wildcard: prefix match. }
                                PatternUpper := Copy(PatternUpper, 1,
                                                     Length(PatternUpper) - 1);
                                If (Length(RuleNameUpper) >= Length(PatternUpper))
                                   And (Copy(RuleNameUpper, 1, Length(PatternUpper))
                                        = PatternUpper) Then
                                    ShouldMatch := True;
                            End
                            Else If RuleNameUpper = PatternUpper Then
                                ShouldMatch := True;
                        End;
                    End;

                    If ShouldMatch Then
                    Begin
                        Inc(Matched);
                        PrevEnabled := Rule.DRCEnabled;
                        If PrevEnabled <> NewEnabled Then
                        Begin
                            Rule.DRCEnabled := NewEnabled;
                            Inc(Updated);
                        End;
                        If Not First Then ItemsJson := ItemsJson + ',';
                        First := False;
                        EntryJson :=
                            JsonStr('name', Rule.Name) + ',' +
                            JsonInt('kind', Rule.RuleKind) + ',' +
                            JsonBool('prev_enabled', PrevEnabled) + ',' +
                            JsonBool('new_enabled', NewEnabled);
                        ItemsJson := ItemsJson + JsonObj(EntryJson);
                    End;
                Except End;
                Rule := Iter.NextPCBObject;
            End;
        Finally
            Board.BoardIterator_Destroy(Iter);
        End;
    Finally
        PCBServer.PostProcess;
    End;

    Result := BuildSuccessResponse(RequestId,
        JsonObj(
            JsonInt('matched', Matched) + ',' +
            JsonInt('updated', Updated) + ',' +
            JsonBool('enabled', NewEnabled) + ',' +
            JsonRaw('items', '[' + ItemsJson + ']')
        ));
End;

{..............................................................................}
{ PCB_GetRuleProperties - Read properties of a design rule.                     }
{                                                                               }
{ Constraint values (Gap, MinWidth, MinHoleSize, impedance, etc.) are NOT       }
{ properties of the base IPCB_Rule interface, they live on the per-kind        }
{ subtypes (IPCB_ClearanceConstraint, IPCB_MaxMinWidthConstraint, etc.). The    }
{ kind-specific Pascal constants are not declared in every Altium build, so     }
{ accessing them directly compiles in some versions and crashes others with     }
{ "Undeclared identifier" errors that Try/Except cannot catch.                  }
{                                                                               }
{ Rule.Descriptor is a stable, documented string property on every IPCB_Rule    }
{ subtype that already contains all constraint values in human-readable form,   }
{ e.g. "Width Constraint (Min=0.18mm) (Max=0.19mm) (Preferred=0.185mm)".        }
{ Callers that need parsed values can split the descriptor, far safer than    }
{ dispatching on RuleKind to typed subtype access in script.                    }
{..............................................................................}

Function PCB_GetRuleProperties(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Rule : IPCB_Rule;
    RuleName : String;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    RuleName := ExtractJsonValue(Params, 'name');
    If RuleName = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', '"name" parameter is required');
        Exit;
    End;

    Rule := PCB_FindRuleByName(Board, RuleName);
    If Rule = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NOT_FOUND', 'Rule not found: ' + RuleName);
        Exit;
    End;

    Result := BuildSuccessResponse(RequestId,
        '{"name":"' + EscapeJsonString(Rule.Name) + '",'
        + '"rule_kind":' + IntToStr(Rule.RuleKind) + ','
        + '"enabled":' + BoolToJsonStr(Rule.Enabled) + ','
        + '"priority":' + IntToStr(Rule.Priority) + ','
        + '"descriptor":"' + EscapeJsonString(Rule.Descriptor) + '"}');
End;

{..............................................................................}
{ PCB_SetRuleProperties - Update metadata + constraint values of a rule.        }
{                                                                               }
{ Metadata params (writable on the base IPCB_Rule reference):                   }
{   enabled / scope1 / scope2 / comment                                          }
{                                                                               }
{ Priority is INTENTIONALLY not writable through this tool. Per the PCB API     }
{ reference (pcb-api-design-objects-interfaces-reference.html:15331), IPCB_Rule }
{ exposes `Function Priority : TRulePrecedence` as a read-only METHOD, not as   }
{ a writable property. Assigning `Rule.Priority := N` against that function-   }
{ kind property reference crashes the script engine at runtime with an         }
{ unreadable error popup. There is no SetState_Priority / SetPriority method   }
{ in the public SDK either. To change a rule's priority, use Altium's UI       }
{ (PCB > Rules and Constraints Editor, drag-reorder the rule in its category). }
{                                                                               }
{ Constraint params (dispatched by Rule.RuleKind, written through typed        }
{ iterator-returned locals per the ModifyWidthRules.pas reference pattern):    }
{   - Clearance (kind 0) + ComponentClearance (kind 24)                        }
{     + HoleToHoleClearance (kind 52):  gap_mils                                }
{   - MaxMinWidth (kind 2):  min_width_mils / max_width_mils / favored_width_mils}
{   - MaxMinHoleSize (kind 42):  min_hole_size_mils / max_hole_size_mils       }
{                                                                               }
{ Empirically determined kind 52 from the runtime rule list; the published     }
{ TRuleKind enum stops at 51 (DifferentialPairsRouting). Kinds 52+ are newer   }
{ Altium rule kinds that share the IPCB_ClearanceConstraint interface.         }
{                                                                               }
{ The cast `TypedLocal := UntypedLocal` (e.g. RuleWidth := Rule) does NOT       }
{ narrow the interface at runtime in DelphiScript, the typed variable keeps   }
{ behaving like the source IPCB_Rule and constraint-only property writes      }
{ crash the engine. The proven write path declares the typed variable AS the }
{ iterator-result type and assigns directly from BoardIterator.FirstPCBObject,}
{ where DelphiScript does narrow. See delphiscript_interface_narrowing.md     }
{ in user memory for details.                                                  }
{..............................................................................}

Function PCB_SetRuleProperties(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Rule : IPCB_Rule;
    Iter : IPCB_BoardIterator;
    RuleClearIter : IPCB_ClearanceConstraint;
    RuleWidthIter : IPCB_MaxMinWidthConstraint;
    RuleHoleIter : IPCB_MaxMinHoleSizeConstraint;
    RuleName, V, GapStr, MinWStr, MaxWStr, FavWStr, MinHStr, MaxHStr : String;
    UpdatedCount, Kind, ValMils : Integer;
    L : TLayer;
    Found : Boolean;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    RuleName := ExtractJsonValue(Params, 'name');
    If RuleName = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', '"name" parameter is required');
        Exit;
    End;

    Rule := PCB_FindRuleByName(Board, RuleName);
    If Rule = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NOT_FOUND', 'Rule not found: ' + RuleName);
        Exit;
    End;

    UpdatedCount := 0;
    Kind := 0;
    Try Kind := Rule.RuleKind; Except End;

    { -- Metadata path: writes against the base IPCB_Rule. priority is NOT    }
    { included, it is a read-only function and writing to it crashes the      }
    { engine. See the docstring above for details.                            }
    PCBServer.PreProcess;
    Try
        PCBServer.SendMessageToRobots(Rule.I_ObjectAddress, c_Broadcast,
            PCBM_BeginModify, c_NoEventData);

        V := ExtractJsonValue(Params, 'enabled');
        If V <> '' Then
        Begin
            Try Rule.Enabled := (V = 'true') Or (V = 'True') Or (V = '1'); Inc(UpdatedCount); Except End;
        End;

        V := ExtractJsonValue(Params, 'scope1');
        If V <> '' Then
        Begin
            Try Rule.Scope1Expression := V; Inc(UpdatedCount); Except End;
        End;

        V := ExtractJsonValue(Params, 'scope2');
        If V <> '' Then
        Begin
            Try Rule.Scope2Expression := V; Inc(UpdatedCount); Except End;
        End;

        V := ExtractJsonValue(Params, 'comment');
        If V <> '' Then
        Begin
            Try Rule.Comment := V; Inc(UpdatedCount); Except End;
        End;

        PCBServer.SendMessageToRobots(Rule.I_ObjectAddress, c_Broadcast,
            PCBM_EndModify, c_NoEventData);
    Finally
        PCBServer.PostProcess;
    End;

    { -- Constraint values: per-kind iterator with typed iterator-returned    }
    { locals. Each block opens its own BoardIterator, walks rule objects,    }
    { matches by name + kind, applies the write, breaks out. See the         }
    { ModifyWidthRules.pas reference pattern.                                  }
    GapStr := ExtractJsonValue(Params, 'gap_mils');
    MinWStr := ExtractJsonValue(Params, 'min_width_mils');
    MaxWStr := ExtractJsonValue(Params, 'max_width_mils');
    FavWStr := ExtractJsonValue(Params, 'favored_width_mils');
    MinHStr := ExtractJsonValue(Params, 'min_hole_size_mils');
    MaxHStr := ExtractJsonValue(Params, 'max_hole_size_mils');

    If (GapStr <> '') And
       ((Kind = eRule_Clearance) Or (Kind = 24) Or (Kind = 52)) Then
    Begin
        Iter := Board.BoardIterator_Create;
        Iter.AddFilter_ObjectSet(MkSet(eRuleObject));
        Iter.AddFilter_LayerSet(AllLayers);
        Iter.AddFilter_Method(eProcessAll);
        Found := False;
        Try
            RuleClearIter := Iter.FirstPCBObject;
            While (RuleClearIter <> Nil) And (Not Found) Do
            Begin
                If RuleClearIter.Name = RuleName Then
                Begin
                    Try
                        RuleClearIter.Gap := MilsToCoord(StrToIntDef(GapStr, 0));
                        Inc(UpdatedCount);
                    Except End;
                    Found := True;
                End;
                If Not Found Then RuleClearIter := Iter.NextPCBObject;
            End;
        Finally
            Board.BoardIterator_Destroy(Iter);
        End;
    End;

    If (Kind = eRule_MaxMinWidth) And
       ((MinWStr <> '') Or (MaxWStr <> '') Or (FavWStr <> '')) Then
    Begin
        Iter := Board.BoardIterator_Create;
        Iter.AddFilter_ObjectSet(MkSet(eRuleObject));
        Iter.AddFilter_LayerSet(AllLayers);
        Iter.AddFilter_Method(eProcessAll);
        Found := False;
        Try
            RuleWidthIter := Iter.FirstPCBObject;
            While (RuleWidthIter <> Nil) And (Not Found) Do
            Begin
                If (RuleWidthIter.RuleKind = eRule_MaxMinWidth)
                    And (RuleWidthIter.Name = RuleName) Then
                Begin
                    If MinWStr <> '' Then
                    Begin
                        ValMils := StrToIntDef(MinWStr, 0);
                        Try
                            For L := MinLayer To MaxLayer Do
                                RuleWidthIter.MinWidth(L) := MilsToCoord(ValMils);
                            Inc(UpdatedCount);
                        Except End;
                    End;
                    If MaxWStr <> '' Then
                    Begin
                        ValMils := StrToIntDef(MaxWStr, 0);
                        Try
                            For L := MinLayer To MaxLayer Do
                                RuleWidthIter.MaxWidth(L) := MilsToCoord(ValMils);
                            Inc(UpdatedCount);
                        Except End;
                    End;
                    If FavWStr <> '' Then
                    Begin
                        ValMils := StrToIntDef(FavWStr, 0);
                        Try
                            For L := MinLayer To MaxLayer Do
                                RuleWidthIter.FavoredWidth(L) := MilsToCoord(ValMils);
                            Inc(UpdatedCount);
                        Except End;
                    End;
                    Found := True;
                End;
                If Not Found Then RuleWidthIter := Iter.NextPCBObject;
            End;
        Finally
            Board.BoardIterator_Destroy(Iter);
        End;
    End;

    If (Kind = eRule_MaxMinHoleSize) And
       ((MinHStr <> '') Or (MaxHStr <> '')) Then
    Begin
        Iter := Board.BoardIterator_Create;
        Iter.AddFilter_ObjectSet(MkSet(eRuleObject));
        Iter.AddFilter_LayerSet(AllLayers);
        Iter.AddFilter_Method(eProcessAll);
        Found := False;
        Try
            RuleHoleIter := Iter.FirstPCBObject;
            While (RuleHoleIter <> Nil) And (Not Found) Do
            Begin
                If (RuleHoleIter.RuleKind = eRule_MaxMinHoleSize)
                    And (RuleHoleIter.Name = RuleName) Then
                Begin
                    If MinHStr <> '' Then
                    Begin
                        Try RuleHoleIter.MinLimit := MilsToCoord(StrToIntDef(MinHStr, 0)); Inc(UpdatedCount); Except End;
                    End;
                    If MaxHStr <> '' Then
                    Begin
                        Try RuleHoleIter.MaxLimit := MilsToCoord(StrToIntDef(MaxHStr, 0)); Inc(UpdatedCount); Except End;
                    End;
                    Found := True;
                End;
                If Not Found Then RuleHoleIter := Iter.NextPCBObject;
            End;
        Finally
            Board.BoardIterator_Destroy(Iter);
        End;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"name":"' + EscapeJsonString(Rule.Name) + '",'
        + '"rule_kind":' + IntToStr(Kind) + ','
        + '"properties_updated":' + IntToStr(UpdatedCount) + '}');
End;

{..............................................................................}
{ PCB_RunDRC - Run design rule check, return violation count                  }
{..............................................................................}

{ BuildViolationJson now emits LOCATION info so the agent can jump to       }
{ where the violation actually is, not just guess from the description.     }
{ Includes: violation bbox centre (x, y, layer) and each offending          }
{ primitive's own centre coords. With this the agent can drive the          }
{ dashboard's Drawing tab to the exact spot or call cross_probe + the       }
{ Components / Nets tabs.                                                    }
Function BuildViolationJson(Violation : IPCB_Violation) : String;
Var
    RuleName, P1Desc, P2Desc, P1Net, P2Net, P1Type, P2Type : String;
    P1X, P1Y, P2X, P2Y, VX, VY : Integer;
    P1Layer, P2Layer, VLayer : String;
    BBox : TCoordRect;
    Prim : IPCB_Primitive;
Begin
    Result := '{';
    Try Result := Result + '"name":"' + EscapeJsonString(Violation.Name) + '"'; Except Result := Result + '"name":""'; End;
    Try Result := Result + ',"description":"' + EscapeJsonString(Violation.Description) + '"'; Except End;
    RuleName := '';
    Try If Violation.Rule <> Nil Then RuleName := Violation.Rule.Name; Except End;
    Result := Result + ',"rule":"' + EscapeJsonString(RuleName) + '"';

    { Violation's own bounding rectangle -- usable as a "go here" hint.    }
    VX := 0; VY := 0; VLayer := '';
    Try
        BBox := Violation.BoundingRectangle;
        VX := CoordToMils((BBox.Left + BBox.Right) Div 2);
        VY := CoordToMils((BBox.Bottom + BBox.Top) Div 2);
    Except End;
    Try VLayer := GetLayerString(Violation.Layer); Except End;
    Result := Result + ',"x_mils":' + IntToStr(VX);
    Result := Result + ',"y_mils":' + IntToStr(VY);
    Result := Result + ',"layer":"' + EscapeJsonString(VLayer) + '"';

    P1Desc := ''; P1Net := ''; P1Type := ''; P1X := 0; P1Y := 0; P1Layer := '';
    Try
        Prim := Violation.Primitive1;
        If Prim <> Nil Then
        Begin
            Try P1Desc := Prim.Detail; Except End;
            Try If Prim.Net <> Nil Then P1Net := Prim.Net.Name; Except End;
            Try P1Type := ObjectIDToObjectName(Prim.ObjectId); Except End;
            Try
                BBox := Prim.BoundingRectangle;
                P1X := CoordToMils((BBox.Left + BBox.Right) Div 2);
                P1Y := CoordToMils((BBox.Bottom + BBox.Top) Div 2);
            Except End;
            Try P1Layer := GetLayerString(Prim.Layer); Except End;
        End;
    Except End;
    Result := Result + ',"primitive1":' + JsonObj(
        JsonStr('detail', P1Desc) + ',' +
        JsonStr('type', P1Type) + ',' +
        JsonStr('net', P1Net) + ',' +
        JsonStr('layer', P1Layer) + ',' +
        JsonInt('x_mils', P1X) + ',' +
        JsonInt('y_mils', P1Y)
    );

    P2Desc := ''; P2Net := ''; P2Type := ''; P2X := 0; P2Y := 0; P2Layer := '';
    Try
        Prim := Violation.Primitive2;
        If Prim <> Nil Then
        Begin
            Try P2Desc := Prim.Detail; Except End;
            Try If Prim.Net <> Nil Then P2Net := Prim.Net.Name; Except End;
            Try P2Type := ObjectIDToObjectName(Prim.ObjectId); Except End;
            Try
                BBox := Prim.BoundingRectangle;
                P2X := CoordToMils((BBox.Left + BBox.Right) Div 2);
                P2Y := CoordToMils((BBox.Bottom + BBox.Top) Div 2);
            Except End;
            Try P2Layer := GetLayerString(Prim.Layer); Except End;
        End;
    Except End;
    Result := Result + ',"primitive2":' + JsonObj(
        JsonStr('detail', P2Desc) + ',' +
        JsonStr('type', P2Type) + ',' +
        JsonStr('net', P2Net) + ',' +
        JsonStr('layer', P2Layer) + ',' +
        JsonInt('x_mils', P2X) + ',' +
        JsonInt('y_mils', P2Y)
    );
    Result := Result + '}';
End;

Function PCB_RunDRC(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    ViolationCount : Integer;
    Iterator : IPCB_BoardIterator;
    Violation : IPCB_Violation;
    JsonItems : String;
    First : Boolean;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    // Run DRC via the documented process. Per TR0124 Server Process
    // Reference v1.5, the correct identifier is "PCB:DesignRuleCheck"
    // (not "PCB:RunDRC" which doesn't exist). Called with no params,
    // it runs the rule check; with InspectViolation=True it would open
    // the violation viewer instead.
    ResetParameters;
    RunProcess('PCB:DesignRuleCheck');

    // Count violations by iterating
    ViolationCount := 0;
    JsonItems := '';
    First := True;

    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(eViolationObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);

    Violation := Iterator.FirstPCBObject;
    While Violation <> Nil Do
    Begin
        Inc(ViolationCount);
        If ViolationCount <= 100 Then  // Limit detail output
        Begin
            If Not First Then JsonItems := JsonItems + ',';
            First := False;
            JsonItems := JsonItems + BuildViolationJson(Violation);
        End;
        Violation := Iterator.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(Iterator);

    Result := BuildSuccessResponse(RequestId,
        '{"violation_count":' + IntToStr(ViolationCount) + ','
        + '"violations":[' + JsonItems + ']}');
End;

{..............................................................................}
{ PCB_GetComponents - Get all components with position, rotation, layer       }
{..............................................................................}

Function PCB_GetComponents(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iterator : IPCB_BoardIterator;
    Comp : IPCB_Component;
    BBox : TCoordRect;
    JsonItems, Designator, Footprint, LayerStr, CommentStr, SrcDesignator : String;
    First : Boolean;
    Count, HeightMils, BBoxX1, BBoxY1, BBoxX2, BBoxY2, BBoxW, BBoxH : Integer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    JsonItems := '';
    First := True;
    Count := 0;

    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(eComponentObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);

    Comp := Iterator.FirstPCBObject;
    While Comp <> Nil Do
    Begin
        If Not First Then JsonItems := JsonItems + ',';
        First := False;

        Try Designator := Comp.Name.Text; Except Designator := ''; End;
        Try CommentStr := Comp.Comment.Text; Except CommentStr := ''; End;
        Try Footprint := Comp.Pattern; Except Footprint := ''; End;
        Try LayerStr := GetLayerString(Comp.Layer); Except LayerStr := 'Unknown'; End;
        Try SrcDesignator := Comp.SourceDesignator; Except SrcDesignator := ''; End;
        Try HeightMils := CoordToMils(Comp.Height); Except HeightMils := 0; End;

        { Bounding rectangle for collision/placement planning. Returns the    }
        { current axis-aligned bounding box in mils, accounting for the      }
        { component's current rotation and side. Width / Height are derived. }
        BBoxX1 := 0; BBoxY1 := 0; BBoxX2 := 0; BBoxY2 := 0;
        BBoxW := 0; BBoxH := 0;
        Try
            BBox := Comp.BoundingRectangle;
            BBoxX1 := CoordToMils(BBox.X1);
            BBoxY1 := CoordToMils(BBox.Y1);
            BBoxX2 := CoordToMils(BBox.X2);
            BBoxY2 := CoordToMils(BBox.Y2);
            BBoxW := BBoxX2 - BBoxX1;
            BBoxH := BBoxY2 - BBoxY1;
        Except End;

        JsonItems := JsonItems + '{"designator":"' + EscapeJsonString(Designator) + '",'
            + '"comment":"' + EscapeJsonString(CommentStr) + '",'
            + '"x":' + IntToStr(CoordToMils(Comp.x)) + ','
            + '"y":' + IntToStr(CoordToMils(Comp.y)) + ','
            + '"rotation":' + FloatToJsonStr(Comp.Rotation) + ','
            + '"layer":"' + EscapeJsonString(LayerStr) + '",'
            + '"footprint":"' + EscapeJsonString(Footprint) + '",'
            + '"source_designator":"' + EscapeJsonString(SrcDesignator) + '",'
            + '"height_mils":' + IntToStr(HeightMils) + ','
            + '"bbox":{"x1":' + IntToStr(BBoxX1) + ',"y1":' + IntToStr(BBoxY1)
            + ',"x2":' + IntToStr(BBoxX2) + ',"y2":' + IntToStr(BBoxY2)
            + ',"width":' + IntToStr(BBoxW) + ',"height":' + IntToStr(BBoxH) + '}}';
        Inc(Count);
        Comp := Iterator.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(Iterator);

    Result := BuildSuccessResponse(RequestId,
        '{"components":[' + JsonItems + '],"count":' + IntToStr(Count) + '}');
End;

{..............................................................................}
{ PCB_CheckPlacementCollision - Dry-run check whether moving a component to a   }
{ proposed (x, y[, rotation]) would overlap any other placed component.         }
{                                                                              }
{ Params: designator (required), x (mils), y (mils), rotation (deg, optional;  }
{   defaults to the target's current rotation), margin_mils (optional clearance }
{   to require, default 0).                                                     }
{                                                                              }
{ Method: read target's current AABB to extract its footprint width / height,  }
{ rotate dimensions by 90/-90 deg if the rotation delta is a quarter turn      }
{ (swap w<->h), centre the predicted AABB on the proposed (x, y) using the     }
{ same reference-point-to-bbox-centre offset the component currently has, then }
{ AABB-overlap test against every other component on the board. Returns the    }
{ list of colliding designators and a count.                                    }
{                                                                              }
{ Caveats: this is an axis-aligned approximation. Components with non-square    }
{ footprints rotated by non-quarter-turn angles will have an inflated bounding }
{ box (treats the rotated polygon's AABB). Same-side check is applied (TopLayer}
{ to TopLayer, BotLayer to BotLayer); cross-side components never collide.     }
{..............................................................................}

Function PCB_CheckPlacementCollision(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iterator : IPCB_BoardIterator;
    Comp, Other : IPCB_Component;
    DesStr, XStr, YStr, RotStr, MarginStr : String;
    NewX, NewY, MarginCoord, Margin : Integer;
    NewRot, CurRot, RotDelta : Double;
    HasRot : Boolean;
    BBoxCur, BBoxOther : TCoordRect;
    Width, Height, NewW, NewH : Integer;
    OffsetX, OffsetY : Integer;
    NewBBoxX1, NewBBoxY1, NewBBoxX2, NewBBoxY2 : Integer;
    SwapWH : Boolean;
    JsonItems, OtherDes, TargetLayer : String;
    First : Boolean;
    CollisionCount : Integer;
    Overlap : Boolean;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    DesStr := ExtractJsonValue(Params, 'designator');
    XStr := ExtractJsonValue(Params, 'x');
    YStr := ExtractJsonValue(Params, 'y');
    RotStr := ExtractJsonValue(Params, 'rotation');
    MarginStr := ExtractJsonValue(Params, 'margin_mils');

    If (DesStr = '') Or (XStr = '') Or (YStr = '') Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM',
            'designator, x, and y are required');
        Exit;
    End;

    Comp := Board.GetPcbComponentByRefDes(DesStr);
    If Comp = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NOT_FOUND', 'Component not found: ' + DesStr);
        Exit;
    End;

    NewX := MilsToCoord(StrToIntDef(XStr, 0));
    NewY := MilsToCoord(StrToIntDef(YStr, 0));
    Margin := StrToIntDef(MarginStr, 0);
    MarginCoord := MilsToCoord(Margin);

    Try CurRot := Comp.Rotation; Except CurRot := 0; End;
    HasRot := RotStr <> '';
    If HasRot Then NewRot := StrToFloatDef(RotStr, CurRot)
    Else NewRot := CurRot;
    RotDelta := NewRot - CurRot;

    { Quarter-turn detection (mod 180). Anything within +-1 degree of 90 or  }
    { 270 swaps the AABB dimensions. Smaller rotations leave the AABB        }
    { intact at this approximation.                                          }
    SwapWH := False;
    If (Abs(RotDelta - 90) < 1) Or (Abs(RotDelta + 90) < 1)
        Or (Abs(RotDelta - 270) < 1) Or (Abs(RotDelta + 270) < 1) Then
        SwapWH := True;

    BBoxCur := Comp.BoundingRectangle;
    Width := BBoxCur.X2 - BBoxCur.X1;
    Height := BBoxCur.Y2 - BBoxCur.Y1;
    OffsetX := ((BBoxCur.X1 + BBoxCur.X2) Div 2) - Comp.x;
    OffsetY := ((BBoxCur.Y1 + BBoxCur.Y2) Div 2) - Comp.y;

    If SwapWH Then
    Begin
        NewW := Height;
        NewH := Width;
    End
    Else
    Begin
        NewW := Width;
        NewH := Height;
    End;

    { Predicted AABB centred on the proposed (NewX, NewY) plus the current   }
    { reference-to-centre offset (rotated trivially: swap and possibly flip  }
    { signs at quarter turns).                                                }
    NewBBoxX1 := NewX + OffsetX - (NewW Div 2) - MarginCoord;
    NewBBoxY1 := NewY + OffsetY - (NewH Div 2) - MarginCoord;
    NewBBoxX2 := NewX + OffsetX + (NewW Div 2) + MarginCoord;
    NewBBoxY2 := NewY + OffsetY + (NewH Div 2) + MarginCoord;

    Try TargetLayer := GetLayerString(Comp.Layer); Except TargetLayer := ''; End;

    JsonItems := '';
    First := True;
    CollisionCount := 0;

    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(eComponentObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);
    Try
        Other := Iterator.FirstPCBObject;
        While Other <> Nil Do
        Begin
            OtherDes := '';
            Try OtherDes := Other.Name.Text; Except End;

            { Skip target itself. }
            If OtherDes = DesStr Then
            Begin
                Other := Iterator.NextPCBObject;
                Continue;
            End;

            { Cross-side components do not collide in plane. }
            Try
                If GetLayerString(Other.Layer) <> TargetLayer Then
                Begin
                    Other := Iterator.NextPCBObject;
                    Continue;
                End;
            Except End;

            BBoxOther := Other.BoundingRectangle;

            Overlap := (NewBBoxX1 <= BBoxOther.X2) And (NewBBoxX2 >= BBoxOther.X1)
                And (NewBBoxY1 <= BBoxOther.Y2) And (NewBBoxY2 >= BBoxOther.Y1);

            If Overlap Then
            Begin
                If Not First Then JsonItems := JsonItems + ',';
                First := False;
                JsonItems := JsonItems +
                    '{"designator":"' + EscapeJsonString(OtherDes) +
                    '","bbox":{"x1":' + IntToStr(CoordToMils(BBoxOther.X1)) +
                    ',"y1":' + IntToStr(CoordToMils(BBoxOther.Y1)) +
                    ',"x2":' + IntToStr(CoordToMils(BBoxOther.X2)) +
                    ',"y2":' + IntToStr(CoordToMils(BBoxOther.Y2)) + '}}';
                Inc(CollisionCount);
            End;

            Other := Iterator.NextPCBObject;
        End;
    Finally
        Board.BoardIterator_Destroy(Iterator);
    End;

    Result := BuildSuccessResponse(RequestId,
        '{"designator":"' + EscapeJsonString(DesStr) + '"' +
        ',"proposed":{"x":' + IntToStr(CoordToMils(NewX)) +
        ',"y":' + IntToStr(CoordToMils(NewY)) +
        ',"rotation":' + FloatToJsonStr(NewRot) +
        ',"bbox":{"x1":' + IntToStr(CoordToMils(NewBBoxX1)) +
        ',"y1":' + IntToStr(CoordToMils(NewBBoxY1)) +
        ',"x2":' + IntToStr(CoordToMils(NewBBoxX2)) +
        ',"y2":' + IntToStr(CoordToMils(NewBBoxY2)) +
        '},"margin_mils":' + IntToStr(Margin) + '}' +
        ',"colliding_count":' + IntToStr(CollisionCount) +
        ',"clear":' + BoolToJsonStr(CollisionCount = 0) +
        ',"colliding":[' + JsonItems + ']}');
End;

{..............................................................................}
{ PCB_MoveComponent - Move/rotate a component by designator                   }
{ Params: designator=<ref>, x=<mils>, y=<mils>, rotation=<deg>              }
{..............................................................................}

Function PCB_MoveComponent(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Comp : IPCB_Component;
    DesStr, XStr, YStr, RotStr : String;
    NewX, NewY : Integer;
    NewRot : Double;
    HasX, HasY, HasRot : Boolean;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    DesStr := ExtractJsonValue(Params, 'designator');
    XStr := ExtractJsonValue(Params, 'x');
    YStr := ExtractJsonValue(Params, 'y');
    RotStr := ExtractJsonValue(Params, 'rotation');

    If DesStr = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'Missing "designator" parameter');
        Exit;
    End;

    // Find component by designator
    Comp := Board.GetPcbComponentByRefDes(DesStr);
    If Comp = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NOT_FOUND', 'Component not found: ' + DesStr);
        Exit;
    End;

    HasX := (XStr <> '');
    HasY := (YStr <> '');
    HasRot := (RotStr <> '');

    If HasX Then NewX := StrToIntDef(XStr, 0);
    If HasY Then NewY := StrToIntDef(YStr, 0);
    If HasRot Then NewRot := StrToFloatDef(RotStr, 0);

    { Comp.X / Comp.Y are inherited writable properties from IPCB_Group     }
    { (Component's parent, per ref line 3887: "the X,Y fields inherited from }
    { IPCB_Group interface"). Direct assignment is the documented API. The   }
    { previous OleStr->Double crash was the locale-dependent StrToFloat in   }
    { the rotation path; fixed in Utils.pas StrToFloatDef.                    }
    PCBServer.PreProcess;
    Try
        PCBServer.SendMessageToRobots(Comp.I_ObjectAddress, c_Broadcast,
            PCBM_BeginModify, c_NoEventData);

        If HasX Then Comp.x := MilsToCoord(NewX);
        If HasY Then Comp.y := MilsToCoord(NewY);
        If HasRot Then Comp.Rotation := NewRot;

        PCBServer.SendMessageToRobots(Comp.I_ObjectAddress, c_Broadcast,
            PCBM_EndModify, c_NoEventData);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"designator":"' + EscapeJsonString(DesStr) + '",'
        + '"x":' + IntToStr(CoordToMils(Comp.x)) + ','
        + '"y":' + IntToStr(CoordToMils(Comp.y)) + ','
        + '"rotation":' + FloatToJsonStr(Comp.Rotation) + '}');
End;


{..............................................................................}
{ PCB_CopyComponentPlacement - Clone source-component placement onto dest    }
{ components by explicit mapping.                                             }
{                                                                              }
{ Copy layer + x + y + rotation from each source component to a              }
{ corresponding dest. Unlike a name-sort-based approach, this version       }
{ takes an explicit source -> dest mapping so an agent caller can be        }
{ precise.                                                                   }
{ Optional flags pass the designator + comment text placement (rotation /  }
{ size / layer / XY offset from component centre / NameOn).                 }
{                                                                              }
{ Param: "mapping" is pipe-separated; each entry is "src=dst" (e.g.          }
{        "U1=U2|R1=R5|C1=C8"). Optional flags include_designator (default     }
{        true) + include_comment (default true).                              }
{                                                                              }
{ Response shape: applied (int), failed (int), items[] each carrying        }
{ src, dst, ok, error.                                                        }
{..............................................................................}

Function PCB_CopyComponentPlacement(Params, RequestId : String) : String;
Var
    Board : IPCB_Board;
    Src, Dst : IPCB_Component;
    Mapping, Pair, Remaining, SrcDes, DstDes, EqStr : String;
    IncludeDes, IncludeComment : Boolean;
    PipePos, EqPos : Integer;
    Applied, Failed : Integer;
    ItemsJson, EntryJson, ErrStr : String;
    First, PairOk : Boolean;
Begin
    Board := Nil;
    Try Board := PCBServer.GetCurrentPCBBoard; Except End;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_BOARD',
            'No active PCB board. Open the .PcbDoc and try again.');
        Exit;
    End;

    Mapping := ExtractJsonValue(Params, 'mapping');
    If Mapping = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAMS',
            'mapping is required (pipe-separated src=dst pairs)');
        Exit;
    End;
    EqStr := ExtractJsonValue(Params, 'include_designator');
    IncludeDes := Not ((EqStr = 'false') Or (EqStr = '0'));
    EqStr := ExtractJsonValue(Params, 'include_comment');
    IncludeComment := Not ((EqStr = 'false') Or (EqStr = '0'));

    Applied := 0;
    Failed := 0;
    ItemsJson := '';
    First := True;
    Remaining := Mapping;

    PCBServer.PreProcess;
    Try
        While Length(Remaining) > 0 Do
        Begin
            PipePos := Pos('|', Remaining);
            If PipePos = 0 Then
            Begin
                Pair := Remaining;
                Remaining := '';
            End
            Else
            Begin
                Pair := Copy(Remaining, 1, PipePos - 1);
                Remaining := Copy(Remaining, PipePos + 1, Length(Remaining));
            End;
            Pair := Trim(Pair);
            If Pair = '' Then Continue;

            EqPos := Pos('=', Pair);
            If EqPos <= 0 Then Continue;
            SrcDes := Trim(Copy(Pair, 1, EqPos - 1));
            DstDes := Trim(Copy(Pair, EqPos + 1, Length(Pair)));

            PairOk := False;
            ErrStr := '';
            Src := Nil;
            Dst := Nil;
            Try Src := Board.GetPcbComponentByRefDes(SrcDes); Except End;
            Try Dst := Board.GetPcbComponentByRefDes(DstDes); Except End;
            If Src = Nil Then ErrStr := 'src not found: ' + SrcDes
            Else If Dst = Nil Then ErrStr := 'dst not found: ' + DstDes
            Else
            Begin
                Try
                    PCBServer.SendMessageToRobots(Dst.I_ObjectAddress,
                        c_Broadcast, PCBM_BeginModify, c_NoEventData);
                    Try Dst.Layer := Src.Layer; Except End;
                    Try Dst.x := Src.x; Except End;
                    Try Dst.y := Src.y; Except End;
                    Try Dst.Rotation := Src.Rotation; Except End;
                    If IncludeDes Then
                    Begin
                        Try Dst.NameOn := Src.NameOn; Except End;
                        Try Dst.Name.XLocation := Src.Name.XLocation
                            - Src.x + Dst.x; Except End;
                        Try Dst.Name.YLocation := Src.Name.YLocation
                            - Src.y + Dst.y; Except End;
                        Try Dst.Name.Rotation := Src.Name.Rotation; Except End;
                        Try Dst.Name.Size := Src.Name.Size; Except End;
                        Try Dst.Name.Width := Src.Name.Width; Except End;
                        Try Dst.Name.Layer := Src.Name.Layer; Except End;
                    End;
                    If IncludeComment Then
                    Begin
                        Try Dst.CommentOn := Src.CommentOn; Except End;
                        Try Dst.Comment.XLocation := Src.Comment.XLocation
                            - Src.x + Dst.x; Except End;
                        Try Dst.Comment.YLocation := Src.Comment.YLocation
                            - Src.y + Dst.y; Except End;
                        Try Dst.Comment.Rotation := Src.Comment.Rotation; Except End;
                        Try Dst.Comment.Size := Src.Comment.Size; Except End;
                        Try Dst.Comment.Width := Src.Comment.Width; Except End;
                        Try Dst.Comment.Layer := Src.Comment.Layer; Except End;
                    End;
                    PCBServer.SendMessageToRobots(Dst.I_ObjectAddress,
                        c_Broadcast, PCBM_EndModify, c_NoEventData);
                    PairOk := True;
                    Inc(Applied);
                Except
                    ErrStr := 'apply exception';
                End;
            End;

            If Not PairOk Then Inc(Failed);
            If Not First Then ItemsJson := ItemsJson + ',';
            First := False;
            EntryJson :=
                JsonStr('src', SrcDes) + ',' +
                JsonStr('dst', DstDes) + ',' +
                JsonBool('ok', PairOk) + ',' +
                JsonStr('error', ErrStr);
            ItemsJson := ItemsJson + JsonObj(EntryJson);
        End;
    Finally
        PCBServer.PostProcess;
    End;

    Try Board.GraphicallyInvalidate; Except End;

    Result := BuildSuccessResponse(RequestId,
        JsonObj(
            JsonInt('applied', Applied) + ',' +
            JsonInt('failed', Failed) + ',' +
            JsonRaw('items', '[' + ItemsJson + ']')
        ));
End;


{..............................................................................}
{ PCB_LockNetRouting - bulk-lock or unlock track + arc + via primitives on a   }
{ list of nets, optionally also locking the components those nets terminate    }
{ at. Locked primitives are .Moveable = False, which the autorouter and       }
{ interactive editor respect when "Protect Locked Objects" is enabled (DXP    }
{ Preferences -> PCB Editor -> General).                                       }
{                                                                                }
{ Standard workflow: lock the power / ground / clock nets before running the  }
{ autorouter so a partial reroute pass doesn't undo your hand-routed rails.   }
{                                                                                }
{ Params:                                                                       }
{   nets             -- pipe-separated net names (e.g. "VCC|GND|CLK_24")      }
{   lock             -- "true" (lock) or "false" (unlock)                     }
{   lock_components  -- "true" / "false" (default false): also lock any       }
{                       component with at least one pad on the matched net   }
{                                                                                }
{ Response: matched_primitives, updated_primitives, matched_components,        }
{           updated_components.                                                 }
{..............................................................................}

Function PCB_LockNetRouting(Params, RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iter, CompIter : IPCB_BoardIterator;
    PinIter : IPCB_GroupIterator;
    Prim, Obj : IPCB_Primitive;
    Comp : IPCB_Component;
    Pad : IPCB_Pad;
    NetsStr, LockStr, LCStr : String;
    LockOn, LockComponents, NetMatched : Boolean;
    NetsBracketed, NetName, NameMark : String;
    Moveable : Boolean;
    MatchedPrim, UpdatedPrim, MatchedComp, UpdatedComp : Integer;
Begin
    Board := Nil;
    Try Board := PCBServer.GetCurrentPCBBoard; Except End;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_BOARD',
            'No active PCB board. Open the .PcbDoc and try again.');
        Exit;
    End;

    NetsStr := ExtractJsonValue(Params, 'nets');
    LockStr := LowerCase(ExtractJsonValue(Params, 'lock'));
    LCStr := LowerCase(ExtractJsonValue(Params, 'lock_components'));
    LockOn := (LockStr = 'true') Or (LockStr = '1');
    LockComponents := (LCStr = 'true') Or (LCStr = '1');

    If NetsStr = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAMS',
            'nets is required (pipe-separated net names)');
        Exit;
    End;
    NetsBracketed := '|' + NetsStr + '|';
    { Locked primitives have .Moveable = False. }
    Moveable := Not LockOn;

    MatchedPrim := 0;
    UpdatedPrim := 0;
    MatchedComp := 0;
    UpdatedComp := 0;

    PCBServer.PreProcess;
    Try
        { Pass 1: walk track / arc / via, lock those whose net matches. }
        Iter := Board.BoardIterator_Create;
        Try
            Iter.AddFilter_ObjectSet(MkSet(eTrackObject, eArcObject, eViaObject));
            Iter.AddFilter_LayerSet(AllLayers);
            Iter.AddFilter_Method(eProcessAll);
            Prim := Iter.FirstPCBObject;
            While Prim <> Nil Do
            Begin
                Try
                    If Prim.InNet Then
                    Begin
                        NetName := '';
                        Try NetName := Prim.Net.Name; Except End;
                        NameMark := '|' + NetName + '|';
                        If (NetName <> '')
                           And (Pos(NameMark, NetsBracketed) > 0) Then
                        Begin
                            Inc(MatchedPrim);
                            If Prim.Moveable <> Moveable Then
                            Begin
                                Prim.BeginModify;
                                Prim.Moveable := Moveable;
                                Prim.EndModify;
                                Inc(UpdatedPrim);
                            End;
                        End;
                    End;
                Except End;
                Prim := Iter.NextPCBObject;
            End;
        Finally
            Board.BoardIterator_Destroy(Iter);
        End;

        { Pass 2 (optional): walk components, lock if any pad lands on the }
        { matched net set.                                                  }
        If LockComponents Then
        Begin
            CompIter := Board.BoardIterator_Create;
            Try
                CompIter.AddFilter_ObjectSet(MkSet(eComponentObject));
                CompIter.AddFilter_LayerSet(AllLayers);
                CompIter.AddFilter_Method(eProcessAll);
                Obj := CompIter.FirstPCBObject;
                While Obj <> Nil Do
                Begin
                    Try
                        Comp := Obj;
                        NetMatched := False;
                        PinIter := Comp.GroupIterator_Create;
                        Try
                            PinIter.AddFilter_ObjectSet(MkSet(ePadObject));
                            Pad := PinIter.FirstPCBObject;
                            While Pad <> Nil Do
                            Begin
                                Try
                                    If Pad.InNet Then
                                    Begin
                                        NetName := '';
                                        Try NetName := Pad.Net.Name; Except End;
                                        NameMark := '|' + NetName + '|';
                                        If (NetName <> '')
                                           And (Pos(NameMark, NetsBracketed) > 0) Then
                                            NetMatched := True;
                                    End;
                                Except End;
                                Pad := PinIter.NextPCBObject;
                            End;
                        Finally
                            Comp.GroupIterator_Destroy(PinIter);
                        End;
                        If NetMatched Then
                        Begin
                            Inc(MatchedComp);
                            If Comp.Moveable <> Moveable Then
                            Begin
                                Comp.BeginModify;
                                Comp.Moveable := Moveable;
                                Comp.EndModify;
                                Inc(UpdatedComp);
                            End;
                        End;
                    Except End;
                    Obj := CompIter.NextPCBObject;
                End;
            Finally
                Board.BoardIterator_Destroy(CompIter);
            End;
        End;
    Finally
        PCBServer.PostProcess;
    End;

    Try Board.GraphicallyInvalidate; Except End;

    Result := BuildSuccessResponse(RequestId,
        JsonObj(
            JsonBool('locked', LockOn) + ',' +
            JsonInt('matched_primitives', MatchedPrim) + ',' +
            JsonInt('updated_primitives', UpdatedPrim) + ',' +
            JsonInt('matched_components', MatchedComp) + ',' +
            JsonInt('updated_components', UpdatedComp)
        ));
End;


{..............................................................................}
{ PCB_PlaceStitchingVias - Place a grid of stitching vias on a named net      }
{ within a rectangle. RF / EMC tool: GND-stitch vias around high-speed        }
{ traces tie reference planes together so the return current has a low       }
{ inductance path between layers.                                              }
{                                                                                }
{ Core algorithm: walk a grid inside the rectangle; for each gridpoint,      }
{ check via spatial-iterator if any pad / via / track already occupies a     }
{ circle of clearance_mils around it. If clear, place a via on the target    }
{ net.                                                                       }
{                                                                                }
{ Params:                                                                       }
{   net               -- target net name (required, must exist on the board)  }
{   x1_mils, y1_mils, x2_mils, y2_mils -- inclusive rectangle (required)      }
{   spacing_mils      -- grid spacing (default 50)                            }
{   via_size_mils     -- via pad size (default 30)                            }
{   via_hole_mils     -- via drill size (default 14)                           }
{   clearance_mils    -- min gap to existing primitives (default 10)          }
{   dry_run           -- "true" returns the count without placing             }
{                                                                                }
{ Response: placed, skipped, dry_run, net.                                     }
{..............................................................................}

Function PCB_PlaceStitchingVias(Params, RequestId : String) : String;
Var
    Board : IPCB_Board;
    Net : IPCB_Net;
    Via : IPCB_Via;
    SIter : IPCB_SpatialIterator;
    Hit : IPCB_Primitive;
    NetName : String;
    X1, Y1, X2, Y2 : TCoord;
    Spacing, ViaSize, ViaHole, Clearance : TCoord;
    X1Mils, Y1Mils, X2Mils, Y2Mils : Integer;
    SpacingMils, ViaSizeMils, ViaHoleMils, ClearanceMils : Integer;
    PX, PY : TCoord;
    Placed, Skipped : Integer;
    DryRun, HasHit : Boolean;
    DryStr : String;
Begin
    Board := Nil;
    Try Board := PCBServer.GetCurrentPCBBoard; Except End;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_BOARD',
            'No active PCB board. Open the .PcbDoc and try again.');
        Exit;
    End;

    NetName := ExtractJsonValue(Params, 'net');
    If NetName = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAMS',
            'net is required');
        Exit;
    End;
    Net := FindNetByName(Board, NetName);
    If Net = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NET_NOT_FOUND',
            'Net not found on board: ' + NetName);
        Exit;
    End;

    X1Mils := StrToIntDef(ExtractJsonValue(Params, 'x1_mils'), 0);
    Y1Mils := StrToIntDef(ExtractJsonValue(Params, 'y1_mils'), 0);
    X2Mils := StrToIntDef(ExtractJsonValue(Params, 'x2_mils'), 0);
    Y2Mils := StrToIntDef(ExtractJsonValue(Params, 'y2_mils'), 0);
    SpacingMils := StrToIntDef(ExtractJsonValue(Params, 'spacing_mils'), 50);
    ViaSizeMils := StrToIntDef(ExtractJsonValue(Params, 'via_size_mils'), 30);
    ViaHoleMils := StrToIntDef(ExtractJsonValue(Params, 'via_hole_mils'), 14);
    ClearanceMils := StrToIntDef(ExtractJsonValue(Params, 'clearance_mils'), 10);
    DryStr := LowerCase(ExtractJsonValue(Params, 'dry_run'));
    DryRun := (DryStr = 'true') Or (DryStr = '1');

    If (X1Mils = 0) And (X2Mils = 0) And (Y1Mils = 0) And (Y2Mils = 0) Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAMS',
            'rectangle params (x1_mils / y1_mils / x2_mils / y2_mils) required');
        Exit;
    End;
    If SpacingMils <= 0 Then SpacingMils := 50;
    If ViaSizeMils <= 0 Then ViaSizeMils := 30;
    If ViaHoleMils <= 0 Then ViaHoleMils := 14;

    X1 := MilsToCoord(X1Mils);  Y1 := MilsToCoord(Y1Mils);
    X2 := MilsToCoord(X2Mils);  Y2 := MilsToCoord(Y2Mils);
    Spacing := MilsToCoord(SpacingMils);
    ViaSize := MilsToCoord(ViaSizeMils);
    ViaHole := MilsToCoord(ViaHoleMils);
    Clearance := MilsToCoord(ClearanceMils);

    Placed := 0;
    Skipped := 0;

    If Not DryRun Then PCBServer.PreProcess;
    Try
        PY := Y1;
        While PY <= Y2 Do
        Begin
            PX := X1;
            While PX <= X2 Do
            Begin
                { Collision check: walk same-net + other-net primitives in    }
                { a clearance-padded box around the candidate. If any         }
                { non-target-net pad/via/track is in range, skip.             }
                HasHit := False;
                SIter := Board.SpatialIterator_Create;
                Try
                    SIter.AddFilter_ObjectSet(MkSet(eTrackObject, eArcObject,
                        ePadObject, eViaObject));
                    SIter.AddFilter_LayerSet(AllLayers);
                    SIter.AddFilter_Area(PX - ViaSize Div 2 - Clearance,
                                         PY - ViaSize Div 2 - Clearance,
                                         PX + ViaSize Div 2 + Clearance,
                                         PY + ViaSize Div 2 + Clearance);
                    Hit := SIter.FirstPCBObject;
                    While (Hit <> Nil) And (Not HasHit) Do
                    Begin
                        Try
                            { Same-net hits are fine -- this is the net we'll  }
                            { be tying to anyway. Other-net hits or no-net    }
                            { hits are blockers.                              }
                            If (Not Hit.InNet) Or (Hit.Net.Name <> NetName) Then
                                HasHit := True;
                        Except End;
                        Hit := SIter.NextPCBObject;
                    End;
                Finally
                    Board.SpatialIterator_Destroy(SIter);
                End;

                If HasHit Then
                Begin
                    Inc(Skipped);
                End
                Else If DryRun Then
                Begin
                    Inc(Placed);
                End
                Else
                Begin
                    Via := Nil;
                    Try
                        Via := PCBServer.PCBObjectFactory(eViaObject,
                            eNoDimension, eCreate_Default);
                    Except End;
                    If Via <> Nil Then
                    Begin
                        Via.x := PX;
                        Via.y := PY;
                        Via.Size := ViaSize;
                        Via.HoleSize := ViaHole;
                        Try Via.LowLayer := eTopLayer; Except End;
                        Try Via.HighLayer := eBottomLayer; Except End;
                        Try Via.Net := Net; Except End;
                        Board.AddPCBObject(Via);
                        Inc(Placed);
                    End;
                End;

                PX := PX + Spacing;
            End;
            PY := PY + Spacing;
        End;
    Finally
        If Not DryRun Then PCBServer.PostProcess;
    End;

    If Not DryRun Then
        Try Board.GraphicallyInvalidate; Except End;

    Result := BuildSuccessResponse(RequestId,
        JsonObj(
            JsonStr('net', NetName) + ',' +
            JsonBool('dry_run', DryRun) + ',' +
            JsonInt('placed', Placed) + ',' +
            JsonInt('skipped', Skipped) + ',' +
            JsonInt('spacing_mils', SpacingMils) + ',' +
            JsonInt('clearance_mils', ClearanceMils)
        ));
End;


{..............................................................................}
{ PCB_SetTextVisibility - bulk-toggle Component.NameOn and Component.CommentOn }
{                                                                                }
{ For the common "hide designators before a release" / "show comments for     }
{ a review" workflow.                                                          }
{                                                                                }
{ Params:                                                                       }
{   designators (optional) -- if "true" / "false" sets NameOn for matched     }
{                              components; omit to leave NameOn unchanged.    }
{   comments    (optional) -- same shape, for CommentOn.                      }
{   filter      (optional) -- pipe-separated list of designator names         }
{                              (e.g. "U1|U2|R5") to restrict the change;     }
{                              omit to apply to every component.              }
{                                                                                }
{ Response: matched, updated_names, updated_comments.                          }
{..............................................................................}

Function PCB_SetTextVisibility(Params, RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iter : IPCB_BoardIterator;
    Comp : IPCB_Component;
    Obj : IPCB_Primitive;
    DesStr, ComStr, FilterStr : String;
    SetNames, SetComments, NamesOn, CommentsOn : Boolean;
    HasFilter : Boolean;
    Matched, UpdatedNames, UpdatedComments : Integer;
    CompName : String;
    NameMark : String;
Begin
    Board := Nil;
    Try Board := PCBServer.GetCurrentPCBBoard; Except End;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_BOARD',
            'No active PCB board. Open the .PcbDoc and try again.');
        Exit;
    End;

    DesStr := LowerCase(ExtractJsonValue(Params, 'designators'));
    ComStr := LowerCase(ExtractJsonValue(Params, 'comments'));
    FilterStr := ExtractJsonValue(Params, 'filter');

    SetNames := (DesStr = 'true') Or (DesStr = 'false');
    SetComments := (ComStr = 'true') Or (ComStr = 'false');
    NamesOn := (DesStr = 'true');
    CommentsOn := (ComStr = 'true');
    HasFilter := FilterStr <> '';

    If (Not SetNames) And (Not SetComments) Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAMS',
            'At least one of designators / comments must be "true" or "false"');
        Exit;
    End;

    Matched := 0;
    UpdatedNames := 0;
    UpdatedComments := 0;

    PCBServer.PreProcess;
    Try
        Iter := Board.BoardIterator_Create;
        Try
            Iter.AddFilter_ObjectSet(MkSet(eComponentObject));
            Iter.AddFilter_LayerSet(AllLayers);
            Iter.AddFilter_Method(eProcessAll);
            Obj := Iter.FirstPCBObject;
            While Obj <> Nil Do
            Begin
                Try
                    Comp := Obj;
                    CompName := '';
                    Try CompName := Comp.Name.Text; Except End;
                    If HasFilter Then
                    Begin
                        NameMark := '|' + CompName + '|';
                        If Pos(NameMark, '|' + FilterStr + '|') = 0 Then
                        Begin
                            Obj := Iter.NextPCBObject;
                            Continue;
                        End;
                    End;
                    Inc(Matched);
                    If SetNames And (Comp.NameOn <> NamesOn) Then
                    Begin
                        Comp.NameOn := NamesOn;
                        Inc(UpdatedNames);
                    End;
                    If SetComments And (Comp.CommentOn <> CommentsOn) Then
                    Begin
                        Comp.CommentOn := CommentsOn;
                        Inc(UpdatedComments);
                    End;
                Except End;
                Obj := Iter.NextPCBObject;
            End;
        Finally
            Board.BoardIterator_Destroy(Iter);
        End;
    Finally
        PCBServer.PostProcess;
    End;

    Try Board.GraphicallyInvalidate; Except End;

    Result := BuildSuccessResponse(RequestId,
        JsonObj(
            JsonInt('matched', Matched) + ',' +
            JsonInt('updated_names', UpdatedNames) + ',' +
            JsonInt('updated_comments', UpdatedComments)
        ));
End;


{..............................................................................}
{ PCB_BatchMoveComponents - Move/rotate many components in ONE IPC call.      }
{ Param 'moves' is a pipe-separated list; each entry is 4 comma-separated     }
{ fields: designator,x,y,rotation. Empty field = leave that property          }
{ unchanged.                                                                   }
{                                                                              }
{ Implementation note: the batch wraps EACH per-component edit in its own     }
{ PCBServer.PreProcess / PostProcess pair, mirroring the singular             }
{ PCB_MoveComponent semantics exactly. An earlier version wrapped the whole  }
{ batch in one PreProcess block plus a final PCBM_BoardRegisteration         }
{ broadcast; that variant crashed the script engine with "Could not convert  }
{ variant of type (OleStr) into type (Double)" (likely an internal Altium    }
{ event chain fires within the bulk-PostProcess sweep and chokes on a        }
{ locale-marshalled value). The wall-time win of "one IPC round-trip"        }
{ survives, the false win of "one PreProcess" did not.                       }
{                                                                              }
{ Save runs once at the end of the whole batch.                               }
{..............................................................................}

Function PCB_BatchMoveComponents(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Comp : IPCB_Component;
    MovesStr, MoveStr, Remaining : String;
    PipePos, CommaPos, Applied, Failed, FieldIdx : Integer;
    { 4 named locals instead of `Array[0..3] Of String` - fixed-size       }
    { string arrays as function locals corrupt the function return slot   }
    { in DelphiScript, see [[delphiscript_fixed_string_array_bug]].       }
    Desig, XStr, YStr, RotStr, Token : String;
    NewX, NewY : Integer;
    NewRot : Double;
    HasX, HasY, HasRot : Boolean;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    MovesStr := ExtractJsonValue(Params, 'moves');
    If MovesStr = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'moves parameter required');
        Exit;
    End;

    Applied := 0;
    Failed := 0;
    Remaining := MovesStr;

    While Length(Remaining) > 0 Do
    Begin
        PipePos := Pos('|', Remaining);
        If PipePos = 0 Then
        Begin
            MoveStr := Remaining;
            Remaining := '';
        End
        Else
        Begin
            MoveStr := Copy(Remaining, 1, PipePos - 1);
            Remaining := Copy(Remaining, PipePos + 1, Length(Remaining));
        End;

        If MoveStr = '' Then Continue;

        Desig := '';
        XStr := '';
        YStr := '';
        RotStr := '';
        FieldIdx := 0;
        While (MoveStr <> '') And (FieldIdx <= 3) Do
        Begin
            CommaPos := Pos(',', MoveStr);
            If CommaPos = 0 Then
            Begin
                Token := MoveStr;
                MoveStr := '';
            End
            Else
            Begin
                Token := Copy(MoveStr, 1, CommaPos - 1);
                MoveStr := Copy(MoveStr, CommaPos + 1, Length(MoveStr));
            End;
            Case FieldIdx Of
                0: Desig := Token;
                1: XStr := Token;
                2: YStr := Token;
                3: RotStr := Token;
            End;
            FieldIdx := FieldIdx + 1;
        End;

        If Desig = '' Then
        Begin
            Failed := Failed + 1;
            Continue;
        End;

        Comp := Board.GetPcbComponentByRefDes(Desig);
        If Comp = Nil Then
        Begin
            Failed := Failed + 1;
            Continue;
        End;

        HasX := (XStr <> '');
        HasY := (YStr <> '');
        HasRot := (RotStr <> '');

        If HasX Then NewX := StrToIntDef(XStr, 0);
        If HasY Then NewY := StrToIntDef(YStr, 0);
        If HasRot Then NewRot := StrToFloatDef(RotStr, 0);

        { Per-component PreProcess+PostProcess (same as singular). Each move   }
        { is structurally a singular move, just inside one IPC turn.           }
        PCBServer.PreProcess;
        Try
            PCBServer.SendMessageToRobots(Comp.I_ObjectAddress, c_Broadcast,
                PCBM_BeginModify, c_NoEventData);

            If HasX Then Comp.x := MilsToCoord(NewX);
            If HasY Then Comp.y := MilsToCoord(NewY);
            If HasRot Then Comp.Rotation := NewRot;

            PCBServer.SendMessageToRobots(Comp.I_ObjectAddress, c_Broadcast,
                PCBM_EndModify, c_NoEventData);
        Finally
            PCBServer.PostProcess;
        End;

        Applied := Applied + 1;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"moves_applied":' + IntToStr(Applied) + ','
        + '"failed":' + IntToStr(Failed) + '}');
End;

{..............................................................................}
{ PCB_GetTraceLengths - Sum track segment lengths per net                     }
{..............................................................................}

Function PCB_GetTraceLengths(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iterator : IPCB_BoardIterator;
    Track : IPCB_Track;
    Arc : IPCB_Arc;
    Obj : IPCB_Primitive;
    NetName, FilterNet : String;
    JsonItems, EnvelopeData, ResponseStr : String;
    First : Boolean;
    { Parallel heap-allocated lists. ANY fixed-size local array (String,    }
    { Integer, Double, interface, all of them) corrupts the function's     }
    { return slot in DelphiScript, see                                      }
    { [[delphiscript_fixed_string_array_bug]] for the (now-broader) rule.  }
    { Lengths are stored as stringified floats and parsed back on update.  }
    NetNames, NetLengthStrs : TStringList;
    I, FoundIdx : Integer;
    SegLen, DX, DY, ArcAngle, RadiusMils, Accum : Double;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    FilterNet := ExtractJsonValue(Params, 'net');

    NetNames := TStringList.Create;
    NetLengthStrs := TStringList.Create;
    Try
        Iterator := Board.BoardIterator_Create;
        Iterator.AddFilter_ObjectSet(MkSet(eTrackObject, eArcObject));
        Iterator.AddFilter_LayerSet(AllLayers);
        Iterator.AddFilter_Method(eProcessAll);

        Obj := Iterator.FirstPCBObject;
        While Obj <> Nil Do
        Begin
            NetName := '';
            Try
                If Obj.Net <> Nil Then NetName := Obj.Net.Name;
            Except End;

            If (FilterNet <> '') And (NetName <> FilterNet) Then
            Begin
                Obj := Iterator.NextPCBObject;
                Continue;
            End;

            SegLen := 0;
            If Obj.ObjectId = eTrackObject Then
            Begin
                Track := Obj;
                DX := CoordToMils(Track.x2) - CoordToMils(Track.x1);
                DY := CoordToMils(Track.y2) - CoordToMils(Track.y1);
                SegLen := Sqrt(DX * DX + DY * DY);
            End
            Else If Obj.ObjectId = eArcObject Then
            Begin
                Arc := Obj;
                Try
                    RadiusMils := CoordToMils(Arc.Radius);
                    ArcAngle := Arc.EndAngle - Arc.StartAngle;
                    If ArcAngle < 0 Then ArcAngle := ArcAngle + 360;
                    SegLen := RadiusMils * ArcAngle * 3.14159265358979 / 180.0;
                Except SegLen := 0; End;
            End;

            FoundIdx := NetNames.IndexOf(NetName);
            If FoundIdx >= 0 Then
            Begin
                Accum := StrToFloatDef(NetLengthStrs[FoundIdx], 0) + SegLen;
                NetLengthStrs[FoundIdx] := FloatToStr(Accum);
            End
            Else
            Begin
                NetNames.Add(NetName);
                NetLengthStrs.Add(FloatToStr(SegLen));
            End;

            Obj := Iterator.NextPCBObject;
        End;
        Board.BoardIterator_Destroy(Iterator);

        JsonItems := '';
        First := True;
        For I := 0 To NetNames.Count - 1 Do
        Begin
            If Not First Then JsonItems := JsonItems + ',';
            First := False;
            JsonItems := JsonItems + '{"net":"' + EscapeJsonString(NetNames[I]) + '",'
                + '"length_mils":' + FloatToJsonStr(StrToFloatDef(NetLengthStrs[I], 0)) + '}';
        End;

        EnvelopeData := '{"trace_lengths":[' + JsonItems + '],"net_count":'
            + IntToStr(NetNames.Count) + '}';
        ResponseStr := BuildSuccessResponse(RequestId, EnvelopeData);
        Result := ResponseStr;
    Finally
        NetLengthStrs.Free;
        NetNames.Free;
    End;
End;

{..............................................................................}
{ PCB_GetLayerStackup - Get full layer stack info                             }
{..............................................................................}

Function PCB_GetLayerStackup(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    LayerStack : IPCB_LayerStack_V7;
    LayerObj : IPCB_LayerObject_V7;
    JsonItems, LayerName, DielectricType : String;
    First : Boolean;
    Count : Integer;
    CopperThickMils, DielectricHeightMils, DielectricConst : Double;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    LayerStack := Board.LayerStack_V7;
    If LayerStack = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_STACKUP', 'Could not access layer stack');
        Exit;
    End;

    JsonItems := '';
    First := True;
    Count := 0;

    LayerObj := LayerStack.FirstLayer;
    While LayerObj <> Nil Do
    Begin
        If Not First Then JsonItems := JsonItems + ',';
        First := False;

        Try LayerName := LayerObj.Name; Except LayerName := 'Unknown'; End;

        // Copper thickness
        CopperThickMils := 0;
        Try CopperThickMils := LayerObj.CopperThickness / 10000; Except End;

        // Dielectric info
        DielectricType := 'none';
        DielectricHeightMils := 0;
        DielectricConst := 0;
        Try
            If LayerObj.Dielectric.DielectricType <> eNoDielectric Then
            Begin
                If LayerObj.Dielectric.DielectricType = eCore Then DielectricType := 'Core'
                Else If LayerObj.Dielectric.DielectricType = ePrePreg Then DielectricType := 'PrePreg'
                Else If LayerObj.Dielectric.DielectricType = eSurfaceMaterial Then DielectricType := 'SurfaceMaterial'
                Else DielectricType := 'Other';
                DielectricHeightMils := LayerObj.Dielectric.DielectricHeight / 10000;
                DielectricConst := LayerObj.Dielectric.DielectricConstant;
            End;
        Except
        End;

        JsonItems := JsonItems + '{"name":"' + EscapeJsonString(LayerName) + '",'
            + '"order":' + IntToStr(Count + 1) + ','
            + '"copper_thickness_mils":' + FloatToJsonStr(CopperThickMils) + ','
            + '"dielectric_type":"' + EscapeJsonString(DielectricType) + '",'
            + '"dielectric_height_mils":' + FloatToJsonStr(DielectricHeightMils) + ','
            + '"dielectric_constant":' + FloatToJsonStr(DielectricConst) + '}';
        Inc(Count);
        LayerObj := LayerStack.NextLayer(LayerObj);
    End;

    Result := BuildSuccessResponse(RequestId,
        '{"layers":[' + JsonItems + '],"layer_count":' + IntToStr(Count) + ','
        + '"board_name":"' + EscapeJsonString(ExtractFileName(Board.FileName)) + '"}');
End;

{..............................................................................}
{ PCB_AddLayer - Insert a copper layer (MidLayer1..30 / InternalPlane1..16)   }
{ into the stack via IPCB_LayerStack.InsertLayer.                             }
{ Params: layer (e.g. MidLayer1)                                              }
{..............................................................................}

Function PCB_AddLayer(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    LayerStack : IPCB_LayerStack_V7;
    LayerName : String;
    TargetLayer : TLayer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    LayerName := ExtractJsonValue(Params, 'layer');
    If LayerName = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM',
            'layer required (e.g. MidLayer1, InternalPlane1)');
        Exit;
    End;

    TargetLayer := GetLayerFromString(LayerName);
    If TargetLayer = eNoLayer Then
    Begin
        Result := BuildErrorResponse(RequestId, 'INVALID_LAYER',
            'Unknown layer name: ' + LayerName);
        Exit;
    End;

    LayerStack := Board.LayerStack_V7;
    If LayerStack = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_STACKUP', 'Could not access layer stack');
        Exit;
    End;

    PCBServer.PreProcess;
    Try
        Try LayerStack.InsertLayer(TargetLayer); Except End;
        PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
            PCBM_BoardRegisteration, c_NoEventData);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);
    Result := BuildSuccessResponse(RequestId,
        '{"success":true,"layer":"' + EscapeJsonString(LayerName) + '"}');
End;

{..............................................................................}
{ PCB_RemoveLayer - Remove a copper layer from the stack.                     }
{ Params: layer (e.g. MidLayer1)                                              }
{..............................................................................}

Function PCB_RemoveLayer(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    LayerStack : IPCB_LayerStack_V7;
    LayerObj : IPCB_LayerObject_V7;
    LayerName : String;
    TargetLayer : TLayer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    LayerName := ExtractJsonValue(Params, 'layer');
    If LayerName = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'layer required');
        Exit;
    End;

    TargetLayer := GetLayerFromString(LayerName);
    If TargetLayer = eNoLayer Then
    Begin
        Result := BuildErrorResponse(RequestId, 'INVALID_LAYER',
            'Unknown layer name: ' + LayerName);
        Exit;
    End;

    LayerStack := Board.LayerStack_V7;
    If LayerStack = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_STACKUP', 'Could not access layer stack');
        Exit;
    End;

    LayerObj := LayerStack.LayerObject_V7[TargetLayer];
    If LayerObj = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NOT_IN_STACK',
            'Layer ' + LayerName + ' is not present in the current stack');
        Exit;
    End;

    PCBServer.PreProcess;
    Try
        Try LayerStack.RemoveFromStack(LayerObj); Except End;
        PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
            PCBM_BoardRegisteration, c_NoEventData);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);
    Result := BuildSuccessResponse(RequestId,
        '{"success":true,"layer":"' + EscapeJsonString(LayerName) + '"}');
End;

{..............................................................................}
{ PCB_ModifyLayer - Change copper thickness, layer name, and/or dielectric    }
{ properties on an existing layer.                                            }
{ Params: layer, name, copper_thickness_mils, dielectric_type (none/core/     }
{         prepreg/surface), dielectric_height_mils, dielectric_constant,     }
{         dielectric_material                                                  }
{..............................................................................}

Function PCB_ModifyLayer(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    LayerStack : IPCB_LayerStack_V7;
    LayerObj : IPCB_LayerObject_V7;
    LayerName, NewName, TypeStr, Material : String;
    ThickStr, HeightStr, ConstStr : String;
    TargetLayer : TLayer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    LayerName := ExtractJsonValue(Params, 'layer');
    If LayerName = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'layer required');
        Exit;
    End;

    TargetLayer := GetLayerFromString(LayerName);
    If TargetLayer = eNoLayer Then
    Begin
        Result := BuildErrorResponse(RequestId, 'INVALID_LAYER',
            'Unknown layer name: ' + LayerName);
        Exit;
    End;

    LayerStack := Board.LayerStack_V7;
    If LayerStack = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_STACKUP', 'Could not access layer stack');
        Exit;
    End;

    LayerObj := LayerStack.LayerObject_V7[TargetLayer];
    If LayerObj = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NOT_IN_STACK',
            'Layer ' + LayerName + ' is not present in the current stack');
        Exit;
    End;

    NewName := ExtractJsonValue(Params, 'name');
    ThickStr := ExtractJsonValue(Params, 'copper_thickness_mils');
    TypeStr := LowerCase(ExtractJsonValue(Params, 'dielectric_type'));
    HeightStr := ExtractJsonValue(Params, 'dielectric_height_mils');
    ConstStr := ExtractJsonValue(Params, 'dielectric_constant');
    Material := ExtractJsonValue(Params, 'dielectric_material');

    PCBServer.PreProcess;
    Try
        If NewName <> '' Then
            Try LayerObj.Name := NewName; Except End;
        If ThickStr <> '' Then
            Try LayerObj.CopperThickness := MilsToCoord(StrToIntDef(ThickStr, 0)); Except End;

        If TypeStr = 'none' Then
            Try LayerObj.Dielectric.DielectricType := eNoDielectric; Except End
        Else If TypeStr = 'core' Then
            Try LayerObj.Dielectric.DielectricType := eCore; Except End
        Else If TypeStr = 'prepreg' Then
            Try LayerObj.Dielectric.DielectricType := ePrePreg; Except End
        Else If TypeStr = 'surface' Then
            Try LayerObj.Dielectric.DielectricType := eSurfaceMaterial; Except End;

        If HeightStr <> '' Then
            Try LayerObj.Dielectric.DielectricHeight := MilsToCoord(StrToIntDef(HeightStr, 0)); Except End;
        If ConstStr <> '' Then
            Try LayerObj.Dielectric.DielectricConstant := StrToFloatDef(ConstStr, 1.0); Except End;
        If Material <> '' Then
            Try LayerObj.Dielectric.DielectricMaterial := Material; Except End;

        PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
            PCBM_BoardRegisteration, c_NoEventData);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);
    Result := BuildSuccessResponse(RequestId,
        '{"success":true,"layer":"' + EscapeJsonString(LayerName) + '"}');
End;

{..............................................................................}
{ PCB_GetBoardOutline - Get board outline vertices                            }
{..............................................................................}

Function PCB_GetBoardOutline(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Outline : IPCB_BoardOutline;
    Seg : TPolySegment;
    BR : TCoordRect;
    JsonItems, SegKind : String;
    First : Boolean;
    I : Integer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    Outline := Board.BoardOutline;
    If Outline = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_OUTLINE', 'Board has no outline defined');
        Exit;
    End;

    Try
        Outline.Invalidate;
        Outline.Rebuild;
        Outline.Validate;
    Except
    End;

    // Bounding rectangle
    BR := Outline.BoundingRectangle;

    // Iterate vertices
    JsonItems := '';
    First := True;
    For I := 0 To Outline.PointCount - 1 Do
    Begin
        If Not First Then JsonItems := JsonItems + ',';
        First := False;

        If Outline.Segments[I].Kind = ePolySegmentLine Then
            SegKind := 'line'
        Else
            SegKind := 'arc';

        JsonItems := JsonItems + '{"index":' + IntToStr(I) + ','
            + '"kind":"' + SegKind + '",'
            + '"x":' + IntToStr(CoordToMils(Outline.Segments[I].vx)) + ','
            + '"y":' + IntToStr(CoordToMils(Outline.Segments[I].vy));

        If Outline.Segments[I].Kind <> ePolySegmentLine Then
        Begin
            JsonItems := JsonItems + ','
                + '"cx":' + IntToStr(CoordToMils(Outline.Segments[I].cx)) + ','
                + '"cy":' + IntToStr(CoordToMils(Outline.Segments[I].cy)) + ','
                + '"angle1":' + FloatToJsonStr(Outline.Segments[I].Angle1) + ','
                + '"angle2":' + FloatToJsonStr(Outline.Segments[I].Angle2);
        End;

        JsonItems := JsonItems + '}';
    End;

    Result := BuildSuccessResponse(RequestId,
        '{"point_count":' + IntToStr(Outline.PointCount) + ','
        + '"vertices":[' + JsonItems + '],'
        + '"bounding_rect":{"left":' + IntToStr(CoordToMils(BR.Left))
        + ',"bottom":' + IntToStr(CoordToMils(BR.Bottom))
        + ',"right":' + IntToStr(CoordToMils(BR.Right))
        + ',"top":' + IntToStr(CoordToMils(BR.Top)) + '}}');
End;

{..............................................................................}
{ PCB_GetSelectedObjects - Get properties of currently selected PCB objects   }
{..............................................................................}

Function PCB_GetSelectedObjects(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Obj : IPCB_Primitive;
    PropsStr, JsonItems, ObjTypeStr, NetName, LayerName : String;
    First : Boolean;
    I, Count : Integer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    PropsStr := ExtractJsonValue(Params, 'properties');
    If PropsStr = '' Then PropsStr := 'ObjectId,X,Y,Layer,Net';

    JsonItems := '';
    First := True;
    Count := Board.SelectecObjectCount;

    For I := 0 To Count - 1 Do
    Begin
        Obj := Board.SelectecObject[I];
        If Obj = Nil Then Continue;

        If Not First Then JsonItems := JsonItems + ',';
        First := False;

        // Build JSON using PCBGeneric helpers
        JsonItems := JsonItems + BuildObjectJsonPCB(Obj, PropsStr);
    End;

    Result := BuildSuccessResponse(RequestId,
        '{"objects":[' + JsonItems + '],"count":' + IntToStr(Count) + '}');
End;

{..............................................................................}
{ PCB_SetLayerVisibility - Show/hide specific layers                          }
{ Params: layer=<layer_name>, visible=<true|false>                           }
{..............................................................................}

Function PCB_SetLayerVisibility(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    LayerStr, VisibleStr : String;
    LayerID : TLayer;
    Visible : Boolean;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    LayerStr := ExtractJsonValue(Params, 'layer');
    VisibleStr := ExtractJsonValue(Params, 'visible');

    If LayerStr = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'Missing "layer" parameter');
        Exit;
    End;

    LayerID := GetLayerFromString(LayerStr);
    Visible := (LowerCase(VisibleStr) = 'true') Or (VisibleStr = '1');

    Board.LayerIsDisplayed[LayerID] := Visible;

    // Refresh the view
    // Board.ViewManager_FullUpdate;  // removed, expensive on large boards; Altium auto-refreshes on user interaction

    Result := BuildSuccessResponse(RequestId,
        '{"layer":"' + EscapeJsonString(LayerStr) + '",'
        + '"visible":' + BoolToJsonStr(Visible) + '}');
End;

{..............................................................................}
{ PCB_RepourPolygons - Repour all polygon pours via RunProcess                }
{..............................................................................}

Function PCB_RepourPolygons(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    ResetParameters;
    RunProcess('PCB:RepourAllPolygons');

    // Board.ViewManager_FullUpdate;  // removed, expensive on large boards; Altium auto-refreshes on user interaction

    Result := BuildSuccessResponse(RequestId,
        '{"repoured":true}');
End;

{..............................................................................}
{ PCB_PlaceVia - Place a via at specific coordinates on a net                 }
{ Params: x=<mils>, y=<mils>, net=<name>, size=<mils>, hole_size=<mils>,    }
{         low_layer=<layer>, high_layer=<layer>                              }
{..............................................................................}

Function PCB_PlaceVia(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Via : IPCB_Via;
    XStr, YStr, NetStr, SizeStr, HoleSizeStr, LowLayerStr, HighLayerStr : String;
    FoundNet : IPCB_Net;
    ViaX, ViaY, ViaSize, ViaHole : Integer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    XStr := ExtractJsonValue(Params, 'x');
    YStr := ExtractJsonValue(Params, 'y');
    NetStr := ExtractJsonValue(Params, 'net');
    SizeStr := ExtractJsonValue(Params, 'size');
    HoleSizeStr := ExtractJsonValue(Params, 'hole_size');
    LowLayerStr := ExtractJsonValue(Params, 'low_layer');
    HighLayerStr := ExtractJsonValue(Params, 'high_layer');

    If (XStr = '') Or (YStr = '') Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'Missing "x" and/or "y" parameters');
        Exit;
    End;

    ViaX := StrToIntDef(XStr, 0);
    ViaY := StrToIntDef(YStr, 0);
    ViaSize := StrToIntDef(SizeStr, 50);    // Default 50 mils pad size
    ViaHole := StrToIntDef(HoleSizeStr, 28); // Default 28 mils hole

    PCBServer.PreProcess;
    Try
        Via := PCBServer.PCBObjectFactory(eViaObject, eNoDimension, eCreate_Default);
        If Via = Nil Then
        Begin
            PCBServer.PostProcess;
            Result := BuildErrorResponse(RequestId, 'CREATE_FAILED', 'Failed to create via object');
            Exit;
        End;

        Via.x := MilsToCoord(ViaX);
        Via.y := MilsToCoord(ViaY);
        Via.Size := MilsToCoord(ViaSize);
        Via.HoleSize := MilsToCoord(ViaHole);

        // Set layers
        If LowLayerStr <> '' Then
            Via.LowLayer := GetLayerFromString(LowLayerStr)
        Else
            Via.LowLayer := eTopLayer;

        If HighLayerStr <> '' Then
            Via.HighLayer := GetLayerFromString(HighLayerStr)
        Else
            Via.HighLayer := eBottomLayer;

        // Assign net
        If NetStr <> '' Then
        Begin
            FoundNet := FindNetByName(Board, NetStr);
            If FoundNet <> Nil Then
                Via.Net := FoundNet;
        End;

        Board.AddPCBObject(Via);

        PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
            PCBM_BoardRegisteration, Via.I_ObjectAddress);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"placed":true,'
        + '"x":' + IntToStr(ViaX) + ','
        + '"y":' + IntToStr(ViaY) + ','
        + '"size":' + IntToStr(ViaSize) + ','
        + '"hole_size":' + IntToStr(ViaHole) + '}');
End;

{..............................................................................}
{ PCB_PlaceTrack - Place a track segment between two XY points               }
{ Params: x1, y1, x2, y2 (mils), width (mils), layer, net_name             }
{..............................................................................}

Function PCB_PlaceTrack(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Track : IPCB_Track;
    X1Str, Y1Str, X2Str, Y2Str, WidthStr, LayerStr, NetStr : String;
    FoundNet : IPCB_Net;
    TX1, TY1, TX2, TY2, TWidth : Integer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    X1Str := ExtractJsonValue(Params, 'x1');
    Y1Str := ExtractJsonValue(Params, 'y1');
    X2Str := ExtractJsonValue(Params, 'x2');
    Y2Str := ExtractJsonValue(Params, 'y2');
    WidthStr := ExtractJsonValue(Params, 'width');
    LayerStr := ExtractJsonValue(Params, 'layer');
    NetStr := ExtractJsonValue(Params, 'net_name');

    If (X1Str = '') Or (Y1Str = '') Or (X2Str = '') Or (Y2Str = '') Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'Missing coordinate parameters (x1, y1, x2, y2)');
        Exit;
    End;

    TX1 := StrToIntDef(X1Str, 0);
    TY1 := StrToIntDef(Y1Str, 0);
    TX2 := StrToIntDef(X2Str, 0);
    TY2 := StrToIntDef(Y2Str, 0);
    TWidth := StrToIntDef(WidthStr, 10);

    PCBServer.PreProcess;
    Try
        Track := PCBServer.PCBObjectFactory(eTrackObject, eNoDimension, eCreate_Default);
        If Track = Nil Then
        Begin
            PCBServer.PostProcess;
            Result := BuildErrorResponse(RequestId, 'CREATE_FAILED', 'Failed to create track object');
            Exit;
        End;

        Track.x1 := MilsToCoord(TX1);
        Track.y1 := MilsToCoord(TY1);
        Track.x2 := MilsToCoord(TX2);
        Track.y2 := MilsToCoord(TY2);
        Track.Width := MilsToCoord(TWidth);

        If LayerStr <> '' Then
            Track.Layer := GetLayerFromString(LayerStr)
        Else
            Track.Layer := eTopLayer;

        If NetStr <> '' Then
        Begin
            FoundNet := FindNetByName(Board, NetStr);
            If FoundNet <> Nil Then
                Track.Net := FoundNet;
        End;

        Board.AddPCBObject(Track);

        PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
            PCBM_BoardRegisteration, Track.I_ObjectAddress);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"placed":true,'
        + '"x1":' + IntToStr(TX1) + ','
        + '"y1":' + IntToStr(TY1) + ','
        + '"x2":' + IntToStr(TX2) + ','
        + '"y2":' + IntToStr(TY2) + ','
        + '"width":' + IntToStr(TWidth) + ','
        + '"layer":"' + EscapeJsonString(GetLayerString(Track.Layer)) + '"}');
End;

{..............................................................................}
{ PCB_PlaceTracks - Place many tracks in a single IPC round-trip.              }
{ Param 'tracks' is a pipe-separated list; each track is 7 comma-separated    }
{ fields: x1,y1,x2,y2,width,layer,net_name (width default 10, layer default   }
{ TopLayer, net optional). Wrapped in one PreProcess/PostProcess and one      }
{ save so N tracks cost ~1x the overhead of placing one.                       }
{..............................................................................}

Function PCB_PlaceTracks(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Track : IPCB_Track;
    TracksStr, TrackStr, Remaining, Field : String;
    PipePos, CommaPos, Placed, Failed, FieldIdx : Integer;
    TX1, TY1, TX2, TY2, TWidth : Integer;
    LayerStr, NetStr : String;
    FoundNet : IPCB_Net;
    { 7 named locals instead of `Array[0..6] Of String` - fixed-size       }
    { string arrays as function locals corrupt the function return slot   }
    { in DelphiScript, see [[delphiscript_fixed_string_array_bug]].       }
    F0, F1, F2, F3, F4, F5, F6, Token : String;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    TracksStr := ExtractJsonValue(Params, 'tracks');
    If TracksStr = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'tracks parameter required');
        Exit;
    End;

    Placed := 0;
    Failed := 0;
    Remaining := TracksStr;

    PCBServer.PreProcess;
    Try
        While Length(Remaining) > 0 Do
        Begin
            PipePos := Pos('|', Remaining);
            If PipePos = 0 Then
            Begin
                TrackStr := Remaining;
                Remaining := '';
            End
            Else
            Begin
                TrackStr := Copy(Remaining, 1, PipePos - 1);
                Remaining := Copy(Remaining, PipePos + 1, Length(Remaining));
            End;

            If TrackStr = '' Then Continue;

            F0 := '';
            F1 := '';
            F2 := '';
            F3 := '';
            F4 := '';
            F5 := '';
            F6 := '';
            FieldIdx := 0;
            While (TrackStr <> '') And (FieldIdx <= 6) Do
            Begin
                CommaPos := Pos(',', TrackStr);
                If CommaPos = 0 Then
                Begin
                    Token := TrackStr;
                    TrackStr := '';
                End
                Else
                Begin
                    Token := Copy(TrackStr, 1, CommaPos - 1);
                    TrackStr := Copy(TrackStr, CommaPos + 1, Length(TrackStr));
                End;
                Case FieldIdx Of
                    0: F0 := Token;
                    1: F1 := Token;
                    2: F2 := Token;
                    3: F3 := Token;
                    4: F4 := Token;
                    5: F5 := Token;
                    6: F6 := Token;
                End;
                Inc(FieldIdx);
            End;

            TX1 := StrToIntDef(F0, 0);
            TY1 := StrToIntDef(F1, 0);
            TX2 := StrToIntDef(F2, 0);
            TY2 := StrToIntDef(F3, 0);
            TWidth := StrToIntDef(F4, 10);
            LayerStr := F5;
            NetStr := F6;

            Track := PCBServer.PCBObjectFactory(eTrackObject, eNoDimension, eCreate_Default);
            If Track = Nil Then
            Begin
                Inc(Failed);
                Continue;
            End;

            Track.x1 := MilsToCoord(TX1);
            Track.y1 := MilsToCoord(TY1);
            Track.x2 := MilsToCoord(TX2);
            Track.y2 := MilsToCoord(TY2);
            Track.Width := MilsToCoord(TWidth);

            If LayerStr <> '' Then
                Track.Layer := GetLayerFromString(LayerStr)
            Else
                Track.Layer := eTopLayer;

            If NetStr <> '' Then
            Begin
                FoundNet := FindNetByName(Board, NetStr);
                If FoundNet <> Nil Then Track.Net := FoundNet;
            End;

            Board.AddPCBObject(Track);
            Inc(Placed);
        End;
        { Broadcast ONCE at the end of the batch instead of once per track.   }
        { A single BoardRegisteration on the board object (null child) is     }
        { enough to kick the connectivity/rules engines to refresh the whole  }
        { board, much cheaper than N individual broadcasts.                   }
        PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
            PCBM_BoardRegisteration, c_NoEventData);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"placed":' + IntToStr(Placed) + ','
        + '"failed":' + IntToStr(Failed) + '}');
End;

{..............................................................................}
{ PCB_PlaceArc - Place an arc on the PCB                                      }
{ Params: x_center, y_center, radius, start_angle, end_angle, width, layer   }
{..............................................................................}

Function PCB_PlaceArc(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Arc : IPCB_Arc;
    XCStr, YCStr, RadStr, SAStr, EAStr, WidthStr, LayerStr : String;
    ArcXC, ArcYC, ArcRad, ArcWidth : Integer;
    ArcSA, ArcEA : Double;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    XCStr := ExtractJsonValue(Params, 'x_center');
    YCStr := ExtractJsonValue(Params, 'y_center');
    RadStr := ExtractJsonValue(Params, 'radius');
    SAStr := ExtractJsonValue(Params, 'start_angle');
    EAStr := ExtractJsonValue(Params, 'end_angle');
    WidthStr := ExtractJsonValue(Params, 'width');
    LayerStr := ExtractJsonValue(Params, 'layer');

    If (XCStr = '') Or (YCStr = '') Or (RadStr = '') Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'Missing required parameters (x_center, y_center, radius)');
        Exit;
    End;

    ArcXC := StrToIntDef(XCStr, 0);
    ArcYC := StrToIntDef(YCStr, 0);
    ArcRad := StrToIntDef(RadStr, 100);
    ArcSA := StrToFloatDef(SAStr, 0);
    ArcEA := StrToFloatDef(EAStr, 360);
    ArcWidth := StrToIntDef(WidthStr, 10);

    PCBServer.PreProcess;
    Try
        Arc := PCBServer.PCBObjectFactory(eArcObject, eNoDimension, eCreate_Default);
        If Arc = Nil Then
        Begin
            PCBServer.PostProcess;
            Result := BuildErrorResponse(RequestId, 'CREATE_FAILED', 'Failed to create arc object');
            Exit;
        End;

        Arc.XCenter := MilsToCoord(ArcXC);
        Arc.YCenter := MilsToCoord(ArcYC);
        Arc.Radius := MilsToCoord(ArcRad);
        Arc.StartAngle := ArcSA;
        Arc.EndAngle := ArcEA;
        Arc.LineWidth := MilsToCoord(ArcWidth);

        If LayerStr <> '' Then
            Arc.Layer := GetLayerFromString(LayerStr)
        Else
            Arc.Layer := eTopLayer;

        Board.AddPCBObject(Arc);

        PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
            PCBM_BoardRegisteration, Arc.I_ObjectAddress);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"placed":true,'
        + '"x_center":' + IntToStr(ArcXC) + ','
        + '"y_center":' + IntToStr(ArcYC) + ','
        + '"radius":' + IntToStr(ArcRad) + ','
        + '"start_angle":' + FloatToJsonStr(ArcSA) + ','
        + '"end_angle":' + FloatToJsonStr(ArcEA) + ','
        + '"width":' + IntToStr(ArcWidth) + ','
        + '"layer":"' + EscapeJsonString(GetLayerString(Arc.Layer)) + '"}');
End;

{..............................................................................}
{ PCB_PlaceText - Place text string on the PCB                                }
{ Params: text, x, y (mils), layer, height (mils), rotation (deg)           }
{..............................................................................}

Function PCB_PlaceText(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    TextObj : IPCB_Text;
    TextStr, XStr, YStr, LayerStr, HeightStr, RotStr : String;
    TX, TY, THeight : Integer;
    TRot : Double;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    TextStr := ExtractJsonValue(Params, 'text');
    XStr := ExtractJsonValue(Params, 'x');
    YStr := ExtractJsonValue(Params, 'y');
    LayerStr := ExtractJsonValue(Params, 'layer');
    HeightStr := ExtractJsonValue(Params, 'height');
    RotStr := ExtractJsonValue(Params, 'rotation');

    If TextStr = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'Missing "text" parameter');
        Exit;
    End;

    If (XStr = '') Or (YStr = '') Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'Missing "x" and/or "y" parameters');
        Exit;
    End;

    TX := StrToIntDef(XStr, 0);
    TY := StrToIntDef(YStr, 0);
    THeight := StrToIntDef(HeightStr, 60);
    TRot := StrToFloatDef(RotStr, 0);

    PCBServer.PreProcess;
    Try
        TextObj := PCBServer.PCBObjectFactory(eTextObject, eNoDimension, eCreate_Default);
        If TextObj = Nil Then
        Begin
            PCBServer.PostProcess;
            Result := BuildErrorResponse(RequestId, 'CREATE_FAILED', 'Failed to create text object');
            Exit;
        End;

        TextObj.XLocation := MilsToCoord(TX);
        TextObj.YLocation := MilsToCoord(TY);
        TextObj.Text := TextStr;
        TextObj.Size := MilsToCoord(THeight);
        TextObj.Rotation := TRot;

        If LayerStr <> '' Then
            TextObj.Layer := GetLayerFromString(LayerStr)
        Else
            TextObj.Layer := eTopOverlay;

        Board.AddPCBObject(TextObj);

        PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
            PCBM_BoardRegisteration, TextObj.I_ObjectAddress);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"placed":true,'
        + '"text":"' + EscapeJsonString(TextStr) + '",'
        + '"x":' + IntToStr(TX) + ','
        + '"y":' + IntToStr(TY) + ','
        + '"height":' + IntToStr(THeight) + ','
        + '"rotation":' + FloatToJsonStr(TRot) + ','
        + '"layer":"' + EscapeJsonString(GetLayerString(TextObj.Layer)) + '"}');
End;

{..............................................................................}
{ PCB_PlaceFill - Place a copper fill rectangle                               }
{ Params: x1, y1, x2, y2 (mils), layer, net_name                           }
{..............................................................................}

Function PCB_PlaceFill(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Fill : IPCB_Fill;
    X1Str, Y1Str, X2Str, Y2Str, LayerStr, NetStr : String;
    FoundNet : IPCB_Net;
    FX1, FY1, FX2, FY2 : Integer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    X1Str := ExtractJsonValue(Params, 'x1');
    Y1Str := ExtractJsonValue(Params, 'y1');
    X2Str := ExtractJsonValue(Params, 'x2');
    Y2Str := ExtractJsonValue(Params, 'y2');
    LayerStr := ExtractJsonValue(Params, 'layer');
    NetStr := ExtractJsonValue(Params, 'net_name');

    If (X1Str = '') Or (Y1Str = '') Or (X2Str = '') Or (Y2Str = '') Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'Missing coordinate parameters (x1, y1, x2, y2)');
        Exit;
    End;

    FX1 := StrToIntDef(X1Str, 0);
    FY1 := StrToIntDef(Y1Str, 0);
    FX2 := StrToIntDef(X2Str, 0);
    FY2 := StrToIntDef(Y2Str, 0);

    PCBServer.PreProcess;
    Try
        Fill := PCBServer.PCBObjectFactory(eFillObject, eNoDimension, eCreate_Default);
        If Fill = Nil Then
        Begin
            PCBServer.PostProcess;
            Result := BuildErrorResponse(RequestId, 'CREATE_FAILED', 'Failed to create fill object');
            Exit;
        End;

        Fill.X1Location := MilsToCoord(FX1);
        Fill.Y1Location := MilsToCoord(FY1);
        Fill.X2Location := MilsToCoord(FX2);
        Fill.Y2Location := MilsToCoord(FY2);
        Fill.Rotation := 0;

        If LayerStr <> '' Then
            Fill.Layer := GetLayerFromString(LayerStr)
        Else
            Fill.Layer := eTopLayer;

        If NetStr <> '' Then
        Begin
            FoundNet := FindNetByName(Board, NetStr);
            If FoundNet <> Nil Then
                Fill.Net := FoundNet;
        End;

        Board.AddPCBObject(Fill);

        PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
            PCBM_BoardRegisteration, Fill.I_ObjectAddress);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"placed":true,'
        + '"x1":' + IntToStr(FX1) + ','
        + '"y1":' + IntToStr(FY1) + ','
        + '"x2":' + IntToStr(FX2) + ','
        + '"y2":' + IntToStr(FY2) + ','
        + '"layer":"' + EscapeJsonString(GetLayerString(Fill.Layer)) + '"}');
End;

{..............................................................................}
{ PCB_StartPolygonPlacement - Launches Altium's interactive polygon tool      }
{ Requires user to draw the polygon boundary in Altium afterward              }
{ Params: layer, net_name                                                    }
{..............................................................................}

Function PCB_StartPolygonPlacement(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    LayerStr, NetStr : String;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    LayerStr := ExtractJsonValue(Params, 'layer');
    NetStr := ExtractJsonValue(Params, 'net_name');

    ResetParameters;
    If LayerStr <> '' Then
        AddStringParameter('Layer', LayerStr);
    If NetStr <> '' Then
        AddStringParameter('Net', NetStr);
    RunProcess('PCB:PlacePolygonPlane');

    // Board.ViewManager_FullUpdate;  // removed, expensive on large boards; Altium auto-refreshes on user interaction

    Result := BuildSuccessResponse(RequestId,
        '{"interactive_tool_launched":true,'
        + '"layer":"' + EscapeJsonString(LayerStr) + '",'
        + '"net_name":"' + EscapeJsonString(NetStr) + '",'
        + '"note":"Interactive polygon placement tool launched. Requires user to draw the polygon boundary in Altium Designer, no polygon is created by this call."}');
End;

{..............................................................................}
{ PCB_CreateDesignRule - Create a new design rule                             }
{ Params: rule_type (clearance/width/via_size), name, value (mils),          }
{         scope (query expression for Scope1)                                }
{..............................................................................}

Function PCB_CreateDesignRule(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Rule : IPCB_Rule;
    RuleClear : IPCB_ClearanceConstraint;
    RuleWidth : IPCB_MaxMinWidthConstraint;
    RuleHole : IPCB_MaxMinHoleSizeConstraint;
    RuleDiff : IPCB_DifferentialPairsRoutingRule;
    RuleTypeStr, RuleName, ValueStr, MaxValueStr, FavoredValueStr : String;
    ScopeStr, NetScopeStr, MaxUncoupStr : String;
    RuleValue, MaxValue, FavoredValue, MaxUncoupVal, NetScopeVal : Integer;
    HasMaxValue : Boolean;
    L : TLayer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    RuleTypeStr := ExtractJsonValue(Params, 'rule_type');
    RuleName := ExtractJsonValue(Params, 'name');
    ValueStr := ExtractJsonValue(Params, 'value');
    MaxValueStr := ExtractJsonValue(Params, 'max_value');
    FavoredValueStr := ExtractJsonValue(Params, 'favored_value');
    MaxUncoupStr := ExtractJsonValue(Params, 'max_uncoupled_length');
    ScopeStr := ExtractJsonValue(Params, 'scope');
    NetScopeStr := LowerCase(ExtractJsonValue(Params, 'net_scope'));

    If RuleName = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'Missing "name" parameter');
        Exit;
    End;

    If RuleTypeStr = '' Then
        RuleTypeStr := 'clearance';

    { Map textual net_scope to the enum used by the rule object.                  }
    { Blank / "any_net" keeps prior default behavior. For clearance rules          }
    { "different_nets" is the normal setting, same-net tracks touching pads      }
    { of their own net should NOT count as a clearance violation.                  }
    If NetScopeStr = 'different_nets' Then
        NetScopeVal := eNetScope_DifferentNetsOnly
    Else If NetScopeStr = 'same_net' Then
        NetScopeVal := eNetScope_SameNetOnly
    Else
        NetScopeVal := eNetScope_AnyNet;

    RuleValue := StrToIntDef(ValueStr, 10);

    { Independent max / favored values: when omitted, fall back to the legacy }
    { 5x-min default for width/via_size so existing callers keep working. The }
    { previous handler forced max = value * 5 unconditionally, which silently }
    { clamped any Width rule's max to 25 mil when value was 5 mil and broke   }
    { wider power-trace use cases.                                              }
    HasMaxValue := MaxValueStr <> '';
    MaxValue := StrToIntDef(MaxValueStr, RuleValue * 5);
    FavoredValue := StrToIntDef(FavoredValueStr, RuleValue);
    MaxUncoupVal := StrToIntDef(MaxUncoupStr, 1000);

    { Constraint values are NOT properties of the base IPCB_Rule interface,    }
    { they live on the per-kind subtypes (IPCB_ClearanceConstraint,            }
    { IPCB_MaxMinWidthConstraint, IPCB_MaxMinHoleSizeConstraint, ...). DelphiScript  }
    { needs the variable typed as the actual subtype to expose constraint      }
    { setters; assigning to a base IPCB_Rule var and then writing Rule.Gap     }
    { fails with "Undeclared identifier" on builds where IPCB_Rule does not    }
    { surface the union of constraint properties (e.g. AD 26.5+).              }
    { Indexed properties also use function-call form: RuleWidth.MinWidth(L)    }
    { not RuleWidth.MinWidth[L], bracket form is rejected in DelphiScript.    }
    Rule := Nil;
    PCBServer.PreProcess;
    Try
        If RuleTypeStr = 'clearance' Then
        Begin
            RuleClear := PCBServer.PCBRuleFactory(eRule_Clearance);
            RuleClear.Name := RuleName;
            RuleClear.NetScope := NetScopeVal;
            RuleClear.LayerKind := eRuleLayerKind_SameLayer;
            RuleClear.Gap := MilsToCoord(RuleValue);
            If ScopeStr <> '' Then
                RuleClear.Scope1Expression := ScopeStr;
            Rule := RuleClear;
        End
        Else If RuleTypeStr = 'width' Then
        Begin
            RuleWidth := PCBServer.PCBRuleFactory(eRule_MaxMinWidth);
            RuleWidth.Name := RuleName;
            RuleWidth.NetScope := NetScopeVal;
            RuleWidth.LayerKind := eRuleLayerKind_SameLayer;
            For L := MinLayer To MaxLayer Do
            Begin
                RuleWidth.MinWidth(L) := MilsToCoord(RuleValue);
                RuleWidth.MaxWidth(L) := MilsToCoord(MaxValue);
                RuleWidth.FavoredWidth(L) := MilsToCoord(FavoredValue);
            End;
            If ScopeStr <> '' Then
                RuleWidth.Scope1Expression := ScopeStr;
            Rule := RuleWidth;
        End
        Else If RuleTypeStr = 'via_size' Then
        Begin
            RuleHole := PCBServer.PCBRuleFactory(eRule_MaxMinHoleSize);
            RuleHole.Name := RuleName;
            RuleHole.NetScope := NetScopeVal;
            RuleHole.LayerKind := eRuleLayerKind_SameLayer;
            RuleHole.MinLimit := MilsToCoord(RuleValue);
            RuleHole.MaxLimit := MilsToCoord(MaxValue);
            If ScopeStr <> '' Then
                RuleHole.Scope1Expression := ScopeStr;
            Rule := RuleHole;
        End
        Else If RuleTypeStr = 'differential_pairs' Then
        Begin
            { IPCB_DifferentialPairsRoutingRule exposes MinGap / MaxGap /     }
            { PreferedGap (note SDK spelling: one 'r') as layer-indexed      }
            { properties, and MaxUncoupledLength as a single scalar. value ->}
            { MinGap, max_value -> MaxGap, favored_value -> PreferedGap. The }
            { width constraints shown in the rule's descriptor come from a   }
            { separate Width rule scoped to the diff pair, not from this    }
            { interface; create that separately with rule_type='width' if   }
            { needed.                                                          }
            RuleDiff := PCBServer.PCBRuleFactory(eRule_DifferentialPairsRouting);
            RuleDiff.Name := RuleName;
            RuleDiff.NetScope := NetScopeVal;
            RuleDiff.LayerKind := eRuleLayerKind_SameLayer;
            For L := MinLayer To MaxLayer Do
            Begin
                RuleDiff.MinGap(L) := MilsToCoord(RuleValue);
                RuleDiff.MaxGap(L) := MilsToCoord(MaxValue);
                RuleDiff.PreferedGap(L) := MilsToCoord(FavoredValue);
            End;
            RuleDiff.MaxUncoupledLength := MilsToCoord(MaxUncoupVal);
            If ScopeStr <> '' Then
                RuleDiff.Scope1Expression := ScopeStr;
            Rule := RuleDiff;
        End
        Else
        Begin
            PCBServer.PostProcess;
            Result := BuildErrorResponse(RequestId, 'INVALID_PARAM',
                'Unknown rule_type: ' + RuleTypeStr + '. Use clearance, width, via_size, or differential_pairs');
            Exit;
        End;

        Rule.Enabled := True;
        Board.AddPCBObject(Rule);
        PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
            PCBM_BoardRegisteration, Rule.I_ObjectAddress);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"created":true,'
        + '"name":"' + EscapeJsonString(RuleName) + '",'
        + '"rule_type":"' + EscapeJsonString(RuleTypeStr) + '",'
        + '"value_mils":' + IntToStr(RuleValue) + '}');
End;

{..............................................................................}
{ PCB_DeleteDesignRule - Delete a design rule by name                         }
{ Params: name=<rule_name>                                                   }
{..............................................................................}

Function PCB_DeleteDesignRule(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iterator : IPCB_BoardIterator;
    Rule : IPCB_Rule;
    RuleName : String;
    Found : Boolean;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    RuleName := ExtractJsonValue(Params, 'name');
    If RuleName = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'Missing "name" parameter');
        Exit;
    End;

    // Find the rule by name
    Found := False;
    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(eRuleObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);

    Rule := Iterator.FirstPCBObject;
    While Rule <> Nil Do
    Begin
        If Rule.Name = RuleName Then
        Begin
            Found := True;
            Break;
        End;
        Rule := Iterator.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(Iterator);

    If Not Found Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NOT_FOUND', 'Design rule not found: ' + RuleName);
        Exit;
    End;

    PCBServer.PreProcess;
    Try
        PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
            PCBM_BoardRegisteration, Rule.I_ObjectAddress);
        Board.RemovePCBObject(Rule);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"deleted":true,"name":"' + EscapeJsonString(RuleName) + '"}');
End;

{..............................................................................}
{ PCB_GetComponentPads - Get all pads of a specific component                 }
{ Params: designator=<ref>                                                   }
{..............................................................................}

Function PCB_GetComponentPads(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Comp : IPCB_Component;
    GrpIter : IPCB_GroupIterator;
    Pad : IPCB_Pad;
    DesStr, JsonItems, PadName, NetName, LayerStr, ShapeStr : String;
    First : Boolean;
    Count : Integer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    DesStr := ExtractJsonValue(Params, 'designator');
    If DesStr = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'Missing "designator" parameter');
        Exit;
    End;

    Comp := Board.GetPcbComponentByRefDes(DesStr);
    If Comp = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NOT_FOUND', 'Component not found: ' + DesStr);
        Exit;
    End;

    JsonItems := '';
    First := True;
    Count := 0;

    GrpIter := Comp.GroupIterator_Create;
    GrpIter.AddFilter_ObjectSet(MkSet(ePadObject));

    Pad := GrpIter.FirstPCBObject;
    While Pad <> Nil Do
    Begin
        If Not First Then JsonItems := JsonItems + ',';
        First := False;

        Try PadName := Pad.Name; Except PadName := ''; End;
        Try
            If Pad.Net <> Nil Then NetName := Pad.Net.Name
            Else NetName := '';
        Except NetName := ''; End;
        Try LayerStr := GetLayerString(Pad.Layer); Except LayerStr := 'Unknown'; End;

        JsonItems := JsonItems + '{"name":"' + EscapeJsonString(PadName) + '",'
            + '"x":' + IntToStr(CoordToMils(Pad.x)) + ','
            + '"y":' + IntToStr(CoordToMils(Pad.y)) + ','
            + '"net":"' + EscapeJsonString(NetName) + '",'
            + '"layer":"' + EscapeJsonString(LayerStr) + '",'
            + '"hole_size":' + IntToStr(CoordToMils(Pad.HoleSize)) + ','
            + '"top_x_size":' + IntToStr(CoordToMils(Pad.TopXSize)) + ','
            + '"top_y_size":' + IntToStr(CoordToMils(Pad.TopYSize)) + ','
            + '"rotation":' + FloatToJsonStr(Pad.Rotation) + '}';
        Inc(Count);
        Pad := GrpIter.NextPCBObject;
    End;
    Comp.GroupIterator_Destroy(GrpIter);

    Result := BuildSuccessResponse(RequestId,
        '{"designator":"' + EscapeJsonString(DesStr) + '",'
        + '"pads":[' + JsonItems + '],"pad_count":' + IntToStr(Count) + '}');
End;

{..............................................................................}
{ PCB_FlipComponent - Flip a component to the other side (top<->bottom)      }
{ Params: designator=<ref>                                                   }
{..............................................................................}

Function PCB_FlipComponent(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Comp : IPCB_Component;
    DesStr, OldLayer, NewLayer : String;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    DesStr := ExtractJsonValue(Params, 'designator');
    If DesStr = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'Missing "designator" parameter');
        Exit;
    End;

    Comp := Board.GetPcbComponentByRefDes(DesStr);
    If Comp = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NOT_FOUND', 'Component not found: ' + DesStr);
        Exit;
    End;

    Try OldLayer := GetLayerString(Comp.Layer); Except OldLayer := 'Unknown'; End;

    PCBServer.PreProcess;
    Try
        PCBServer.SendMessageToRobots(Comp.I_ObjectAddress, c_Broadcast,
            PCBM_BeginModify, c_NoEventData);

        // Flip the component to the opposite side of the board
        If Comp.Layer = eTopLayer Then
            Comp.Layer := eBottomLayer
        Else
            Comp.Layer := eTopLayer;

        PCBServer.SendMessageToRobots(Comp.I_ObjectAddress, c_Broadcast,
            PCBM_EndModify, c_NoEventData);
    Finally
        PCBServer.PostProcess;
    End;

    Try NewLayer := GetLayerString(Comp.Layer); Except NewLayer := 'Unknown'; End;
    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"designator":"' + EscapeJsonString(DesStr) + '",'
        + '"old_layer":"' + EscapeJsonString(OldLayer) + '",'
        + '"new_layer":"' + EscapeJsonString(NewLayer) + '"}');
End;

{..............................................................................}
{ PCB_AlignComponents - Align specified components                            }
{ Params: designators=<comma-separated>, alignment=<left/right/top/bottom/  }
{         center_x/center_y>                                                 }
{..............................................................................}

Function PCB_AlignComponents(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    DesStr, AlignStr, Remaining, OneDesig : String;
    Comp : IPCB_Component;
    CommaPos, I, CX, CY : Integer;
    { Heap-allocated list of resolved designators. The original code held }
    { IPCB_Component pointers in `Array[0..99] Of IPCB_Component`, which  }
    { is a fixed-size local array of a managed type - that triggers the   }
    { return-slot corruption documented in                                 }
    { [[delphiscript_fixed_string_array_bug]]. The fix walks twice:        }
    { first pass to validate + measure bounds, second pass to apply.      }
    Resolved : TStringList;
    MinX, MaxX, MinY, MaxY, CenterX, CenterY : Integer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    DesStr := ExtractJsonValue(Params, 'designators');
    AlignStr := ExtractJsonValue(Params, 'alignment');

    If DesStr = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'Missing "designators" parameter');
        Exit;
    End;

    If AlignStr = '' Then AlignStr := 'left';

    Resolved := TStringList.Create;
    Try
        Remaining := DesStr;
        While Remaining <> '' Do
        Begin
            CommaPos := Pos(',', Remaining);
            If CommaPos > 0 Then
            Begin
                OneDesig := Copy(Remaining, 1, CommaPos - 1);
                Remaining := Copy(Remaining, CommaPos + 1, Length(Remaining));
            End
            Else
            Begin
                OneDesig := Remaining;
                Remaining := '';
            End;
            If OneDesig <> '' Then
            Begin
                Comp := Board.GetPcbComponentByRefDes(OneDesig);
                If Comp <> Nil Then Resolved.Add(OneDesig);
            End;
        End;

        If Resolved.Count < 2 Then
        Begin
            Result := BuildErrorResponse(RequestId, 'INSUFFICIENT',
                'Need at least 2 valid components to align');
            Exit;
        End;

        { First pass: bounding extents. }
        Comp := Board.GetPcbComponentByRefDes(Resolved[0]);
        MinX := CoordToMils(Comp.x);
        MaxX := MinX;
        MinY := CoordToMils(Comp.y);
        MaxY := MinY;
        For I := 1 To Resolved.Count - 1 Do
        Begin
            Comp := Board.GetPcbComponentByRefDes(Resolved[I]);
            If Comp = Nil Then Continue;
            CX := CoordToMils(Comp.x);
            CY := CoordToMils(Comp.y);
            If CX < MinX Then MinX := CX;
            If CX > MaxX Then MaxX := CX;
            If CY < MinY Then MinY := CY;
            If CY > MaxY Then MaxY := CY;
        End;
        CenterX := (MinX + MaxX) Div 2;
        CenterY := (MinY + MaxY) Div 2;

        { Second pass: apply alignment. }
        PCBServer.PreProcess;
        Try
            For I := 0 To Resolved.Count - 1 Do
            Begin
                Comp := Board.GetPcbComponentByRefDes(Resolved[I]);
                If Comp = Nil Then Continue;
                PCBServer.SendMessageToRobots(Comp.I_ObjectAddress, c_Broadcast,
                    PCBM_BeginModify, c_NoEventData);

                If AlignStr = 'left' Then
                    Comp.x := MilsToCoord(MinX)
                Else If AlignStr = 'right' Then
                    Comp.x := MilsToCoord(MaxX)
                Else If AlignStr = 'top' Then
                    Comp.y := MilsToCoord(MaxY)
                Else If AlignStr = 'bottom' Then
                    Comp.y := MilsToCoord(MinY)
                Else If AlignStr = 'center_x' Then
                    Comp.x := MilsToCoord(CenterX)
                Else If AlignStr = 'center_y' Then
                    Comp.y := MilsToCoord(CenterY);

                PCBServer.SendMessageToRobots(Comp.I_ObjectAddress, c_Broadcast,
                    PCBM_EndModify, c_NoEventData);
            End;
        Finally
            PCBServer.PostProcess;
        End;

        SaveDocByPath(Board.FileName);

        Result := BuildSuccessResponse(RequestId,
            '{"aligned":true,'
            + '"alignment":"' + EscapeJsonString(AlignStr) + '",'
            + '"component_count":' + IntToStr(Resolved.Count) + '}');
    Finally
        Resolved.Free;
    End;
End;

{..............................................................................}
{ PCB_GetClearanceViolations - Get clearance violations for a net             }
{ Params: net (optional) - if specified, only show violations for this net   }
{..............................................................................}

Function PCB_GetClearanceViolations(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iterator : IPCB_BoardIterator;
    Violation : IPCB_Violation;
    FilterNet, ViolDesc, ViolName : String;
    JsonItems : String;
    First : Boolean;
    Count : Integer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    FilterNet := ExtractJsonValue(Params, 'net');

    // First run DRC to refresh violations -- correct documented process
    // is PCB:DesignRuleCheck per TR0124, not PCB:RunDRC.
    ResetParameters;
    RunProcess('PCB:DesignRuleCheck');

    JsonItems := '';
    First := True;
    Count := 0;

    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(eViolationObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);

    Violation := Iterator.FirstPCBObject;
    While Violation <> Nil Do
    Begin
        Try ViolDesc := Violation.Description; Except ViolDesc := ''; End;
        Try ViolName := Violation.Name; Except ViolName := ''; End;

        // Filter by net if specified (check if net name appears in description)
        If (FilterNet = '') Or (Pos(FilterNet, ViolDesc) > 0) Or (Pos(FilterNet, ViolName) > 0) Then
        Begin
            If Count < 200 Then
            Begin
                If Not First Then JsonItems := JsonItems + ',';
                First := False;
                JsonItems := JsonItems + BuildViolationJson(Violation);
            End;
            Inc(Count);
        End;
        Violation := Iterator.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(Iterator);

    Result := BuildSuccessResponse(RequestId,
        '{"violation_count":' + IntToStr(Count) + ','
        + '"violations":[' + JsonItems + ']}');
End;

{..............................................................................}
{ PCB_SnapToGrid - Snap a component to the nearest grid point                }
{ Params: designator=<ref>, grid_size=<mils>                                }
{..............................................................................}

Function PCB_SnapToGrid(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Comp : IPCB_Component;
    DesStr, GridStr : String;
    GridSize, OldX, OldY, NewX, NewY : Integer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    DesStr := ExtractJsonValue(Params, 'designator');
    GridStr := ExtractJsonValue(Params, 'grid_size');

    If DesStr = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'Missing "designator" parameter');
        Exit;
    End;

    GridSize := StrToIntDef(GridStr, 50);
    If GridSize <= 0 Then GridSize := 50;

    Comp := Board.GetPcbComponentByRefDes(DesStr);
    If Comp = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NOT_FOUND', 'Component not found: ' + DesStr);
        Exit;
    End;

    OldX := CoordToMils(Comp.x);
    OldY := CoordToMils(Comp.y);

    // Snap to nearest grid point using rounding
    NewX := Round(OldX / GridSize) * GridSize;
    NewY := Round(OldY / GridSize) * GridSize;

    PCBServer.PreProcess;
    Try
        PCBServer.SendMessageToRobots(Comp.I_ObjectAddress, c_Broadcast,
            PCBM_BeginModify, c_NoEventData);

        Comp.x := MilsToCoord(NewX);
        Comp.y := MilsToCoord(NewY);

        PCBServer.SendMessageToRobots(Comp.I_ObjectAddress, c_Broadcast,
            PCBM_EndModify, c_NoEventData);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"designator":"' + EscapeJsonString(DesStr) + '",'
        + '"old_x":' + IntToStr(OldX) + ','
        + '"old_y":' + IntToStr(OldY) + ','
        + '"new_x":' + IntToStr(NewX) + ','
        + '"new_y":' + IntToStr(NewY) + ','
        + '"grid_size":' + IntToStr(GridSize) + '}');
End;

{..............................................................................}
{ PCB_GetDiffPairRules - Get all differential pair routing rules              }
{ Returns design rules (not pair objects) of kind eRule_DifferentialPairsRouting }
{..............................................................................}

Function PCB_GetDiffPairRules(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iterator : IPCB_BoardIterator;
    Rule : IPCB_Rule;
    JsonItems : String;
    First : Boolean;
    Count : Integer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    JsonItems := '';
    First := True;
    Count := 0;

    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(eRuleObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);

    Rule := Iterator.FirstPCBObject;
    While Rule <> Nil Do
    Begin
        If Rule.RuleKind = eRule_DifferentialPairsRouting Then
        Begin
            If Not First Then JsonItems := JsonItems + ',';
            First := False;
            JsonItems := JsonItems + '{"name":"' + EscapeJsonString(Rule.Name) + '",'
                + '"enabled":' + BoolToJsonStr(Rule.Enabled) + ','
                + '"scope_1":"' + EscapeJsonString(Rule.Scope1Expression) + '",'
                + '"scope_2":"' + EscapeJsonString(Rule.Scope2Expression) + '",'
                + '"comment":"' + EscapeJsonString(Rule.Comment) + '",'
                + '"descriptor":"' + EscapeJsonString(Rule.Descriptor) + '"}';
            Inc(Count);
        End;
        Rule := Iterator.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(Iterator);

    Result := BuildSuccessResponse(RequestId,
        '{"diff_pair_rules":[' + JsonItems + '],"count":' + IntToStr(Count) + '}');
End;

{..............................................................................}
{ PCB_GetVias - Get all vias on the board with position, size, net, layers   }
{..............................................................................}

Function PCB_GetVias(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iterator : IPCB_BoardIterator;
    Via : IPCB_Via;
    JsonItems, NetName : String;
    First : Boolean;
    Count : Integer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    JsonItems := '';
    First := True;
    Count := 0;

    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(eViaObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);

    Via := Iterator.FirstPCBObject;
    While Via <> Nil Do
    Begin
        If Not First Then JsonItems := JsonItems + ',';
        First := False;

        Try
            If Via.Net <> Nil Then NetName := Via.Net.Name
            Else NetName := '';
        Except NetName := ''; End;

        JsonItems := JsonItems + '{"x":' + IntToStr(CoordToMils(Via.x)) + ','
            + '"y":' + IntToStr(CoordToMils(Via.y)) + ','
            + '"size":' + IntToStr(CoordToMils(Via.Size)) + ','
            + '"hole_size":' + IntToStr(CoordToMils(Via.HoleSize)) + ','
            + '"net":"' + EscapeJsonString(NetName) + '",'
            + '"low_layer":"' + EscapeJsonString(GetLayerString(Via.LowLayer)) + '",'
            + '"high_layer":"' + EscapeJsonString(GetLayerString(Via.HighLayer)) + '"}';
        Inc(Count);
        Via := Iterator.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(Iterator);

    Result := BuildSuccessResponse(RequestId,
        '{"vias":[' + JsonItems + '],"count":' + IntToStr(Count) + '}');
End;

{..............................................................................}
{ PCB_DeleteObject - Delete a PCB object at specific coordinates on a layer  }
{ Params: x, y (mils), layer, object_type (track/via/fill/text)             }
{..............................................................................}

Function PCB_DeleteObject(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iterator : IPCB_BoardIterator;
    Obj : IPCB_Primitive;
    XStr, YStr, LayerStr, ObjTypeStr : String;
    TargetX, TargetY, ObjX, ObjY : Integer;
    TargetLayer : TLayer;
    ObjFilter : TObjectId;
    Found : Boolean;
    FoundObj : IPCB_Primitive;
    Dist, BestDist : Double;
    BRect : TCoordRect;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    XStr := ExtractJsonValue(Params, 'x');
    YStr := ExtractJsonValue(Params, 'y');
    LayerStr := ExtractJsonValue(Params, 'layer');
    ObjTypeStr := ExtractJsonValue(Params, 'object_type');

    If (XStr = '') Or (YStr = '') Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'Missing "x" and/or "y" parameters');
        Exit;
    End;

    If ObjTypeStr = '' Then ObjTypeStr := 'track';

    TargetX := StrToIntDef(XStr, 0);
    TargetY := StrToIntDef(YStr, 0);

    If LayerStr <> '' Then
        TargetLayer := GetLayerFromString(LayerStr)
    Else
        TargetLayer := eTopLayer;

    // Map object type string to filter
    If ObjTypeStr = 'track' Then
        ObjFilter := eTrackObject
    Else If ObjTypeStr = 'via' Then
        ObjFilter := eViaObject
    Else If ObjTypeStr = 'fill' Then
        ObjFilter := eFillObject
    Else If ObjTypeStr = 'text' Then
        ObjFilter := eTextObject
    Else If ObjTypeStr = 'pad' Then
        ObjFilter := ePadObject
    Else If ObjTypeStr = 'arc' Then
        ObjFilter := eArcObject
    Else If ObjTypeStr = 'polygon' Then
        ObjFilter := ePolyObject
    Else If ObjTypeStr = 'region' Then
        ObjFilter := eRegionObject
    Else If ObjTypeStr = 'component' Then
        ObjFilter := eComponentObject
    Else
    Begin
        Result := BuildErrorResponse(RequestId, 'INVALID_PARAM',
            'Unknown object_type: ' + ObjTypeStr +
            '. Use track, via, fill, text, pad, arc, polygon, region, or component');
        Exit;
    End;

    // Find the closest matching object at the target coordinates
    Found := False;
    FoundObj := Nil;
    BestDist := 1e30;

    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(ObjFilter));
    Iterator.AddFilter_LayerSet(MkSet(TargetLayer));
    Iterator.AddFilter_Method(eProcessAll);

    Obj := Iterator.FirstPCBObject;
    While Obj <> Nil Do
    Begin
        // Get object position based on type
        If ObjFilter = eTrackObject Then
        Begin
            ObjX := CoordToMils((Obj.x1 + Obj.x2) Div 2);
            ObjY := CoordToMils((Obj.y1 + Obj.y2) Div 2);
        End
        Else If (ObjFilter = eViaObject) Or (ObjFilter = ePadObject) Then
        Begin
            ObjX := CoordToMils(Obj.x);
            ObjY := CoordToMils(Obj.y);
        End
        Else If ObjFilter = eFillObject Then
        Begin
            ObjX := CoordToMils((Obj.X1Location + Obj.X2Location) Div 2);
            ObjY := CoordToMils((Obj.Y1Location + Obj.Y2Location) Div 2);
        End
        Else
        Begin
            { Polygons, regions, components, arcs and text: use the bounding
              rectangle centre -- XLocation is not exposed on every type. }
            BRect := Obj.BoundingRectangle;
            ObjX := CoordToMils((BRect.Left + BRect.Right) Div 2);
            ObjY := CoordToMils((BRect.Bottom + BRect.Top) Div 2);
        End;

        Dist := Sqrt((ObjX - TargetX) * (ObjX - TargetX) + (ObjY - TargetY) * (ObjY - TargetY));
        If Dist < BestDist Then
        Begin
            BestDist := Dist;
            FoundObj := Obj;
            Found := True;
        End;

        Obj := Iterator.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(Iterator);

    If (Not Found) Or (BestDist > 100) Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NOT_FOUND',
            'No ' + ObjTypeStr + ' found within 100 mils of (' + IntToStr(TargetX) + ',' + IntToStr(TargetY) + ')');
        Exit;
    End;

    PCBServer.PreProcess;
    Try
        PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
            PCBM_BoardRegisteration, FoundObj.I_ObjectAddress);
        Board.RemovePCBObject(FoundObj);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"deleted":true,'
        + '"object_type":"' + EscapeJsonString(ObjTypeStr) + '",'
        + '"distance_mils":' + FloatToJsonStr(BestDist) + '}');
End;

{..............................................................................}
{ PCB_GetPadProperties - Get detailed pad info filtered by net or component  }
{ Params: net (optional), designator (optional)                              }
{..............................................................................}

Function PCB_GetPadProperties(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iterator : IPCB_BoardIterator;
    Pad : IPCB_Pad;
    FilterNet, FilterDesig : String;
    JsonItems, PadName, NetName, LayerStr, CompDesig, ShapeStr : String;
    PadCache : TPadCache;
    SolderMask, PasteMask : Integer;
    First : Boolean;
    Count : Integer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    FilterNet := ExtractJsonValue(Params, 'net');
    FilterDesig := ExtractJsonValue(Params, 'designator');

    JsonItems := '';
    First := True;
    Count := 0;

    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(ePadObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);

    Pad := Iterator.FirstPCBObject;
    While Pad <> Nil Do
    Begin
        // Get pad net name
        NetName := '';
        Try
            If Pad.Net <> Nil Then NetName := Pad.Net.Name;
        Except End;

        // Get parent component designator
        CompDesig := '';
        Try
            If Pad.Component <> Nil Then CompDesig := Pad.Component.Name.Text;
        Except End;

        // Apply filters
        If (FilterNet <> '') And (NetName <> FilterNet) Then
        Begin
            Pad := Iterator.NextPCBObject;
            Continue;
        End;
        If (FilterDesig <> '') And (CompDesig <> FilterDesig) Then
        Begin
            Pad := Iterator.NextPCBObject;
            Continue;
        End;

        If Not First Then JsonItems := JsonItems + ',';
        First := False;

        Try PadName := Pad.Name; Except PadName := ''; End;
        Try LayerStr := GetLayerString(Pad.Layer); Except LayerStr := 'Unknown'; End;

        // Get pad shape as string
        Try
            If Pad.TopShape = eRounded Then ShapeStr := 'Round'
            Else If Pad.TopShape = eRectangular Then ShapeStr := 'Rectangular'
            Else If Pad.TopShape = eOctagonal Then ShapeStr := 'Octagonal'
            Else If Pad.TopShape = eRoundedRectangular Then ShapeStr := 'RoundedRect'
            Else ShapeStr := 'Other';
        Except ShapeStr := 'Unknown'; End;

        // Get cache (solder/paste mask expansion)
        SolderMask := 0;
        PasteMask := 0;
        Try
            PadCache := Pad.GetState_Cache;
            If PadCache.SolderMaskExpansionValid = eCacheManual Then
                SolderMask := CoordToMils(PadCache.SolderMaskExpansion);
            If PadCache.PasteMaskExpansionValid = eCacheManual Then
                PasteMask := CoordToMils(PadCache.PasteMaskExpansion);
        Except End;

        JsonItems := JsonItems + '{"name":"' + EscapeJsonString(PadName) + '",'
            + '"component":"' + EscapeJsonString(CompDesig) + '",'
            + '"x":' + IntToStr(CoordToMils(Pad.x)) + ','
            + '"y":' + IntToStr(CoordToMils(Pad.y)) + ','
            + '"net":"' + EscapeJsonString(NetName) + '",'
            + '"layer":"' + EscapeJsonString(LayerStr) + '",'
            + '"shape":"' + EscapeJsonString(ShapeStr) + '",'
            + '"top_x_size":' + IntToStr(CoordToMils(Pad.TopXSize)) + ','
            + '"top_y_size":' + IntToStr(CoordToMils(Pad.TopYSize)) + ','
            + '"hole_size":' + IntToStr(CoordToMils(Pad.HoleSize)) + ','
            + '"rotation":' + FloatToJsonStr(Pad.Rotation) + ','
            + '"is_smd":' + BoolToJsonStr(Pad.IsSurfaceMount) + ','
            + '"solder_mask_expansion":' + IntToStr(SolderMask) + ','
            + '"paste_mask_expansion":' + IntToStr(PasteMask) + '}';
        Inc(Count);

        If Count >= 500 Then Break;  // Limit output size
        Pad := Iterator.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(Iterator);

    Result := BuildSuccessResponse(RequestId,
        '{"pads":[' + JsonItems + '],"count":' + IntToStr(Count) + '}');
End;

{..............................................................................}
{ PCB_SetTrackWidth - Modify track width for all tracks on a specific net    }
{ Params: net_name, width_mils                                               }
{..............................................................................}

Function PCB_SetTrackWidth(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iterator : IPCB_BoardIterator;
    Track : IPCB_Track;
    NetNameStr, WidthStr, TrackNetName : String;
    NewWidth, ModCount : Integer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    NetNameStr := ExtractJsonValue(Params, 'net_name');
    WidthStr := ExtractJsonValue(Params, 'width_mils');

    If NetNameStr = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'Missing "net_name" parameter');
        Exit;
    End;
    If WidthStr = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'Missing "width_mils" parameter');
        Exit;
    End;

    NewWidth := StrToIntDef(WidthStr, 10);
    ModCount := 0;

    PCBServer.PreProcess;
    Try
        Iterator := Board.BoardIterator_Create;
        Iterator.AddFilter_ObjectSet(MkSet(eTrackObject));
        Iterator.AddFilter_LayerSet(AllLayers);
        Iterator.AddFilter_Method(eProcessAll);

        Track := Iterator.FirstPCBObject;
        While Track <> Nil Do
        Begin
            TrackNetName := '';
            Try
                If Track.Net <> Nil Then TrackNetName := Track.Net.Name;
            Except End;

            If TrackNetName = NetNameStr Then
            Begin
                PCBServer.SendMessageToRobots(Track.I_ObjectAddress, c_Broadcast,
                    PCBM_BeginModify, c_NoEventData);

                Track.Width := MilsToCoord(NewWidth);

                PCBServer.SendMessageToRobots(Track.I_ObjectAddress, c_Broadcast,
                    PCBM_EndModify, c_NoEventData);
                Inc(ModCount);
            End;
            Track := Iterator.NextPCBObject;
        End;
        Board.BoardIterator_Destroy(Iterator);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"modified":true,'
        + '"net_name":"' + EscapeJsonString(NetNameStr) + '",'
        + '"width_mils":' + IntToStr(NewWidth) + ','
        + '"tracks_modified":' + IntToStr(ModCount) + '}');
End;

{..............................................................................}
{ PCB_GetUnroutedNets - Get nets with unrouted connections (ratsnest lines)  }
{..............................................................................}

Function PCB_GetUnroutedNets(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iterator : IPCB_BoardIterator;
    Obj : IPCB_Primitive;
    JsonItems, NetName, CountStr : String;
    FinalResp : String;
    First : Boolean;
    Count, I, FoundIdx, NewCount : Integer;
    { Heap-allocated parallel lists. Function-local `Array[0..N] Of T`     }
    { where T is any type (String, Integer, ...) silently corrupts this    }
    { function's return slot in DelphiScript - both array-of-string AND   }
    { array-of-int trigger it, the originally-documented narrower theory   }
    { was wrong. See [[delphiscript_fixed_string_array_bug]].              }
    NetNames, NetCounts : TStringList;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    NetNames := TStringList.Create;
    NetCounts := TStringList.Create;
    Try
        Count := 0;

        Iterator := Board.BoardIterator_Create;
        Iterator.AddFilter_ObjectSet(MkSet(eTrackObject, eViaObject, ePadObject,
            eComponentObject, eFillObject, eTextObject, ePolyObject, eConnectionObject));
        Iterator.AddFilter_LayerSet(AllLayers);
        Iterator.AddFilter_Method(eProcessAll);

        Obj := Iterator.FirstPCBObject;
        While Obj <> Nil Do
        Begin
            If Obj.ObjectId = eConnectionObject Then
            Begin
                NetName := '';
                Try
                    If Obj.Net <> Nil Then NetName := Obj.Net.Name;
                Except End;

                FoundIdx := NetNames.IndexOf(NetName);
                If FoundIdx >= 0 Then
                Begin
                    NewCount := StrToIntDef(NetCounts[FoundIdx], 0) + 1;
                    NetCounts[FoundIdx] := IntToStr(NewCount);
                End
                Else
                Begin
                    NetNames.Add(NetName);
                    NetCounts.Add('1');
                End;

                Inc(Count);
            End;
            Obj := Iterator.NextPCBObject;
        End;
        Board.BoardIterator_Destroy(Iterator);

        JsonItems := '';
        First := True;
        For I := 0 To NetNames.Count - 1 Do
        Begin
            If Not First Then JsonItems := JsonItems + ',';
            First := False;
            CountStr := NetCounts[I];
            JsonItems := JsonItems + '{"net":"' + EscapeJsonString(NetNames[I]) + '",'
                + '"unrouted_connections":' + CountStr + '}';
        End;

        FinalResp := BuildSuccessResponse(RequestId,
            '{"unrouted_nets":[' + JsonItems + '],"net_count":' + IntToStr(NetNames.Count)
            + ',"total_unrouted":' + IntToStr(Count) + '}');
        Result := FinalResp;
    Finally
        NetCounts.Free;
        NetNames.Free;
    End;
End;

{..............................................................................}
{ PolygonAreaSqMils - Compute a polygon's outline area in SQUARE MILS via the  }
{ shoelace formula over its line vertices. Altium's IPCB_Polygon.AreaSize is   }
{ unreliable (it can exceed the bounding box) and IPCB_Polygon.GeometricPolygon }
{ is undeclared in this script binding, so this vertex sum is the trustworthy   }
{ area. CRITICAL: convert each coord to mils (/ 10000.0, a REAL division)       }
{ BEFORE multiplying -- raw internal coords (~2e7) overflow 32-bit integer      }
{ multiplication and yield garbage.                                            }
{..............................................................................}

Function PolygonAreaSqMils(Poly : IPCB_Polygon) : Double;
Var
    I, N : Integer;
    Ax, Ay, Bx, By, Sum : Double;
Begin
    Result := 0;
    N := 0;
    Try N := Poly.PointCount; Except End;
    If N < 3 Then Exit;
    Sum := 0;
    For I := 0 To N - 1 Do
    Begin
        Try
            Ax := Poly.Segments[I].vx / 10000.0;
            Ay := Poly.Segments[I].vy / 10000.0;
            If I < N - 1 Then
            Begin
                Bx := Poly.Segments[I + 1].vx / 10000.0;
                By := Poly.Segments[I + 1].vy / 10000.0;
            End
            Else
            Begin
                Bx := Poly.Segments[0].vx / 10000.0;
                By := Poly.Segments[0].vy / 10000.0;
            End;
            Sum := Sum + (Ax * By - Bx * Ay);
        Except End;
    End;
    Result := Abs(Sum) / 2.0;
End;

{..............................................................................}
{ PCB_GetPolygons - Get all polygon pours with layer, net, hatching, etc.    }
{..............................................................................}

Function PCB_GetPolygons(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iterator : IPCB_BoardIterator;
    Polygon : IPCB_Polygon;
    JsonItems, NetName, LayerStr, HatchStr : String;
    First : Boolean;
    Count, VCount : Integer;
    AreaInternal : Int64;
    AreaSqMils, AreaMm2, BBoxMm2 : Double;
    BR : TCoordRect;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    JsonItems := '';
    First := True;
    Count := 0;

    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(ePolyObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);

    Polygon := Iterator.FirstPCBObject;
    While Polygon <> Nil Do
    Begin
        If Not First Then JsonItems := JsonItems + ',';
        First := False;

        NetName := '';
        Try
            If Polygon.Net <> Nil Then NetName := Polygon.Net.Name;
        Except End;
        Try LayerStr := GetLayerString(Polygon.Layer); Except LayerStr := 'Unknown'; End;

        // Get hatching style
        HatchStr := 'Unknown';
        Try
            If Polygon.PolyHatchStyle = ePolySolid Then HatchStr := 'Solid'
            Else If Polygon.PolyHatchStyle = ePolyNoHatch Then HatchStr := 'NoHatch'
            Else If Polygon.PolyHatchStyle = ePolyHatch45 Then HatchStr := '45Degree'
            Else If Polygon.PolyHatchStyle = ePolyHatch90 Then HatchStr := '90Degree'
            Else HatchStr := 'Other';
        Except End;

        { Compute actual copper area (after pour) and bounding-rect      }
        { area for the polygon outline. Used for current-capacity audits }
        { (multiply area_mm2 by copper thickness for cubic copper) and   }
        { for spotting accidentally-tiny power islands.                  }
        { AreaSize is the polygon OUTLINE area. IPCB_Polygon.GeometricPolygon
          is undeclared in this Altium script binding, so the actual-copper
          area is not available here -- use AreaSize (outline) + bbox. }
        AreaSqMils := 0;
        Try AreaSqMils := PolygonAreaSqMils(Polygon); Except End;
        AreaMm2 := AreaSqMils * 0.00064516;
        BR := Polygon.BoundingRectangle;
        BBoxMm2 := CoordToMM(BR.Right - BR.Left)
                 * CoordToMM(BR.Top - BR.Bottom);
        VCount := 0;
        Try VCount := Polygon.PointCount; Except End;

        JsonItems := JsonItems + '{"index":' + IntToStr(Count) + ','
            + '"name":"' + EscapeJsonString(Polygon.Name) + '",'
            + '"net":"' + EscapeJsonString(NetName) + '",'
            + '"layer":"' + EscapeJsonString(LayerStr) + '",'
            + '"hatch_style":"' + EscapeJsonString(HatchStr) + '",'
            + '"pour_over":' + BoolToJsonStr(Polygon.PourOver <> ePolygonPourOver_None) + ','
            + '"area_sqmils":' + IntToStr(Trunc(AreaSqMils)) + ','
            + '"area_mm2":' + FloatToJsonStr(AreaMm2) + ','
            + '"bbox_mm2":' + FloatToJsonStr(BBoxMm2) + ','
            + '"vertex_count":' + IntToStr(VCount) + '}';
        Inc(Count);
        Polygon := Iterator.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(Iterator);

    Result := BuildSuccessResponse(RequestId,
        '{"polygons":[' + JsonItems + '],"count":' + IntToStr(Count) + '}');
End;

{..............................................................................}
{ PCB_ModifyPolygon - Modify polygon pour properties                         }
{ Params: index (required), net (optional), layer (optional),               }
{         hatch_style (optional: Solid/45Degree/90Degree/Horizontal/Vertical)}
{..............................................................................}

Function PCB_ModifyPolygon(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iterator : IPCB_BoardIterator;
    Polygon : IPCB_Polygon;
    IndexStr, NetStr, LayerStr, HatchStr : String;
    TargetIdx, CurIdx : Integer;
    FoundPoly : IPCB_Polygon;
    FoundNet : IPCB_Net;
    Found : Boolean;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    IndexStr := ExtractJsonValue(Params, 'index');
    NetStr := ExtractJsonValue(Params, 'net');
    LayerStr := ExtractJsonValue(Params, 'layer');
    HatchStr := ExtractJsonValue(Params, 'hatch_style');

    If IndexStr = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'Missing "index" parameter');
        Exit;
    End;

    TargetIdx := StrToIntDef(IndexStr, -1);
    If TargetIdx < 0 Then
    Begin
        Result := BuildErrorResponse(RequestId, 'INVALID_PARAM', 'Invalid index value');
        Exit;
    End;

    // Find the polygon at the specified index
    Found := False;
    FoundPoly := Nil;
    CurIdx := 0;

    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(ePolyObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);

    Polygon := Iterator.FirstPCBObject;
    While Polygon <> Nil Do
    Begin
        If CurIdx = TargetIdx Then
        Begin
            FoundPoly := Polygon;
            Found := True;
            Break;
        End;
        Inc(CurIdx);
        Polygon := Iterator.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(Iterator);

    If Not Found Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NOT_FOUND', 'Polygon index ' + IntToStr(TargetIdx) + ' not found');
        Exit;
    End;

    PCBServer.PreProcess;
    Try
        PCBServer.SendMessageToRobots(FoundPoly.I_ObjectAddress, c_Broadcast,
            PCBM_BeginModify, c_NoEventData);

        // Modify net
        If NetStr <> '' Then
        Begin
            FoundNet := FindNetByName(Board, NetStr);
            If FoundNet <> Nil Then
                FoundPoly.Net := FoundNet;
        End;

        // Modify layer
        If LayerStr <> '' Then
            FoundPoly.Layer := GetLayerFromString(LayerStr);

        // Modify hatch style
        If HatchStr <> '' Then
        Begin
            If HatchStr = 'Solid' Then
                FoundPoly.PolyHatchStyle := ePolySolid
            Else If HatchStr = 'NoHatch' Then
                FoundPoly.PolyHatchStyle := ePolyNoHatch
            Else If HatchStr = '45Degree' Then
                FoundPoly.PolyHatchStyle := ePolyHatch45
            Else If HatchStr = '90Degree' Then
                FoundPoly.PolyHatchStyle := ePolyHatch90;
        End;

        PCBServer.SendMessageToRobots(FoundPoly.I_ObjectAddress, c_Broadcast,
            PCBM_EndModify, c_NoEventData);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"modified":true,'
        + '"index":' + IntToStr(TargetIdx) + ','
        + '"name":"' + EscapeJsonString(FoundPoly.Name) + '"}');
End;

{..............................................................................}
{ PCB_GetRoomRules - Get all room-like rules (confinement constraint rules)  }
{ Returns design rules of kind eRule_ConfinementConstraint, not physical rooms }
{..............................................................................}

Function PCB_GetRoomRules(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iterator : IPCB_BoardIterator;
    Rule : IPCB_Rule;
    Room : IPCB_ConfinementConstraint;
    JsonItems, KindStr : String;
    BR : TCoordRect;
    First : Boolean;
    Count : Integer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    JsonItems := '';
    First := True;
    Count := 0;

    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(eRuleObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);

    Rule := Iterator.FirstPCBObject;
    While Rule <> Nil Do
    Begin
        If Rule.RuleKind = eRule_ConfinementConstraint Then
        Begin
            If Not First Then JsonItems := JsonItems + ',';
            First := False;

            { BoundingRect / Kind / Comment etc. live on IPCB_ConfinementConstraint, }
            { not on the base IPCB_Rule. Narrow the typed reference before reading. }
            Room := Rule;
            Try
                BR := Room.BoundingRect;
            Except
                BR.Left := 0; BR.Bottom := 0; BR.Right := 0; BR.Top := 0;
            End;

            Try
                If Room.Kind = eConfineIn Then KindStr := 'ConfineIn'
                Else KindStr := 'ConfineOut';
            Except KindStr := 'Unknown'; End;

            JsonItems := JsonItems + '{"name":"' + EscapeJsonString(Rule.Name) + '",'
                + '"enabled":' + BoolToJsonStr(Rule.Enabled) + ','
                + '"kind":"' + EscapeJsonString(KindStr) + '",'
                + '"scope_1":"' + EscapeJsonString(Rule.Scope1Expression) + '",'
                + '"comment":"' + EscapeJsonString(Rule.Comment) + '",'
                + '"x1":' + IntToStr(CoordToMils(BR.Left)) + ','
                + '"y1":' + IntToStr(CoordToMils(BR.Bottom)) + ','
                + '"x2":' + IntToStr(CoordToMils(BR.Right)) + ','
                + '"y2":' + IntToStr(CoordToMils(BR.Top)) + '}';
            Inc(Count);
        End;
        Rule := Iterator.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(Iterator);

    Result := BuildSuccessResponse(RequestId,
        '{"room_rules":[' + JsonItems + '],"count":' + IntToStr(Count) + '}');
End;

{..............................................................................}
{ PCB_CreateRoom - Create a room (confinement constraint) for components     }
{ Params: name, x1, y1, x2, y2 (mils), components (comma-separated desig)  }
{..............................................................................}

Function PCB_CreateRoom(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Rule : IPCB_ConfinementConstraint;
    CoordRect : TCoordRect;
    RoomName, X1Str, Y1Str, X2Str, Y2Str, CompsStr, ScopeExpr : String;
    Remaining, OneDesig : String;
    RX1, RY1, RX2, RY2, CommaPos : Integer;
    First : Boolean;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    RoomName := ExtractJsonValue(Params, 'name');
    X1Str := ExtractJsonValue(Params, 'x1');
    Y1Str := ExtractJsonValue(Params, 'y1');
    X2Str := ExtractJsonValue(Params, 'x2');
    Y2Str := ExtractJsonValue(Params, 'y2');
    CompsStr := ExtractJsonValue(Params, 'components');

    If RoomName = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'Missing "name" parameter');
        Exit;
    End;
    If (X1Str = '') Or (Y1Str = '') Or (X2Str = '') Or (Y2Str = '') Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'Missing coordinate parameters (x1, y1, x2, y2)');
        Exit;
    End;

    RX1 := StrToIntDef(X1Str, 0);
    RY1 := StrToIntDef(Y1Str, 0);
    RX2 := StrToIntDef(X2Str, 0);
    RY2 := StrToIntDef(Y2Str, 0);

    // Build scope expression from component designators
    ScopeExpr := '';
    If CompsStr <> '' Then
    Begin
        First := True;
        Remaining := CompsStr;
        While Remaining <> '' Do
        Begin
            CommaPos := Pos(',', Remaining);
            If CommaPos > 0 Then
            Begin
                OneDesig := Copy(Remaining, 1, CommaPos - 1);
                Remaining := Copy(Remaining, CommaPos + 1, Length(Remaining));
            End
            Else
            Begin
                OneDesig := Remaining;
                Remaining := '';
            End;
            If OneDesig <> '' Then
            Begin
                If Not First Then ScopeExpr := ScopeExpr + ' OR ';
                First := False;
                ScopeExpr := ScopeExpr + 'InComponent(''' + OneDesig + ''')';
            End;
        End;
    End;
    If ScopeExpr = '' Then ScopeExpr := 'All';

    PCBServer.PreProcess;
    Try
        Rule := PCBServer.PCBRuleFactory(eRule_ConfinementConstraint);
        Rule.Name := RoomName;
        Rule.Comment := 'Room: ' + RoomName;
        Rule.NetScope := eNetScope_AnyNet;
        Rule.LayerKind := eRuleLayerKind_SameLayer;
        Rule.Scope1Expression := ScopeExpr;
        Rule.Kind := eConfineIn;
        Rule.Enabled := True;

        CoordRect.Left := MilsToCoord(RX1);
        CoordRect.Bottom := MilsToCoord(RY1);
        CoordRect.Right := MilsToCoord(RX2);
        CoordRect.Top := MilsToCoord(RY2);
        Rule.BoundingRect := CoordRect;

        Board.AddPCBObject(Rule);
        PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
            PCBM_BoardRegisteration, Rule.I_ObjectAddress);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"created":true,'
        + '"name":"' + EscapeJsonString(RoomName) + '",'
        + '"x1":' + IntToStr(RX1) + ','
        + '"y1":' + IntToStr(RY1) + ','
        + '"x2":' + IntToStr(RX2) + ','
        + '"y2":' + IntToStr(RY2) + ','
        + '"scope":"' + EscapeJsonString(ScopeExpr) + '"}');
End;

{..............................................................................}
{ PCB_GetBoardStatistics - Comprehensive board statistics                    }
{..............................................................................}

Function PCB_GetBoardStatistics(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iterator : IPCB_BoardIterator;
    Obj : IPCB_Primitive;
    Outline : IPCB_BoardOutline;
    LayerStack : IPCB_LayerStack_V7;
    LayerObj : IPCB_LayerObject_V7;
    BR : TCoordRect;
    TrackCount, ViaCount, PadCount, CompCount : Integer;
    FillCount, TextCount, PolyCount, ConnCount : Integer;
    LayerCount : Integer;
    TotalTraceLen, DX, DY : Double;
    BoardWidth, BoardHeight, BoardArea : Double;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    TrackCount := 0;
    ViaCount := 0;
    PadCount := 0;
    CompCount := 0;
    FillCount := 0;
    TextCount := 0;
    PolyCount := 0;
    ConnCount := 0;
    TotalTraceLen := 0;

    // Count all object types in a single pass
    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(eTrackObject, eViaObject, ePadObject,
        eComponentObject, eFillObject, eTextObject, ePolyObject, eConnectionObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);

    Obj := Iterator.FirstPCBObject;
    While Obj <> Nil Do
    Begin
        If Obj.ObjectId = eTrackObject Then
        Begin
            Inc(TrackCount);
            DX := CoordToMils(Obj.x2) - CoordToMils(Obj.x1);
            DY := CoordToMils(Obj.y2) - CoordToMils(Obj.y1);
            TotalTraceLen := TotalTraceLen + Sqrt(DX * DX + DY * DY);
        End
        Else If Obj.ObjectId = eViaObject Then Inc(ViaCount)
        Else If Obj.ObjectId = ePadObject Then Inc(PadCount)
        Else If Obj.ObjectId = eComponentObject Then Inc(CompCount)
        Else If Obj.ObjectId = eFillObject Then Inc(FillCount)
        Else If Obj.ObjectId = eTextObject Then Inc(TextCount)
        Else If Obj.ObjectId = ePolyObject Then Inc(PolyCount)
        Else If Obj.ObjectId = eConnectionObject Then Inc(ConnCount);
        Obj := Iterator.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(Iterator);

    // Board dimensions from outline
    BoardWidth := 0;
    BoardHeight := 0;
    BoardArea := 0;
    Try
        Outline := Board.BoardOutline;
        If Outline <> Nil Then
        Begin
            Outline.Invalidate;
            Outline.Rebuild;
            Outline.Validate;
            BR := Outline.BoundingRectangle;
            BoardWidth := CoordToMils(BR.Right) - CoordToMils(BR.Left);
            BoardHeight := CoordToMils(BR.Top) - CoordToMils(BR.Bottom);
            BoardArea := BoardWidth * BoardHeight;
        End;
    Except End;

    // Layer count
    LayerCount := 0;
    Try
        LayerStack := Board.LayerStack_V7;
        If LayerStack <> Nil Then
        Begin
            LayerObj := LayerStack.FirstLayer;
            While LayerObj <> Nil Do
            Begin
                Inc(LayerCount);
                LayerObj := LayerStack.NextLayer(LayerObj);
            End;
        End;
    Except End;

    Result := BuildSuccessResponse(RequestId,
        '{"track_count":' + IntToStr(TrackCount) + ','
        + '"via_count":' + IntToStr(ViaCount) + ','
        + '"pad_count":' + IntToStr(PadCount) + ','
        + '"component_count":' + IntToStr(CompCount) + ','
        + '"fill_count":' + IntToStr(FillCount) + ','
        + '"text_count":' + IntToStr(TextCount) + ','
        + '"polygon_count":' + IntToStr(PolyCount) + ','
        + '"unrouted_connections":' + IntToStr(ConnCount) + ','
        + '"total_trace_length_mils":' + FloatToJsonStr(TotalTraceLen) + ','
        + '"board_width_mils":' + FloatToJsonStr(BoardWidth) + ','
        + '"board_height_mils":' + FloatToJsonStr(BoardHeight) + ','
        + '"board_area_sq_mils":' + FloatToJsonStr(BoardArea) + ','
        + '"layer_count":' + IntToStr(LayerCount) + ','
        + '"board_name":"' + EscapeJsonString(ExtractFileName(Board.FileName)) + '"}');
End;

{..............................................................................}
{ PCB_ExportCoordinates - Export pick-and-place component coordinates        }
{..............................................................................}

Function PCB_ExportCoordinates(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iterator : IPCB_BoardIterator;
    Comp : IPCB_Component;
    JsonItems, Designator, Footprint, LayerStr, Comment : String;
    First : Boolean;
    Count : Integer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    JsonItems := '';
    First := True;
    Count := 0;

    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(eComponentObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);

    Comp := Iterator.FirstPCBObject;
    While Comp <> Nil Do
    Begin
        If Not First Then JsonItems := JsonItems + ',';
        First := False;

        Try Designator := Comp.Name.Text; Except Designator := ''; End;
        Try Footprint := Comp.Pattern; Except Footprint := ''; End;
        Try LayerStr := GetLayerString(Comp.Layer); Except LayerStr := 'Unknown'; End;
        Try Comment := Comp.Comment.Text; Except Comment := ''; End;

        JsonItems := JsonItems + '{"designator":"' + EscapeJsonString(Designator) + '",'
            + '"footprint":"' + EscapeJsonString(Footprint) + '",'
            + '"comment":"' + EscapeJsonString(Comment) + '",'
            + '"x":' + IntToStr(CoordToMils(Comp.x)) + ','
            + '"y":' + IntToStr(CoordToMils(Comp.y)) + ','
            + '"rotation":' + FloatToJsonStr(Comp.Rotation) + ','
            + '"layer":"' + EscapeJsonString(LayerStr) + '",';
        If Comp.Layer = eTopLayer Then
            JsonItems := JsonItems + '"side":"Top"}'
        Else
            JsonItems := JsonItems + '"side":"Bottom"}';
        Inc(Count);
        Comp := Iterator.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(Iterator);

    Result := BuildSuccessResponse(RequestId,
        '{"placements":[' + JsonItems + '],"count":' + IntToStr(Count) + ','
        + '"board_name":"' + EscapeJsonString(ExtractFileName(Board.FileName)) + '"}');
End;

{..............................................................................}
{ PCB_SetBoardShape - Define the board outline as a rectangle                 }
{ Params: x1,y1,x2,y2 in mils (opposite corners, any order)                   }
{..............................................................................}

Function PCB_SetBoardShape(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    X1, Y1, X2, Y2, TmpI : Integer;
    Cx1, Cy1, Cx2, Cy2 : TCoord;
    Seg : TPolySegment;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    X1 := StrToIntDef(ExtractJsonValue(Params, 'x1'), 0);
    Y1 := StrToIntDef(ExtractJsonValue(Params, 'y1'), 0);
    X2 := StrToIntDef(ExtractJsonValue(Params, 'x2'), 0);
    Y2 := StrToIntDef(ExtractJsonValue(Params, 'y2'), 0);

    If X1 > X2 Then Begin TmpI := X1; X1 := X2; X2 := TmpI; End;
    If Y1 > Y2 Then Begin TmpI := Y1; Y1 := Y2; Y2 := TmpI; End;

    Cx1 := MilsToCoord(X1);  Cy1 := MilsToCoord(Y1);
    Cx2 := MilsToCoord(X2);  Cy2 := MilsToCoord(Y2);

    PCBServer.PreProcess;
    Try
        { IPCB_BoardOutline inherits from IPCB_Polygon. Per the verified
          DelphiScript idiom, you assign a
          full TPolySegment record to Segments[I] rather than writing
          individual fields through the indexed property. Build each
          corner as a local record and assign in order. }
        Board.BoardOutline.PointCount := 4;
        Seg := TPolySegment;   { instantiate before any field write -- see memory }
        Seg.Kind := ePolySegmentLine;

        Seg.vx := Cx1;  Seg.vy := Cy1;  Board.BoardOutline.Segments[0] := Seg;
        Seg.vx := Cx2;  Seg.vy := Cy1;  Board.BoardOutline.Segments[1] := Seg;
        Seg.vx := Cx2;  Seg.vy := Cy2;  Board.BoardOutline.Segments[2] := Seg;
        Seg.vx := Cx1;  Seg.vy := Cy2;  Board.BoardOutline.Segments[3] := Seg;

        Board.BoardOutline.Invalidate;
        Board.BoardOutline.Rebuild;
        Board.BoardOutline.Validate;
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"success":true,'
        + '"x1":' + IntToStr(X1) + ',"y1":' + IntToStr(Y1) + ','
        + '"x2":' + IntToStr(X2) + ',"y2":' + IntToStr(Y2) + '}');
End;

{..............................................................................}
{ PCB_PlacePolygonRect - Drop a copper polygon pour on a rectangular area     }
{ Params: x1,y1,x2,y2 in mils, net=<name>, layer=<layer>, pour_over=<bool>   }
{..............................................................................}

Function PCB_PlacePolygonRect(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Polygon : IPCB_Polygon;
    X1, Y1, X2, Y2, TmpI : Integer;
    Cx1, Cy1, Cx2, Cy2 : TCoord;
    NetStr, LayerStr, PourOverStr : String;
    FoundNet : IPCB_Net;
    Seg : TPolySegment;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    X1 := StrToIntDef(ExtractJsonValue(Params, 'x1'), 0);
    Y1 := StrToIntDef(ExtractJsonValue(Params, 'y1'), 0);
    X2 := StrToIntDef(ExtractJsonValue(Params, 'x2'), 0);
    Y2 := StrToIntDef(ExtractJsonValue(Params, 'y2'), 0);
    NetStr := ExtractJsonValue(Params, 'net');
    LayerStr := ExtractJsonValue(Params, 'layer');
    PourOverStr := ExtractJsonValue(Params, 'pour_over');

    If X1 > X2 Then Begin TmpI := X1; X1 := X2; X2 := TmpI; End;
    If Y1 > Y2 Then Begin TmpI := Y1; Y1 := Y2; Y2 := TmpI; End;
    Cx1 := MilsToCoord(X1);  Cy1 := MilsToCoord(Y1);
    Cx2 := MilsToCoord(X2);  Cy2 := MilsToCoord(Y2);

    PCBServer.PreProcess;
    Try
        Polygon := PCBServer.PCBObjectFactory(ePolyObject, eNoDimension, eCreate_Default);
        If Polygon = Nil Then
        Begin
            PCBServer.PostProcess;
            Result := BuildErrorResponse(RequestId, 'CREATE_FAILED', 'Failed to create polygon object');
            Exit;
        End;

        If LayerStr = '' Then LayerStr := 'TopLayer';
        Polygon.Layer := GetLayerFromString(LayerStr);

        Polygon.PolyHatchStyle := ePolySolid;

        { Build the 4-corner outline via the Segments API. CRITICAL: a local
          TPolySegment must be instantiated with ':= TPolySegment' before its
          fields can be written -- without that, Seg.Kind := ... raises
          "Undeclared identifier: Kind" in the script engine. IPCB_Polygon has
          no SetOutlineContour (that is a region-only method), so Segments is
          the only path. Whole-record writes (Segments[i] := Seg) are fine. }
        Polygon.PointCount := 4;
        Seg := TPolySegment;
        Seg.Kind := ePolySegmentLine;
        Seg.vx := Cx1;  Seg.vy := Cy1;  Polygon.Segments[0] := Seg;
        Seg.vx := Cx2;  Seg.vy := Cy1;  Polygon.Segments[1] := Seg;
        Seg.vx := Cx2;  Seg.vy := Cy2;  Polygon.Segments[2] := Seg;
        Seg.vx := Cx1;  Seg.vy := Cy2;  Polygon.Segments[3] := Seg;

        { Assign net if specified. }
        If NetStr <> '' Then
        Begin
            Try FoundNet := Board.GetNetByName(NetStr); Except FoundNet := Nil; End;
            If FoundNet <> Nil Then Polygon.Net := FoundNet;
        End;

        { PourOver controls whether the polygon pours over existing
          same-net primitives (tracks/pads). Default true, matches the
          common "pour GND plane" case. }
        If (PourOverStr = '') Or (LowerCase(PourOverStr) = 'true') Then
            Polygon.PourOver := ePolygonPourOver_SameNet
        Else
            Polygon.PourOver := ePolygonPourOver_None;

        Board.AddPCBObject(Polygon);
        PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
            PCBM_BoardRegisteration, Polygon.I_ObjectAddress);
        Polygon.Rebuild;
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"placed":true,'
        + '"x1":' + IntToStr(X1) + ',"y1":' + IntToStr(Y1) + ','
        + '"x2":' + IntToStr(X2) + ',"y2":' + IntToStr(Y2) + ','
        + '"layer":"' + EscapeJsonString(LayerStr) + '",'
        + '"net":"' + EscapeJsonString(NetStr) + '"}');
End;

{..............................................................................}
{ PCB_PlaceViaArray - Stitch vias in a grid across a rectangle                }
{ Params: x1,y1,x2,y2 in mils, pitch=<mils>, net=<name>, size=<mils>,        }
{         hole_size=<mils>, low_layer=<layer>, high_layer=<layer>            }
{..............................................................................}

Function PCB_PlaceViaArray(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Via : IPCB_Via;
    X1, Y1, X2, Y2, TmpI, Pitch, ViaSize, ViaHole : Integer;
    NetStr, LowLayerStr, HighLayerStr : String;
    FoundNet : IPCB_Net;
    Ix, Iy, PlacedCount : Integer;
    LowLayer, HighLayer : TLayer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    X1 := StrToIntDef(ExtractJsonValue(Params, 'x1'), 0);
    Y1 := StrToIntDef(ExtractJsonValue(Params, 'y1'), 0);
    X2 := StrToIntDef(ExtractJsonValue(Params, 'x2'), 0);
    Y2 := StrToIntDef(ExtractJsonValue(Params, 'y2'), 0);
    Pitch := StrToIntDef(ExtractJsonValue(Params, 'pitch'), 50);
    ViaSize := StrToIntDef(ExtractJsonValue(Params, 'size'), 30);
    ViaHole := StrToIntDef(ExtractJsonValue(Params, 'hole_size'), 12);
    NetStr := ExtractJsonValue(Params, 'net');
    LowLayerStr := ExtractJsonValue(Params, 'low_layer');
    HighLayerStr := ExtractJsonValue(Params, 'high_layer');

    If X1 > X2 Then Begin TmpI := X1; X1 := X2; X2 := TmpI; End;
    If Y1 > Y2 Then Begin TmpI := Y1; Y1 := Y2; Y2 := TmpI; End;
    If Pitch < 10 Then Pitch := 10;   { Safety: clamp to sane minimum. }

    FoundNet := Nil;
    If NetStr <> '' Then
        Try FoundNet := Board.GetNetByName(NetStr); Except End;

    If LowLayerStr = '' Then LowLayer := eTopLayer
    Else LowLayer := GetLayerFromString(LowLayerStr);
    If HighLayerStr = '' Then HighLayer := eBottomLayer
    Else HighLayer := GetLayerFromString(HighLayerStr);

    PlacedCount := 0;
    PCBServer.PreProcess;
    Try
        Iy := Y1;
        While Iy <= Y2 Do
        Begin
            Ix := X1;
            While Ix <= X2 Do
            Begin
                Via := PCBServer.PCBObjectFactory(eViaObject, eNoDimension, eCreate_Default);
                If Via <> Nil Then
                Begin
                    Via.x := MilsToCoord(Ix);
                    Via.y := MilsToCoord(Iy);
                    Via.Size := MilsToCoord(ViaSize);
                    Via.HoleSize := MilsToCoord(ViaHole);
                    Via.LowLayer := LowLayer;
                    Via.HighLayer := HighLayer;
                    If FoundNet <> Nil Then Via.Net := FoundNet;
                    Board.AddPCBObject(Via);
                    PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
                        PCBM_BoardRegisteration, Via.I_ObjectAddress);
                    Inc(PlacedCount);
                End;
                Ix := Ix + Pitch;
            End;
            Iy := Iy + Pitch;
        End;
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"placed":' + IntToStr(PlacedCount) + ','
        + '"x1":' + IntToStr(X1) + ',"y1":' + IntToStr(Y1) + ','
        + '"x2":' + IntToStr(X2) + ',"y2":' + IntToStr(Y2) + ','
        + '"pitch":' + IntToStr(Pitch) + ','
        + '"net":"' + EscapeJsonString(NetStr) + '"}');
End;

{..............................................................................}
{ PCB_CreateDiffPair - Create a differential-pair object from two net names  }
{ Params: name=<diff pair name>, positive_net=<net>, negative_net=<net>      }
{..............................................................................}

Function PCB_CreateDiffPair(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    DiffPair : IPCB_DifferentialPair;
    DPName, PosNet, NegNet : String;
    PosNetObj, NegNetObj : IPCB_Net;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    DPName := ExtractJsonValue(Params, 'name');
    PosNet := ExtractJsonValue(Params, 'positive_net');
    NegNet := ExtractJsonValue(Params, 'negative_net');

    If (PosNet = '') Or (NegNet = '') Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM',
            'positive_net and negative_net are required');
        Exit;
    End;
    If DPName = '' Then DPName := PosNet + '_' + NegNet;

    PosNetObj := Nil;
    NegNetObj := Nil;
    Try PosNetObj := Board.GetNetByName(PosNet); Except End;
    Try NegNetObj := Board.GetNetByName(NegNet); Except End;
    If (PosNetObj = Nil) Or (NegNetObj = Nil) Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NET_NOT_FOUND',
            'Could not find one or both nets on the board');
        Exit;
    End;

    PCBServer.PreProcess;
    Try
        DiffPair := PCBServer.PCBObjectFactory(eDifferentialPairObject, eNoDimension, eCreate_Default);
        If DiffPair = Nil Then
        Begin
            PCBServer.PostProcess;
            Result := BuildErrorResponse(RequestId, 'CREATE_FAILED', 'Failed to create diff pair object');
            Exit;
        End;

        DiffPair.Name := DPName;
        DiffPair.PositiveNet := PosNetObj;
        DiffPair.NegativeNet := NegNetObj;

        Board.AddPCBObject(DiffPair);
        PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
            PCBM_BoardRegisteration, DiffPair.I_ObjectAddress);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"created":true,"name":"' + EscapeJsonString(DPName) + '",'
        + '"positive_net":"' + EscapeJsonString(PosNet) + '",'
        + '"negative_net":"' + EscapeJsonString(NegNet) + '"}');
End;

{..............................................................................}
{ PCB_PlaceRegion - Drop a solid copper region (no net) on a rectangle        }
{ Params: x1,y1,x2,y2 in mils, layer=<layer>, net=<optional>                  }
{ Regions are solid primitives without a net; they don't participate in the   }
{ connectivity engine. Use place_polygon_rect for a net-associated pour.      }
{..............................................................................}

Function PCB_PlaceRegion(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Region : IPCB_Region;
    Contour : IPCB_Contour;
    X1, Y1, X2, Y2, TmpI : Integer;
    Cx1, Cy1, Cx2, Cy2 : TCoord;
    LayerStr, NetStr : String;
    FoundNet : IPCB_Net;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    X1 := StrToIntDef(ExtractJsonValue(Params, 'x1'), 0);
    Y1 := StrToIntDef(ExtractJsonValue(Params, 'y1'), 0);
    X2 := StrToIntDef(ExtractJsonValue(Params, 'x2'), 0);
    Y2 := StrToIntDef(ExtractJsonValue(Params, 'y2'), 0);
    LayerStr := ExtractJsonValue(Params, 'layer');
    NetStr := ExtractJsonValue(Params, 'net');

    If X1 > X2 Then Begin TmpI := X1; X1 := X2; X2 := TmpI; End;
    If Y1 > Y2 Then Begin TmpI := Y1; Y1 := Y2; Y2 := TmpI; End;
    Cx1 := MilsToCoord(X1);  Cy1 := MilsToCoord(Y1);
    Cx2 := MilsToCoord(X2);  Cy2 := MilsToCoord(Y2);

    PCBServer.PreProcess;
    Try
        Region := PCBServer.PCBObjectFactory(eRegionObject, eNoDimension, eCreate_Default);
        If Region = Nil Then
        Begin
            PCBServer.PostProcess;
            Result := BuildErrorResponse(RequestId, 'CREATE_FAILED', 'Failed to create region object');
            Exit;
        End;

        If LayerStr = '' Then LayerStr := 'TopLayer';
        Region.Layer := GetLayerFromString(LayerStr);

        { IPCB_Region uses MainContour + SetOutlineContour (NOT the polygon
          Segments API). Note that the contour X[I]/Y[I] arrays are
          1-based (not 0-based). }
        Contour := Region.MainContour.Replicate;
        Contour.Count := 4;
        Contour.X[1] := Cx1;  Contour.Y[1] := Cy1;
        Contour.X[2] := Cx2;  Contour.Y[2] := Cy1;
        Contour.X[3] := Cx2;  Contour.Y[3] := Cy2;
        Contour.X[4] := Cx1;  Contour.Y[4] := Cy2;
        Region.SetOutlineContour(Contour);

        If NetStr <> '' Then
        Begin
            Try FoundNet := Board.GetNetByName(NetStr); Except FoundNet := Nil; End;
            If FoundNet <> Nil Then Region.Net := FoundNet;
        End;

        Board.AddPCBObject(Region);
        PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
            PCBM_BoardRegisteration, Region.I_ObjectAddress);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"placed":true,'
        + '"x1":' + IntToStr(X1) + ',"y1":' + IntToStr(Y1) + ','
        + '"x2":' + IntToStr(X2) + ',"y2":' + IntToStr(Y2) + ','
        + '"layer":"' + EscapeJsonString(LayerStr) + '",'
        + '"net":"' + EscapeJsonString(NetStr) + '"}');
End;

{..............................................................................}
{ PCB_DistributeComponents - Evenly space components along X or Y             }
{ Params: designators=<comma list>, axis=x|y, start=<mils>, end=<mils>        }
{..............................................................................}

Function PCB_DistributeComponents(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    DesStr, AxisStr, StartStr, EndStr, Remaining, DesName : String;
    AxisX : Boolean;
    StartVal, EndVal, CommaPos, Count, I : Integer;
    Step : Double;
    NewPos : Integer;
    CompList : TInterfaceList;
    Comp : IPCB_Component;
    Iterator : IPCB_BoardIterator;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    DesStr := ExtractJsonValue(Params, 'designators');
    AxisStr := LowerCase(ExtractJsonValue(Params, 'axis'));
    StartStr := ExtractJsonValue(Params, 'start');
    EndStr := ExtractJsonValue(Params, 'end');

    If DesStr = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'designators required');
        Exit;
    End;
    If (StartStr = '') Or (EndStr = '') Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'start and end required');
        Exit;
    End;
    If AxisStr = '' Then AxisStr := 'x';
    AxisX := (AxisStr = 'x');
    StartVal := StrToIntDef(StartStr, 0);
    EndVal := StrToIntDef(EndStr, 0);

    { Collect components that match the comma list, preserving list order. }
    CompList := CreateObject(TInterfaceList);
    Remaining := DesStr;
    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(eComponentObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);
    While Remaining <> '' Do
    Begin
        CommaPos := Pos(',', Remaining);
        If CommaPos > 0 Then
        Begin
            DesName := Copy(Remaining, 1, CommaPos - 1);
            Remaining := Copy(Remaining, CommaPos + 1, Length(Remaining));
        End
        Else
        Begin
            DesName := Remaining;
            Remaining := '';
        End;
        If DesName = '' Then Continue;

        Comp := Iterator.FirstPCBObject;
        While Comp <> Nil Do
        Begin
            Try
                If Comp.Name.Text = DesName Then
                Begin
                    CompList.Add(Comp);
                    Break;
                End;
            Except End;
            Comp := Iterator.NextPCBObject;
        End;
    End;
    Board.BoardIterator_Destroy(Iterator);

    Count := CompList.Count;
    If Count < 2 Then
    Begin
        CompList.Free;
        Result := BuildErrorResponse(RequestId, 'TOO_FEW',
            'Need at least 2 components to distribute (matched ' + IntToStr(Count) + ')');
        Exit;
    End;

    Step := (EndVal - StartVal) / (Count - 1);

    PCBServer.PreProcess;
    Try
        For I := 0 To Count - 1 Do
        Begin
            Comp := CompList.Items[I];
            If Comp = Nil Then Continue;
            NewPos := StartVal + Round(Step * I);
            PCBServer.SendMessageToRobots(Comp.I_ObjectAddress, c_Broadcast,
                PCBM_BeginModify, c_NoEventData);
            If AxisX Then
                Comp.x := MilsToCoord(NewPos)
            Else
                Comp.y := MilsToCoord(NewPos);
            PCBServer.SendMessageToRobots(Comp.I_ObjectAddress, c_Broadcast,
                PCBM_EndModify, c_NoEventData);
        End;
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);
    CompList.Free;

    Result := BuildSuccessResponse(RequestId,
        '{"distributed":' + IntToStr(Count) + ','
        + '"axis":"' + EscapeJsonString(AxisStr) + '",'
        + '"start":' + IntToStr(StartVal) + ',"end":' + IntToStr(EndVal) + '}');
End;

{..............................................................................}
{ PCB_PlaceDimension - Place a linear dimension (horizontal or vertical)      }
{ Params: x1,y1,x2,y2 in mils, layer=<layer>, orientation=horizontal|vertical }
{..............................................................................}

Function PCB_PlaceDimension(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Dim : IPCB_Dimension;
    X1, Y1, X2, Y2, TextX, TextY : Integer;
    Orient, LayerStr : String;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    X1 := StrToIntDef(ExtractJsonValue(Params, 'x1'), 0);
    Y1 := StrToIntDef(ExtractJsonValue(Params, 'y1'), 0);
    X2 := StrToIntDef(ExtractJsonValue(Params, 'x2'), 0);
    Y2 := StrToIntDef(ExtractJsonValue(Params, 'y2'), 0);
    LayerStr := ExtractJsonValue(Params, 'layer');
    Orient := LowerCase(ExtractJsonValue(Params, 'orientation'));

    If LayerStr = '' Then LayerStr := 'TopOverlay';
    { Auto-detect orientation if unset: whichever axis has the larger delta. }
    If Orient = '' Then
    Begin
        If Abs(X2 - X1) >= Abs(Y2 - Y1) Then Orient := 'horizontal'
        Else Orient := 'vertical';
    End;

    { Centre the text label between the two endpoints with a small offset
      along the perpendicular axis so it doesn't sit on top of geometry. }
    If Orient = 'horizontal' Then
    Begin
        TextX := (X1 + X2) Div 2;
        TextY := Y1 + 50;
    End
    Else
    Begin
        TextX := X1 + 50;
        TextY := (Y1 + Y2) Div 2;
    End;

    PCBServer.PreProcess;
    Try
        Dim := PCBServer.PCBObjectFactory(eDimensionObject, eLinearDimension, eCreate_Default);
        If Dim = Nil Then
        Begin
            PCBServer.PostProcess;
            Result := BuildErrorResponse(RequestId, 'CREATE_FAILED', 'Failed to create dimension');
            Exit;
        End;

        Dim.Layer := GetLayerFromString(LayerStr);
        Dim.DimensionKind := eLinearDimension;
        Dim.X1Location := MilsToCoord(X1);
        Dim.Y1Location := MilsToCoord(Y1);
        { Size is the dimension extent; for horizontal linear it's delta-X,
          for vertical linear it's delta-Y. Negative is clamped to absolute. }
        If Orient = 'horizontal' Then
            Dim.Size := MilsToCoord(Abs(X2 - X1))
        Else
            Dim.Size := MilsToCoord(Abs(Y2 - Y1));

        Dim.TextX := MilsToCoord(TextX);
        Dim.TextY := MilsToCoord(TextY);

        Board.AddPCBObject(Dim);
        PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
            PCBM_BoardRegisteration, Dim.I_ObjectAddress);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"placed":true,'
        + '"x1":' + IntToStr(X1) + ',"y1":' + IntToStr(Y1) + ','
        + '"x2":' + IntToStr(X2) + ',"y2":' + IntToStr(Y2) + ','
        + '"orientation":"' + EscapeJsonString(Orient) + '",'
        + '"layer":"' + EscapeJsonString(LayerStr) + '"}');
End;

{..............................................................................}
{ PCB_PlacePad - Place a standalone pad (fiducial, test point, mounting hole) }
{ Params: x,y in mils, name=<designator>, net=<name>, shape=round|rect|oct,  }
{         x_size=<mils>, y_size=<mils>, hole_size=<mils>, layer=<layer>      }
{..............................................................................}

Function PCB_PlacePad(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Pad : IPCB_Pad;
    X, Y, XSize, YSize, HoleSize : Integer;
    Shape, NameStr, NetStr, LayerStr : String;
    FoundNet : IPCB_Net;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    X := StrToIntDef(ExtractJsonValue(Params, 'x'), 0);
    Y := StrToIntDef(ExtractJsonValue(Params, 'y'), 0);
    XSize := StrToIntDef(ExtractJsonValue(Params, 'x_size'), 60);
    YSize := StrToIntDef(ExtractJsonValue(Params, 'y_size'), 60);
    HoleSize := StrToIntDef(ExtractJsonValue(Params, 'hole_size'), 0);
    Shape := LowerCase(ExtractJsonValue(Params, 'shape'));
    NameStr := ExtractJsonValue(Params, 'name');
    NetStr := ExtractJsonValue(Params, 'net');
    LayerStr := ExtractJsonValue(Params, 'layer');

    If LayerStr = '' Then LayerStr := 'TopLayer';
    If Shape = '' Then Shape := 'round';

    PCBServer.PreProcess;
    Try
        Pad := PCBServer.PCBObjectFactory(ePadObject, eNoDimension, eCreate_Default);
        If Pad = Nil Then
        Begin
            PCBServer.PostProcess;
            Result := BuildErrorResponse(RequestId, 'CREATE_FAILED', 'Failed to create pad');
            Exit;
        End;

        Pad.X := MilsToCoord(X);
        Pad.Y := MilsToCoord(Y);
        Pad.TopXSize := MilsToCoord(XSize);
        Pad.TopYSize := MilsToCoord(YSize);
        Pad.HoleSize := MilsToCoord(HoleSize);
        Pad.Layer := GetLayerFromString(LayerStr);
        If NameStr <> '' Then Pad.Name := NameStr;

        If Shape = 'rect' Then Pad.TopShape := eRectangular
        Else If Shape = 'oct' Then Pad.TopShape := eOctagonal
        Else Pad.TopShape := eRounded;

        If NetStr <> '' Then
        Begin
            Try FoundNet := Board.GetNetByName(NetStr); Except FoundNet := Nil; End;
            If FoundNet <> Nil Then Pad.Net := FoundNet;
        End;

        Board.AddPCBObject(Pad);
        PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
            PCBM_BoardRegisteration, Pad.I_ObjectAddress);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"placed":true,"x":' + IntToStr(X) + ',"y":' + IntToStr(Y) + ','
        + '"x_size":' + IntToStr(XSize) + ',"y_size":' + IntToStr(YSize) + ','
        + '"hole_size":' + IntToStr(HoleSize) + ','
        + '"shape":"' + EscapeJsonString(Shape) + '",'
        + '"layer":"' + EscapeJsonString(LayerStr) + '",'
        + '"name":"' + EscapeJsonString(NameStr) + '",'
        + '"net":"' + EscapeJsonString(NetStr) + '"}');
End;

{..............................................................................}
{ PCB_PlaceAngularDimension - Place an angular dimension (arc between 2 axes) }
{ Params: center_x, center_y, x1,y1, x2,y2 in mils, radius in mils,          }
{         layer=<layer>                                                      }
{..............................................................................}

Function PCB_PlaceAngularDimension(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Dim : IPCB_Dimension;
    Cx, Cy, X1, Y1, X2, Y2, Radius : Integer;
    LayerStr : String;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    Cx := StrToIntDef(ExtractJsonValue(Params, 'center_x'), 0);
    Cy := StrToIntDef(ExtractJsonValue(Params, 'center_y'), 0);
    X1 := StrToIntDef(ExtractJsonValue(Params, 'x1'), 0);
    Y1 := StrToIntDef(ExtractJsonValue(Params, 'y1'), 0);
    X2 := StrToIntDef(ExtractJsonValue(Params, 'x2'), 0);
    Y2 := StrToIntDef(ExtractJsonValue(Params, 'y2'), 0);
    Radius := StrToIntDef(ExtractJsonValue(Params, 'radius'), 100);
    LayerStr := ExtractJsonValue(Params, 'layer');
    If LayerStr = '' Then LayerStr := 'TopOverlay';

    PCBServer.PreProcess;
    Try
        Dim := PCBServer.PCBObjectFactory(eDimensionObject, eAngularDimension, eCreate_Default);
        If Dim = Nil Then
        Begin
            PCBServer.PostProcess;
            Result := BuildErrorResponse(RequestId, 'CREATE_FAILED', 'Failed to create angular dimension');
            Exit;
        End;
        Dim.Layer := GetLayerFromString(LayerStr);
        Dim.DimensionKind := eAngularDimension;
        Dim.X1Location := MilsToCoord(Cx);
        Dim.Y1Location := MilsToCoord(Cy);
        Dim.TextX := MilsToCoord(Cx);
        Dim.TextY := MilsToCoord(Cy + Radius + 20);
        Board.AddPCBObject(Dim);
        PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
            PCBM_BoardRegisteration, Dim.I_ObjectAddress);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"placed":true,"kind":"angular","center_x":' + IntToStr(Cx)
        + ',"center_y":' + IntToStr(Cy) + ',"radius":' + IntToStr(Radius)
        + ',"layer":"' + EscapeJsonString(LayerStr) + '"}');
End;

{..............................................................................}
{ PCB_PlaceRadialDimension - Place a radial dimension around a center point   }
{ Params: center_x, center_y, radius in mils, layer=<layer>                   }
{..............................................................................}

Function PCB_PlaceRadialDimension(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Dim : IPCB_Dimension;
    Cx, Cy, Radius : Integer;
    LayerStr : String;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    Cx := StrToIntDef(ExtractJsonValue(Params, 'center_x'), 0);
    Cy := StrToIntDef(ExtractJsonValue(Params, 'center_y'), 0);
    Radius := StrToIntDef(ExtractJsonValue(Params, 'radius'), 100);
    LayerStr := ExtractJsonValue(Params, 'layer');
    If LayerStr = '' Then LayerStr := 'TopOverlay';

    PCBServer.PreProcess;
    Try
        Dim := PCBServer.PCBObjectFactory(eDimensionObject, eRadialDimension, eCreate_Default);
        If Dim = Nil Then
        Begin
            PCBServer.PostProcess;
            Result := BuildErrorResponse(RequestId, 'CREATE_FAILED', 'Failed to create radial dimension');
            Exit;
        End;
        Dim.Layer := GetLayerFromString(LayerStr);
        Dim.DimensionKind := eRadialDimension;
        Dim.X1Location := MilsToCoord(Cx);
        Dim.Y1Location := MilsToCoord(Cy);
        Dim.Size := MilsToCoord(Radius);
        Dim.TextX := MilsToCoord(Cx + Radius);
        Dim.TextY := MilsToCoord(Cy + Radius);
        Board.AddPCBObject(Dim);
        PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
            PCBM_BoardRegisteration, Dim.I_ObjectAddress);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"placed":true,"kind":"radial","center_x":' + IntToStr(Cx)
        + ',"center_y":' + IntToStr(Cy) + ',"radius":' + IntToStr(Radius)
        + ',"layer":"' + EscapeJsonString(LayerStr) + '"}');
End;

{..............................................................................}
{ PCB_PlaceEmbeddedBoard - Place an IPCB_EmbeddedBoard array (paneling).       }
{ The embedded-board primitive is a grid of child-PCB copies, used for panel  }
{ designs and multi-up arrays. Spacing values are in mils.                    }
{ Params: x, y (bottom-left corner, mils), child_path (full path to the child }
{         .PcbDoc), rows, cols, row_spacing_mils, col_spacing_mils,           }
{         mirror (true/false), layer (default TopLayer).                      }
{..............................................................................}

Function PCB_PlaceEmbeddedBoard(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Emb : IPCB_Primitive;
    ChildPath, LayerStr, MirrorStr : String;
    X, Y, Rows, Cols, RowSpace, ColSpace : Integer;
    TargetLayer : TLayer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    ChildPath := ExtractJsonValue(Params, 'child_path');
    If ChildPath = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'MISSING_PARAM', 'child_path is required');
        Exit;
    End;
    ChildPath := StringReplace(ChildPath, '\\', '\', -1);

    X := StrToIntDef(ExtractJsonValue(Params, 'x'), 0);
    Y := StrToIntDef(ExtractJsonValue(Params, 'y'), 0);
    Rows := StrToIntDef(ExtractJsonValue(Params, 'rows'), 1);
    Cols := StrToIntDef(ExtractJsonValue(Params, 'cols'), 1);
    RowSpace := StrToIntDef(ExtractJsonValue(Params, 'row_spacing_mils'), 0);
    ColSpace := StrToIntDef(ExtractJsonValue(Params, 'col_spacing_mils'), 0);
    LayerStr := ExtractJsonValue(Params, 'layer');
    MirrorStr := LowerCase(ExtractJsonValue(Params, 'mirror'));

    If LayerStr <> '' Then
        TargetLayer := GetLayerFromString(LayerStr)
    Else
        TargetLayer := eTopLayer;

    Emb := PCBServer.PCBObjectFactory(eEmbeddedBoardObject, eNoDimension, eCreate_Default);
    If Emb = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'CREATE_FAILED', 'Failed to create embedded board');
        Exit;
    End;

    PCBServer.PreProcess;
    Try
        Try Emb.Layer := TargetLayer; Except End;
        Try Emb.XLocation := MilsToCoord(X); Except End;
        Try Emb.YLocation := MilsToCoord(Y); Except End;
        Try Emb.DocumentPath := ChildPath; Except End;
        Try Emb.RowCount := Rows; Except End;
        Try Emb.ColCount := Cols; Except End;
        If RowSpace > 0 Then
            Try Emb.RowSpacing := MilsToCoord(RowSpace); Except End;
        If ColSpace > 0 Then
            Try Emb.ColSpacing := MilsToCoord(ColSpace); Except End;
        If (MirrorStr = 'true') Or (MirrorStr = '1') Then
            Try Emb.MirrorFlag := True; Except End;

        Board.AddPCBObject(Emb);
        PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
            PCBM_BoardRegisteration, Emb.I_ObjectAddress);
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        '{"success":true,"child_path":"' + EscapeJsonString(ChildPath) + '",'
        + '"rows":' + IntToStr(Rows) + ',"cols":' + IntToStr(Cols)
        + ',"x":' + IntToStr(X) + ',"y":' + IntToStr(Y) + '}');
End;

{ PCB_AddTestpointsForNetClass                                                 }
{                                                                              }
{ For each net in a target netclass that does NOT already have a testpoint,   }
{ place a new pad above the board outline with the net assigned and the       }
{ standard IsTestpoint_Top / IsTestpoint_Bottom / IsAssyTestpoint_Top /       }
{ IsAssyTestpoint_Bottom flags set per request. The pad lands in a row above }
{ the board outline ready for the user (or `pcb_move_components`) to drag    }
{ into position.                                                              }
{                                                                              }
{ Net is detected as "already covered" if any pad or via on that net carries }
{ ANY of the four testpoint flags (so DFM tools, fab and assembly testpoint  }
{ vendors all see the existing coverage).                                    }
{                                                                              }
{ Params:                                                                      }
{   net_class            (required)  -- netclass name to scan                  }
{   type                 "smd" or "through_hole" (default "smd")              }
{   pad_size_mils        outer pad size, default 40                            }
{   hole_size_mils       drill diameter, default 20 (only used for through)   }
{   fab_top              "true"/"false", default false                         }
{   fab_bottom           default false                                         }
{   assy_top             default true (most common)                            }
{   assy_bottom          default false                                         }
{   force                default false -- ignore existing, always place        }
{                                                                              }
Function PCB_AddTestpointsForNetClass(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iter, NetIter : IPCB_BoardIterator;
    GrIter : IPCB_GroupIterator;
    NetClassObj : IPCB_ObjectClass;
    Net : IPCB_Net;
    Pad : IPCB_Pad;
    Prim : IPCB_Primitive;
    NetClassName, Kind : String;
    PadSizeMils, HoleSizeMils : Integer;
    FabTop, FabBot, AssyTop, AssyBot, Force : Boolean;
    BoardRect : TCoordRect;
    PosX, PosY, PadSize, HoleSize, StepX : Integer;
    ClassFound, CoveredAlready : Boolean;
    Placed, Skipped : Integer;
    ItemsPlaced, ItemsSkipped, NetName : String;
    FirstPlaced, FirstSkipped : Boolean;
Begin
    Board := PCBServer.GetCurrentPCBBoard;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_BOARD',
            'No PCB document focused');
        Exit;
    End;
    NetClassName := ExtractJsonValue(Params, 'net_class');
    If NetClassName = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'BAD_PARAM',
            'net_class is required');
        Exit;
    End;
    Kind := ExtractJsonValue(Params, 'type');
    If Kind = '' Then Kind := 'smd';
    PadSizeMils  := StrToIntDef(ExtractJsonValue(Params, 'pad_size_mils'), 40);
    HoleSizeMils := StrToIntDef(ExtractJsonValue(Params, 'hole_size_mils'), 20);
    FabTop  := LowerCase(ExtractJsonValue(Params, 'fab_top'))  = 'true';
    FabBot  := LowerCase(ExtractJsonValue(Params, 'fab_bottom')) = 'true';
    AssyTop := LowerCase(ExtractJsonValue(Params, 'assy_top'))  = 'true';
    AssyBot := LowerCase(ExtractJsonValue(Params, 'assy_bottom')) = 'true';
    Force   := LowerCase(ExtractJsonValue(Params, 'force')) = 'true';
    If (Not FabTop) And (Not FabBot) And (Not AssyTop) And (Not AssyBot) Then
        AssyTop := True;

    { Locate the netclass.                                                    }
    NetClassObj := Nil;
    ClassFound := False;
    Iter := Board.BoardIterator_Create;
    Try
        Iter.AddFilter_ObjectSet(MkSet(eClassObject));
        Iter.AddFilter_LayerSet(AllLayers);
        Iter.AddFilter_Method(eProcessAll);
        NetClassObj := Iter.FirstPCBObject;
        While NetClassObj <> Nil Do
        Begin
            Try
                If (NetClassObj.MemberKind = eClassMemberKind_Net)
                   And (NetClassObj.Name = NetClassName) Then
                Begin
                    ClassFound := True;
                    Break;
                End;
            Except End;
            NetClassObj := Iter.NextPCBObject;
        End;
    Finally
        Board.BoardIterator_Destroy(Iter);
    End;

    If Not ClassFound Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NETCLASS_NOT_FOUND',
            'No net class named "' + NetClassName + '"');
        Exit;
    End;

    { Walk all nets, filter to this netclass, and emit a testpoint per       }
    { uncovered net. Layout: row above the board outline, padded.           }
    BoardRect := Board.BoardOutline.BoundingRectangle;
    PadSize := MilsToCoord(PadSizeMils);
    HoleSize := MilsToCoord(HoleSizeMils);
    StepX := PadSize + MilsToCoord(20);
    PosX := BoardRect.Left + (PadSize Div 2);
    PosY := BoardRect.Top + MilsToCoord(60) + (PadSize Div 2);

    Placed := 0;
    Skipped := 0;
    ItemsPlaced := '';
    ItemsSkipped := '';
    FirstPlaced := True;
    FirstSkipped := True;

    PCBServer.PreProcess;
    Try
        NetIter := Board.BoardIterator_Create;
        Try
            NetIter.AddFilter_ObjectSet(MkSet(eNetObject));
            NetIter.AddFilter_LayerSet(AllLayers);
            NetIter.AddFilter_Method(eProcessAll);
            Net := NetIter.FirstPCBObject;
            While Net <> Nil Do
            Begin
                Try
                    If NetClassObj.IsMember(Net) Then
                    Begin
                        NetName := '';
                        Try NetName := Net.Name; Except End;

                        CoveredAlready := False;
                        If Not Force Then
                        Begin
                            GrIter := Net.GroupIterator_Create;
                            Try
                                GrIter.AddFilter_ObjectSet(MkSet(ePadObject, eViaObject));
                                GrIter.AddFilter_AllLayers;
                                Prim := GrIter.FirstPCBObject;
                                While Prim <> Nil Do
                                Begin
                                    Try
                                        If Prim.IsTestpoint_Top
                                           Or Prim.IsTestpoint_Bottom
                                           Or Prim.IsAssyTestpoint_Top
                                           Or Prim.IsAssyTestpoint_Bottom Then
                                            CoveredAlready := True;
                                    Except End;
                                    If CoveredAlready Then Break;
                                    Prim := GrIter.NextPCBObject;
                                End;
                            Finally
                                Net.GroupIterator_Destroy(GrIter);
                            End;
                        End;

                        If CoveredAlready Then
                        Begin
                            Inc(Skipped);
                            If Not FirstSkipped Then ItemsSkipped := ItemsSkipped + ',';
                            FirstSkipped := False;
                            ItemsSkipped := ItemsSkipped + '"' +
                                EscapeJsonString(NetName) + '"';
                        End
                        Else
                        Begin
                            Pad := PCBServer.PCBObjectFactory(ePadObject,
                                eNoDimension, eCreate_Default);
                            If Pad <> Nil Then
                            Begin
                                Pad.Mode := ePadMode_Simple;
                                Pad.X := PosX;
                                Pad.Y := PosY;
                                Pad.TopXSize := PadSize;
                                Pad.TopYSize := PadSize;
                                Pad.TopShape := eRounded;
                                If LowerCase(Kind) = 'through_hole' Then
                                Begin
                                    Pad.Layer := eMultiLayer;
                                    Pad.HoleSize := HoleSize;
                                End
                                Else
                                Begin
                                    Pad.Layer := eTopLayer;
                                    Pad.HoleSize := 0;
                                End;
                                Pad.Name := 'TP_' + NetName;
                                Pad.Net := Net;
                                Try Pad.IsTestpoint_Top := FabTop; Except End;
                                Try Pad.IsTestpoint_Bottom := FabBot; Except End;
                                Try Pad.IsAssyTestpoint_Top := AssyTop; Except End;
                                Try Pad.IsAssyTestpoint_Bottom := AssyBot; Except End;
                                Board.AddPCBObject(Pad);

                                Inc(Placed);
                                If Not FirstPlaced Then ItemsPlaced := ItemsPlaced + ',';
                                FirstPlaced := False;
                                ItemsPlaced := ItemsPlaced + '"' +
                                    EscapeJsonString(NetName) + '"';
                                PosX := PosX + StepX;
                            End;
                        End;
                    End;
                Except End;
                Net := NetIter.NextPCBObject;
            End;
        Finally
            Board.BoardIterator_Destroy(NetIter);
        End;
    Finally
        PCBServer.PostProcess;
    End;

    If Placed > 0 Then SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        JsonObj(
            JsonStr('net_class', NetClassName) + ',' +
            JsonStr('type', Kind) + ',' +
            JsonInt('placed', Placed) + ',' +
            JsonInt('skipped_already_covered', Skipped) + ',' +
            JsonRaw('placed_nets', '[' + ItemsPlaced + ']') + ',' +
            JsonRaw('skipped_nets', '[' + ItemsSkipped + ']')
        ));
End;


{ PCB_MakePasteGrid                                                            }
{                                                                              }
{ Split a single pad's solder-paste opening into a grid of smaller fills.     }
{ The classic use case is the central thermal pad on a QFN / DFN / QFP --     }
{ a full-area paste opening makes the IC "swim" sideways during reflow as    }
{ the molten solder pool reduces friction. Splitting the opening into        }
{ smaller squares totalling ~50-75% coverage gives the IC something to       }
{ bond to while letting flux gases escape.                                   }
{                                                                              }
{ Algorithm:                                                                  }
{   - Locate the target pad by designator + pad name (e.g. "U5" pad "9").    }
{   - Suppress the existing full-area paste by setting PasteMaskExpansion    }
{     negative (eCacheManual override on the pad cache).                     }
{   - Compute grid layout: how many grid_size x grid_size squares fit with   }
{     at least min_gap between them.                                          }
{   - If coverage < min_coverage_pct, bump grid_size and retry.              }
{   - Place each square as a Fill on the appropriate paste layer (top or    }
{     bottom, derived from the pad's own layer).                              }
{                                                                              }
{ All input dimensions are mils; coverage is a 0-100 percent.                  }
Function PCB_MakePasteGrid(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iter : IPCB_BoardIterator;
    Comp : IPCB_Component;
    PadIter : IPCB_GroupIterator;
    Pad : IPCB_Pad;
    Cache : TPadCache;
    Designator, PadName, CompDes, PadId : String;
    MinGridSizeMils, MinGapMils : Integer;
    MinCoverPct : Double;
    GridSize, MinGap : Integer;
    GridXCnt, GridYCnt : Integer;
    GridXPad, GridYPad : Integer;
    PadW, PadH, PadCenterX, PadCenterY : Integer;
    PadAreaMils2, PasteAreaMils2 : Int64;
    PctCover : Double;
    Found : Boolean;
    PasteLayer : TLayer;
    Fill : IPCB_Fill;
    I, J, Placed : Integer;
    PadRot : Double;
    FillX1, FillY1, FillX2, FillY2 : Integer;
Begin
    Board := PCBServer.GetCurrentPCBBoard;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_BOARD',
            'No PCB document focused');
        Exit;
    End;
    Designator := ExtractJsonValue(Params, 'designator');
    PadName    := ExtractJsonValue(Params, 'pad_name');
    MinGridSizeMils := StrToIntDef(ExtractJsonValue(Params, 'min_grid_size_mils'), 15);
    MinGapMils      := StrToIntDef(ExtractJsonValue(Params, 'min_gap_mils'), 5);
    MinCoverPct     := StrToFloatDef(ExtractJsonValue(Params, 'min_coverage_pct'), 60.0);
    If Designator = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'BAD_PARAM',
            'designator is required');
        Exit;
    End;
    If PadName = '' Then
    Begin
        Result := BuildErrorResponse(RequestId, 'BAD_PARAM',
            'pad_name is required (use e.g. "0" for QFN exposed pad)');
        Exit;
    End;

    Pad := Nil;
    Found := False;
    Iter := Board.BoardIterator_Create;
    Try
        Iter.AddFilter_ObjectSet(MkSet(eComponentObject));
        Iter.AddFilter_LayerSet(AllLayers);
        Iter.AddFilter_Method(eProcessAll);
        Comp := Iter.FirstPCBObject;
        While (Comp <> Nil) And (Not Found) Do
        Begin
            CompDes := '';
            Try CompDes := Comp.Name.Text; Except End;
            If CompDes = Designator Then
            Begin
                PadIter := Comp.GroupIterator_Create;
                Try
                    PadIter.AddFilter_ObjectSet(MkSet(ePadObject));
                    Pad := PadIter.FirstPCBObject;
                    While (Pad <> Nil) And (Not Found) Do
                    Begin
                        PadId := '';
                        Try PadId := Pad.Name; Except End;
                        If PadId = PadName Then Found := True
                        Else Pad := PadIter.NextPCBObject;
                    End;
                Finally
                    Comp.GroupIterator_Destroy(PadIter);
                End;
            End;
            If Not Found Then Comp := Iter.NextPCBObject;
        End;
    Finally
        Board.BoardIterator_Destroy(Iter);
    End;

    If (Not Found) Or (Pad = Nil) Then
    Begin
        Result := BuildErrorResponse(RequestId, 'PAD_NOT_FOUND',
            'No pad "' + PadName + '" on component "' + Designator + '"');
        Exit;
    End;

    { Pick top/bottom paste based on pad layer.                              }
    If Pad.Layer = eBottomLayer Then PasteLayer := eBottomPaste
    Else PasteLayer := eTopPaste;

    { Pad rotation interpretation: when the component is rotated 90/270 the }
    { pad's X dimension actually maps to physical height -- swap.            }
    PadRot := 0;
    Try PadRot := Pad.Rotation; Except End;
    If (Abs(PadRot - 90) < 1) Or (Abs(PadRot - 270) < 1) Then
    Begin
        PadW := Pad.TopYSize;
        PadH := Pad.TopXSize;
    End
    Else
    Begin
        PadW := Pad.TopXSize;
        PadH := Pad.TopYSize;
    End;

    PadCenterX := Pad.X;
    PadCenterY := Pad.Y;
    PadAreaMils2 := CoordToMils(PadW) * CoordToMils(PadH);
    GridSize := MinGridSizeMils;
    MinGap := MinGapMils;

    PctCover := 0;
    GridXPad := 0;
    GridYPad := 0;
    GridXCnt := 0;
    GridYCnt := 0;

    { Outer loop -- bump grid size until coverage % satisfied.               }
    While PctCover < MinCoverPct Do
    Begin
        GridXCnt := Trunc(CoordToMils(PadW) / GridSize);
        GridYCnt := Trunc(CoordToMils(PadH) / GridSize);

        { Inner loop -- shrink grid count until gap is ≥ MinGap.             }
        GridXPad := 0; GridYPad := 0;
        While (GridXPad < MinGap) Or (GridYPad < MinGap) Do
        Begin
            If GridXCnt <= 0 Then Break;
            If GridYCnt <= 0 Then Break;
            GridXPad := (CoordToMils(PadW) - (GridXCnt * GridSize)) Div (GridXCnt + 1);
            GridYPad := (CoordToMils(PadH) - (GridYCnt * GridSize)) Div (GridYCnt + 1);
            If GridXPad < MinGap Then Dec(GridXCnt);
            If GridYPad < MinGap Then Dec(GridYCnt);
        End;

        If (GridXCnt <= 0) Or (GridYCnt <= 0) Then
        Begin
            Result := BuildErrorResponse(RequestId, 'INFEASIBLE',
                'Pad too small to fit grid at min_grid_size=' + IntToStr(MinGridSizeMils) +
                ' / min_gap=' + IntToStr(MinGapMils));
            Exit;
        End;

        PasteAreaMils2 := GridXCnt * GridYCnt * GridSize * GridSize;
        PctCover := (PasteAreaMils2 * 100.0) / PadAreaMils2;
        If PctCover < MinCoverPct Then GridSize := GridSize + 5;
        { Safety -- stop if a single grid square fills the whole pad.        }
        If GridSize >= CoordToMils(PadW) Then Break;
        If GridSize >= CoordToMils(PadH) Then Break;
    End;

    PCBServer.PreProcess;
    Try
        { Suppress the existing full-area paste opening on the pad.          }
        Cache := Pad.GetState_Cache;
        Try
            Cache.PasteMaskExpansionValid := eCacheManual;
            If PadW > PadH Then Cache.PasteMaskExpansion := -PadW
            Else Cache.PasteMaskExpansion := -PadH;
            Pad.SetState_Cache := Cache;
        Except End;

        Placed := 0;
        For I := 0 To GridXCnt - 1 Do
        Begin
            For J := 0 To GridYCnt - 1 Do
            Begin
                FillX1 := PadCenterX - (PadW Div 2)
                          + MilsToCoord(GridXPad * (I + 1) + I * GridSize);
                FillX2 := FillX1 + MilsToCoord(GridSize);
                FillY1 := PadCenterY - (PadH Div 2)
                          + MilsToCoord(GridYPad * (J + 1) + J * GridSize);
                FillY2 := FillY1 + MilsToCoord(GridSize);

                Fill := PCBServer.PCBObjectFactory(eFillObject, eNoDimension, eCreate_Default);
                Fill.X1Location := FillX1;
                Fill.Y1Location := FillY1;
                Fill.X2Location := FillX2;
                Fill.Y2Location := FillY2;
                Fill.Layer := PasteLayer;
                Fill.Rotation := 0;
                Board.AddPCBObject(Fill);
                Inc(Placed);
            End;
        End;
    Finally
        PCBServer.PostProcess;
    End;

    SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        JsonObj(
            JsonStr('designator', Designator) + ',' +
            JsonStr('pad_name', PadName) + ',' +
            JsonInt('grid_x', GridXCnt) + ',' +
            JsonInt('grid_y', GridYCnt) + ',' +
            JsonInt('grid_size_mils', GridSize) + ',' +
            JsonInt('gap_x_mils', GridXPad) + ',' +
            JsonInt('gap_y_mils', GridYPad) + ',' +
            JsonInt('fills_placed', Placed) + ',' +
            JsonFloat('coverage_pct', PctCover)
        ));
End;


{ PCB_GetDifferentialPairs                                                     }
{                                                                              }
{ Enumerate every IPCB_DifferentialPair on the active PCB and report per-pair }
{ length statistics. Length mismatch between the two halves of a diff pair is }
{ one of the most common high-speed routing bugs -- transceivers spec a max  }
{ skew (USB: < 150 mils, HDMI: < 200 mils, MIPI: < 5 mils for high-rate D-PHY,}
{ PCIe: < 5 mils within a lane). Catching it pre-fab saves a respin.         }
{                                                                              }
{ Per-pair JSON: name, positive_net, negative_net, pos_length_mils,           }
{ neg_length_mils, skew_mils (absolute difference), both_routed (boolean --   }
{ false means one half is still ratsnest-only).                              }
{                                                                              }
{ Uses the PCB API IPCB_DifferentialPair interface.                           }
Function PCB_GetDifferentialPairs(Params : String; RequestId : String) : String;

    Function NetLengthMils(Net : IPCB_Net) : Double;
    Var
        GrIter : IPCB_GroupIterator;
        Prim : IPCB_Primitive;
        Track : IPCB_Track;
        Arc : IPCB_Arc;
        Total : Double;
        Dx, Dy : Double;
        SweepDeg : Double;
    Begin
        Total := 0;
        If Net = Nil Then
        Begin
            Result := 0;
            Exit;
        End;
        GrIter := Net.GroupIterator_Create;
        Try
            GrIter.AddFilter_ObjectSet(MkSet(eTrackObject, eArcObject));
            GrIter.AddFilter_LayerSet(LayerSet.SignalLayers);
            Prim := GrIter.FirstPCBObject;
            While Prim <> Nil Do
            Begin
                Try
                    If Prim.ObjectId = eTrackObject Then
                    Begin
                        Track := Prim;
                        Dx := CoordToMils(Track.X2 - Track.X1);
                        Dy := CoordToMils(Track.Y2 - Track.Y1);
                        Total := Total + Sqrt(Dx * Dx + Dy * Dy);
                    End
                    Else If Prim.ObjectId = eArcObject Then
                    Begin
                        Arc := Prim;
                        SweepDeg := Abs(Arc.EndAngle - Arc.StartAngle);
                        If SweepDeg > 360 Then SweepDeg := SweepDeg - 360;
                        Total := Total + (SweepDeg / 360.0) * 2.0 * 3.14159265358979 *
                                          CoordToMils(Arc.Radius);
                    End;
                Except End;
                Prim := GrIter.NextPCBObject;
            End;
        Finally
            Net.GroupIterator_Destroy(GrIter);
        End;
        Result := Total;
    End;

Var
    Board : IPCB_Board;
    Iter : IPCB_BoardIterator;
    Pair : IPCB_DifferentialPair;
    PosLen, NegLen, Skew : Double;
    PosName, NegName, PairName : String;
    Items, Entry : String;
    First, BothRouted : Boolean;
    Count : Integer;
Begin
    Board := PCBServer.GetCurrentPCBBoard;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_BOARD',
            'No PCB document focused');
        Exit;
    End;

    Items := '';
    First := True;
    Count := 0;

    Iter := Board.BoardIterator_Create;
    Try
        Iter.AddFilter_ObjectSet(MkSet(eDifferentialPairObject));
        Iter.AddFilter_LayerSet(AllLayers);
        Iter.AddFilter_Method(eProcessAll);
        Pair := Iter.FirstPCBObject;
        While Pair <> Nil Do
        Begin
            Try
                Inc(Count);
                PairName := '';
                Try PairName := Pair.Name; Except End;
                PosName := '';
                NegName := '';
                Try
                    If Pair.PositiveNet <> Nil Then PosName := Pair.PositiveNet.Name;
                Except End;
                Try
                    If Pair.NegativeNet <> Nil Then NegName := Pair.NegativeNet.Name;
                Except End;
                PosLen := NetLengthMils(Pair.PositiveNet);
                NegLen := NetLengthMils(Pair.NegativeNet);
                Skew := Abs(PosLen - NegLen);
                BothRouted := (PosLen > 0) And (NegLen > 0);

                If Not First Then Items := Items + ',';
                First := False;
                Entry :=
                    JsonStr('name', PairName) + ',' +
                    JsonStr('positive_net', PosName) + ',' +
                    JsonStr('negative_net', NegName) + ',' +
                    JsonFloat('pos_length_mils', PosLen) + ',' +
                    JsonFloat('neg_length_mils', NegLen) + ',' +
                    JsonFloat('skew_mils', Skew) + ',' +
                    JsonBool('both_routed', BothRouted);
                Items := Items + JsonObj(Entry);
            Except End;
            Pair := Iter.NextPCBObject;
        End;
    Finally
        Board.BoardIterator_Destroy(Iter);
    End;

    Result := BuildSuccessResponse(RequestId,
        JsonObj(
            JsonInt('count', Count) + ',' +
            JsonRaw('pairs', '[' + Items + ']')
        ));
End;


{ PCB_ClearSourceFootprintLibrary                                              }
{                                                                              }
{ Walk components on the board and clear their SourceFootprintLibrary         }
{ property. When a project was created from an Integrated Library, each      }
{ placed component remembers WHICH library it came from. If the user later   }
{ consolidates / renames / moves that library, ECO and Update-From-Lib       }
{ start failing with "library not found" because the component still         }
{ points at the old path. Clearing SourceFootprintLibrary unpins it so       }
{ Altium re-matches by library-reference name from whatever's currently in   }
{ Available Libraries.                                                        }
{                                                                              }
{ Optional designator_filter (pipe-delimited list) restricts the operation;  }
{ omit / empty to clear all components.                                       }
{                                                                              }
{ Clears the source footprint library; supports an optional name filter.     }
Function PCB_ClearSourceFootprintLibrary(Params, RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iter : IPCB_BoardIterator;
    Comp : IPCB_Component;
    Filter, CompDes, OldSrc : String;
    Total, Cleared : Integer;
    UseFilter, Match : Boolean;
Begin
    Board := PCBServer.GetCurrentPCBBoard;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_BOARD',
            'No PCB document focused');
        Exit;
    End;
    Filter := ExtractJsonValue(Params, 'designator_filter');
    UseFilter := Filter <> '';

    Total := 0;
    Cleared := 0;

    PCBServer.PreProcess;
    Try
        Iter := Board.BoardIterator_Create;
        Try
            Iter.AddFilter_ObjectSet(MkSet(eComponentObject));
            Iter.AddFilter_LayerSet(MkSet(eTopLayer, eBottomLayer));
            Iter.AddFilter_Method(eProcessAll);
            Comp := Iter.FirstPCBObject;
            While Comp <> Nil Do
            Begin
                Try
                    Inc(Total);
                    Match := True;
                    If UseFilter Then
                    Begin
                        CompDes := '';
                        Try CompDes := Comp.Name.Text; Except End;
                        { Pipe-delimited match: "|U1|U5|" semantics; pad      }
                        { both sides so partial-name collisions don't fire.   }
                        Match := Pos('|' + CompDes + '|',
                                      '|' + Filter + '|') > 0;
                    End;
                    If Match Then
                    Begin
                        OldSrc := '';
                        Try OldSrc := Comp.SourceFootprintLibrary; Except End;
                        If OldSrc <> '' Then
                        Begin
                            Try Comp.SourceFootprintLibrary := ''; Except End;
                            Inc(Cleared);
                        End;
                    End;
                Except End;
                Comp := Iter.NextPCBObject;
            End;
        Finally
            Board.BoardIterator_Destroy(Iter);
        End;
    Finally
        PCBServer.PostProcess;
    End;

    If Cleared > 0 Then SaveDocByPath(Board.FileName);

    Result := BuildSuccessResponse(RequestId,
        JsonObj(
            JsonInt('total', Total) + ',' +
            JsonInt('cleared', Cleared)
        ));
End;


{ PCB_GetFabStats                                                              }
{                                                                              }
{ DFM (Design For Manufacturing) summary -- the numbers fab houses ask for    }
{ on their quote forms. The agent can fetch this once before sending          }
{ gerbers out and surface red flags (sub-4mil annular ring, sub-5mil tracks,  }
{ excessive distinct drill sizes) to the user.                                }
{                                                                              }
{ Computed metrics:                                                            }
{   - board_width_mm, board_height_mm, board_area_mm2                         }
{   - num_copper_layers                                                       }
{   - vias_total, vias_through, vias_blind, vias_buried                       }
{   - pads_plated, pads_unplated, pads_slotted                                }
{   - min_annular_ring_mils (across all vias + pads with plated holes)        }
{   - min_track_width_mils (across all tracks on copper layers)               }
{   - smallest_hole_mils, largest_hole_mils, distinct_hole_count              }
{                                                                              }
{ Board statistics condensed into a single IPC call.                         }
Function PCB_GetFabStats(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    LayerStack : IPCB_LayerStack;
    LayerObj : IPCB_LayerObject;
    Iter : IPCB_BoardIterator;
    Obj : IPCB_Primitive;
    Via : IPCB_Via;
    Pad : IPCB_Pad;
    Track : IPCB_Track;
    NumCopper : Integer;
    ViasTotal, ViasThrough, ViasBlind, ViasBuried : Integer;
    PadsPlated, PadsUnplated, PadsSlotted : Integer;
    MinAnnularRing, MinTrackWidth : Integer;
    SmallestHole, LargestHole : Integer;
    AnnRing, Width : Integer;
    DistinctHolesList, HoleKey : String;
    DistinctHoleCount : Integer;
    HoleSize : Integer;
    BoardW, BoardH, BoardA : Double;
    R : TCoordRect;
    HasAny : Boolean;
Begin
    Board := PCBServer.GetCurrentPCBBoard;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_BOARD',
            'No PCB document focused');
        Exit;
    End;

    { Bounding box -- use the board outline rect.                            }
    Try
        R := Board.BoardOutline.BoundingRectangle;
        BoardW := CoordToMM(R.Right - R.Left);
        BoardH := CoordToMM(R.Top - R.Bottom);
        BoardA := BoardW * BoardH;
    Except
        BoardW := 0;
        BoardH := 0;
        BoardA := 0;
    End;

    { Layer count -- walk the IPCB_LayerStack and count signal layers.       }
    NumCopper := 0;
    Try
        LayerStack := Board.LayerStack_V7;
        LayerObj := LayerStack.FirstLayer;
        While LayerObj <> Nil Do
        Begin
            Try
                If LayerObj.LayerID >= 1 Then
                    If (LayerObj.LayerID = eTopLayer)
                       Or (LayerObj.LayerID = eBottomLayer)
                       Or ((LayerObj.LayerID >= eMidLayer1)
                            And (LayerObj.LayerID <= eMidLayer30)) Then
                        Inc(NumCopper);
            Except End;
            LayerObj := LayerStack.NextLayer(LayerObj);
        End;
    Except End;

    ViasTotal := 0; ViasThrough := 0; ViasBlind := 0; ViasBuried := 0;
    PadsPlated := 0; PadsUnplated := 0; PadsSlotted := 0;
    MinAnnularRing := MaxInt;
    MinTrackWidth := MaxInt;
    SmallestHole := MaxInt;
    LargestHole := 0;
    DistinctHolesList := '|';
    DistinctHoleCount := 0;
    HasAny := False;

    Iter := Board.BoardIterator_Create;
    Try
        Iter.AddFilter_ObjectSet(MkSet(eViaObject));
        Iter.AddFilter_LayerSet(AllLayers);
        Iter.AddFilter_Method(eProcessAll);
        Obj := Iter.FirstPCBObject;
        While Obj <> Nil Do
        Begin
            Try
                Via := Obj;
                Inc(ViasTotal);
                { Classify by start/stop layer -- through if Top to Bottom; }
                { blind if one side is outer (Top or Bottom) but not both;  }
                { buried if both endpoints are inner layers.                 }
                If (Via.StartLayer = eTopLayer) And (Via.StopLayer = eBottomLayer) Then
                    Inc(ViasThrough)
                Else If (Via.StartLayer = eTopLayer) Or (Via.StartLayer = eBottomLayer)
                     Or (Via.StopLayer = eTopLayer) Or (Via.StopLayer = eBottomLayer) Then
                    Inc(ViasBlind)
                Else
                    Inc(ViasBuried);

                HoleSize := Via.HoleSize;
                If HoleSize > 0 Then
                Begin
                    HasAny := True;
                    AnnRing := (Via.Size - HoleSize) Div 2;
                    If AnnRing < MinAnnularRing Then MinAnnularRing := AnnRing;
                    If HoleSize < SmallestHole Then SmallestHole := HoleSize;
                    If HoleSize > LargestHole Then LargestHole := HoleSize;
                    HoleKey := '|' + IntToStr(HoleSize) + '|';
                    If Pos(HoleKey, DistinctHolesList) = 0 Then
                    Begin
                        DistinctHolesList := DistinctHolesList + IntToStr(HoleSize) + '|';
                        Inc(DistinctHoleCount);
                    End;
                End;
            Except End;
            Obj := Iter.NextPCBObject;
        End;
    Finally
        Board.BoardIterator_Destroy(Iter);
    End;

    { Pads -- track plated vs unplated, slotted vs circular.                 }
    Iter := Board.BoardIterator_Create;
    Try
        Iter.AddFilter_ObjectSet(MkSet(ePadObject));
        Iter.AddFilter_LayerSet(AllLayers);
        Iter.AddFilter_Method(eProcessAll);
        Obj := Iter.FirstPCBObject;
        While Obj <> Nil Do
        Begin
            Try
                Pad := Obj;
                HoleSize := Pad.HoleSize;
                If HoleSize > 0 Then
                Begin
                    HasAny := True;
                    If Pad.Plated Then
                    Begin
                        Inc(PadsPlated);
                        { Pad annular ring is (X|Y - HoleSize) / 2 -- pick }
                        { the tighter of X/Y dimensions.                    }
                        AnnRing := (Pad.TopXSize - HoleSize) Div 2;
                        If ((Pad.TopYSize - HoleSize) Div 2) < AnnRing Then
                            AnnRing := (Pad.TopYSize - HoleSize) Div 2;
                        If AnnRing < MinAnnularRing Then MinAnnularRing := AnnRing;
                    End
                    Else
                        Inc(PadsUnplated);
                    { Slotted pads have HoleType <> eRoundHole.              }
                    Try
                        If Pad.HoleType <> eRoundHole Then Inc(PadsSlotted);
                    Except End;
                    If HoleSize < SmallestHole Then SmallestHole := HoleSize;
                    If HoleSize > LargestHole Then LargestHole := HoleSize;
                    HoleKey := '|' + IntToStr(HoleSize) + '|';
                    If Pos(HoleKey, DistinctHolesList) = 0 Then
                    Begin
                        DistinctHolesList := DistinctHolesList + IntToStr(HoleSize) + '|';
                        Inc(DistinctHoleCount);
                    End;
                End;
            Except End;
            Obj := Iter.NextPCBObject;
        End;
    Finally
        Board.BoardIterator_Destroy(Iter);
    End;

    { Min track width -- copper layers only.                                  }
    Iter := Board.BoardIterator_Create;
    Try
        Iter.AddFilter_ObjectSet(MkSet(eTrackObject));
        Iter.AddFilter_LayerSet(SignalLayers);
        Iter.AddFilter_Method(eProcessAll);
        Obj := Iter.FirstPCBObject;
        While Obj <> Nil Do
        Begin
            Try
                Track := Obj;
                Width := Track.Width;
                If (Width > 0) And (Width < MinTrackWidth) Then
                    MinTrackWidth := Width;
            Except End;
            Obj := Iter.NextPCBObject;
        End;
    Finally
        Board.BoardIterator_Destroy(Iter);
    End;

    If Not HasAny Then
    Begin
        MinAnnularRing := 0;
        SmallestHole := 0;
    End;
    If MinTrackWidth = MaxInt Then MinTrackWidth := 0;

    Result := BuildSuccessResponse(RequestId,
        JsonObj(
            JsonFloat('board_width_mm', BoardW) + ',' +
            JsonFloat('board_height_mm', BoardH) + ',' +
            JsonFloat('board_area_mm2', BoardA) + ',' +
            JsonInt('num_copper_layers', NumCopper) + ',' +
            JsonInt('vias_total', ViasTotal) + ',' +
            JsonInt('vias_through', ViasThrough) + ',' +
            JsonInt('vias_blind', ViasBlind) + ',' +
            JsonInt('vias_buried', ViasBuried) + ',' +
            JsonInt('pads_plated', PadsPlated) + ',' +
            JsonInt('pads_unplated', PadsUnplated) + ',' +
            JsonInt('pads_slotted', PadsSlotted) + ',' +
            JsonInt('min_annular_ring_mils', CoordToMils(MinAnnularRing)) + ',' +
            JsonInt('min_track_width_mils', CoordToMils(MinTrackWidth)) + ',' +
            JsonInt('smallest_hole_mils', CoordToMils(SmallestHole)) + ',' +
            JsonInt('largest_hole_mils', CoordToMils(LargestHole)) + ',' +
            JsonInt('distinct_hole_count', DistinctHoleCount)
        ));
End;


{..............................................................................}
{ PCB_FilletCorners                                                            }
{                                                                              }
{ Round acute track-to-track joins by replacing the shared corner with a      }
{ tangent arc and shortening each track back to its tangent point.           }
{ Interactive fillet tools make the user click corners on the canvas.        }
{ This version is                                                             }
{ agent-shaped: it walks the board, finds every same-net corner whose        }
{ interior angle is below the threshold, and either reports them (dry_run)   }
{ or applies the fillet in one undo group.                                    }
{                                                                              }
{ Params (all JSON strings):                                                   }
{   net           -- optional, restrict to corners on this net                }
{   radius_mils   -- arc radius, default 10                                   }
{   min_angle_deg -- only fillet corners with interior angle < this; default  }
{                    90 (sharp / right-angle corners and worse)               }
{   dry_run       -- "true" (default) returns the list of WOULD-fillet        }
{                    corners without mutating; "false" applies the change    }
{                                                                              }
{ Geometry: for a corner where two tracks share endpoint P and head off in   }
{ directions u and v (unit vectors away from P), with interior angle theta   }
{ between them, the fillet arc has:                                           }
{   tangent_dist (along each track from P) = R / tan(theta / 2)               }
{   center_dist  (along the bisector from P) = R / sin(theta / 2)             }
{   tangent points: T1 = P + tangent_dist * u, T2 = P + tangent_dist * v      }
{   arc center C = P + center_dist * bisector_unit                            }
{                                                                              }
{ Each tangent point sits R away from C and the radius vector C->T is        }
{ perpendicular to the corresponding track direction.                        }
{                                                                              }
{ NOTE: This handler has NOT been validated against a live Altium session.   }
{ Defensive Try/Except wraps every Altium API touch. Recommend running in    }
{ dry_run mode first, eyeballing the items[], and only flipping dry_run off  }
{ on a board you have backed up.                                              }
{..............................................................................}

Function PCB_FilletCorners(Params : String; RequestId : String) : String;
{ DelphiScript does NOT support typed constants (Const cPi : Double = X). }
{ Use an untyped Const -- the literal carries its own Double precision   }
{ when assigned to a Double receiver, which is how cPi is used below.    }
Const
    cPi = 3.14159265358979;
Var
    Board : IPCB_Board;
    Iter, SpatIter : IPCB_BoardIterator;
    Track, Other : IPCB_Track;
    Obj : IPCB_Primitive;
    Arc : IPCB_Arc;
    NetFilter, DryStr, RadStr, AngStr : String;
    DryRun : Boolean;
    RadiusMils : Integer;
    MinAngleDeg : Double;
    Tol, Endpoint : Integer;
    Filleted, Skipped, MaxItems : Integer;
    PX, PY : Integer;
    OX, OY : Integer;
    V1X, V1Y, V2X, V2Y : Double;
    L1, L2, Dot, CosTheta, ThetaRad, ThetaDeg : Double;
    U1X, U1Y, U2X, U2Y : Double;
    BX, BY, BLen : Double;
    TangentDist, CenterDist : Double;
    T1X, T1Y, T2X, T2Y : Double;
    CX, CY : Double;
    R : Double;
    StartAngleDeg, EndAngleDeg : Double;
    A1, A2 : Double;
    ItemsJson, EntryJson, NetName, LayerName : String;
    First : Boolean;
    TrackAddr, OtherAddr : String;
    OtherEndpoint : Integer;
    SaveNeeded : Boolean;
    ApplyOk : Boolean;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB',
            'No PCB document is active');
        Exit;
    End;

    NetFilter := ExtractJsonValue(Params, 'net');
    DryStr := LowerCase(ExtractJsonValue(Params, 'dry_run'));
    RadStr := ExtractJsonValue(Params, 'radius_mils');
    AngStr := ExtractJsonValue(Params, 'min_angle_deg');

    { Default dry_run = TRUE so an agent cannot rewrite the board layout by  }
    { accident. The caller has to pass dry_run="false" to actually mutate.   }
    If DryStr = '' Then DryRun := True
    Else DryRun := (DryStr = 'true') Or (DryStr = '1');

    RadiusMils := StrToIntDef(RadStr, 10);
    If RadiusMils <= 0 Then RadiusMils := 10;
    MinAngleDeg := StrToFloatDef(AngStr, 90.0);
    If MinAngleDeg <= 0 Then MinAngleDeg := 90.0;
    If MinAngleDeg >= 180 Then MinAngleDeg := 179.9;

    R := MilsToCoord(RadiusMils);
    Tol := MilsToCoord(1);
    Filleted := 0;
    Skipped := 0;
    MaxItems := 200;
    ItemsJson := '';
    First := True;
    SaveNeeded := False;

    If Not DryRun Then PCBServer.PreProcess;
    Try
        Iter := Nil;
        Try
            Iter := Board.BoardIterator_Create;
        Except End;
        If Iter = Nil Then
        Begin
            Result := BuildErrorResponse(RequestId, 'ITER_FAILED',
                'Could not create board iterator');
            Exit;
        End;

        Try
            Iter.AddFilter_ObjectSet(MkSet(eTrackObject));
            Iter.AddFilter_LayerSet(LayerSet.SignalLayers);
            Iter.AddFilter_Method(eProcessAll);

            Track := Iter.FirstPCBObject;
            While (Track <> Nil) And (Filleted + Skipped < MaxItems) Do
            Begin
                { Optional net filter. Net handling is wrapped because    }
                { Track.Net may be Nil on free tracks.                      }
                If NetFilter <> '' Then
                Begin
                    Try
                        If (Track.Net = Nil) Or (Track.Net.Name <> NetFilter) Then
                        Begin
                            Track := Iter.NextPCBObject;
                            Continue;
                        End;
                    Except
                        Track := Iter.NextPCBObject;
                        Continue;
                    End;
                End;

                TrackAddr := '';
                Try TrackAddr := Track.I_ObjectAddress; Except End;

                { Examine both endpoints. We dedupe pairs by only            }
                { processing the corner when this track's address sorts      }
                { lexicographically before the neighbour's. Both halves of  }
                { the pair share the same join geometry, so visiting only   }
                { one half is sufficient.                                    }
                For Endpoint := 1 To 2 Do
                Begin
                    Try
                        If Endpoint = 1 Then
                        Begin
                            PX := Track.X1; PY := Track.Y1;
                            V1X := Track.X2 - PX; V1Y := Track.Y2 - PY;
                        End
                        Else
                        Begin
                            PX := Track.X2; PY := Track.Y2;
                            V1X := Track.X1 - PX; V1Y := Track.Y1 - PY;
                        End;
                        L1 := Sqrt(V1X * V1X + V1Y * V1Y);
                        If L1 < 1 Then Continue;

                        SpatIter := Nil;
                        Try SpatIter := Board.SpatialIterator_Create; Except End;
                        If SpatIter = Nil Then Continue;

                        Try
                            SpatIter.AddFilter_ObjectSet(MkSet(eTrackObject));
                            SpatIter.AddFilter_IPCB_LayerSet(MkSet(Track.Layer));
                            SpatIter.AddFilter_Area(
                                PX - Tol, PY - Tol, PX + Tol, PY + Tol);

                            Obj := SpatIter.FirstPCBObject;
                            While (Obj <> Nil)
                                  And (Filleted + Skipped < MaxItems) Do
                            Begin
                                Try
                                    Other := Obj;
                                    OtherAddr := '';
                                    Try OtherAddr := Other.I_ObjectAddress; Except End;

                                    { Skip self; dedupe pairs by sorting on   }
                                    { I_ObjectAddress so each corner is only }
                                    { visited once.                            }
                                    If (OtherAddr = '') Or (OtherAddr = TrackAddr)
                                       Or (OtherAddr <= TrackAddr) Then
                                    Begin
                                        Obj := SpatIter.NextPCBObject;
                                        Continue;
                                    End;

                                    { Same-net check. Tracks with no net are }
                                    { skipped because we can't safely match. }
                                    Try
                                        If (Track.Net = Nil) Or (Other.Net = Nil)
                                           Or (Track.Net.Name <> Other.Net.Name) Then
                                        Begin
                                            Obj := SpatIter.NextPCBObject;
                                            Continue;
                                        End;
                                    Except
                                        Obj := SpatIter.NextPCBObject;
                                        Continue;
                                    End;

                                    { Identify which of Other's endpoints sits }
                                    { on (PX, PY) and build a vector away from }
                                    { the shared point.                          }
                                    If (Abs(Other.X1 - PX) <= Tol)
                                       And (Abs(Other.Y1 - PY) <= Tol) Then
                                    Begin
                                        OtherEndpoint := 1;
                                        V2X := Other.X2 - PX;
                                        V2Y := Other.Y2 - PY;
                                        OX := Other.X2; OY := Other.Y2;
                                    End
                                    Else If (Abs(Other.X2 - PX) <= Tol)
                                            And (Abs(Other.Y2 - PY) <= Tol) Then
                                    Begin
                                        OtherEndpoint := 2;
                                        V2X := Other.X1 - PX;
                                        V2Y := Other.Y1 - PY;
                                        OX := Other.X1; OY := Other.Y1;
                                    End
                                    Else
                                    Begin
                                        Obj := SpatIter.NextPCBObject;
                                        Continue;
                                    End;

                                    L2 := Sqrt(V2X * V2X + V2Y * V2Y);
                                    If L2 < 1 Then
                                    Begin
                                        Obj := SpatIter.NextPCBObject;
                                        Continue;
                                    End;

                                    { Interior angle from dot product. The   }
                                    { vectors point AWAY from the shared    }
                                    { endpoint, so the dot product directly }
                                    { gives the interior angle.              }
                                    Dot := V1X * V2X + V1Y * V2Y;
                                    CosTheta := Dot / (L1 * L2);
                                    If CosTheta > 1.0 Then CosTheta := 1.0;
                                    If CosTheta < -1.0 Then CosTheta := -1.0;
                                    ThetaRad := ArcCos(CosTheta);
                                    ThetaDeg := ThetaRad * 180.0 / cPi;

                                    { Skip nearly-collinear joins (almost 180 }
                                    { degrees) -- no corner to fillet.        }
                                    If ThetaDeg > 179.0 Then
                                    Begin
                                        Obj := SpatIter.NextPCBObject;
                                        Continue;
                                    End;

                                    { Only act on corners sharper than the   }
                                    { user-specified threshold.               }
                                    If ThetaDeg >= MinAngleDeg Then
                                    Begin
                                        Obj := SpatIter.NextPCBObject;
                                        Continue;
                                    End;

                                    { Unit vectors along each track, away    }
                                    { from the shared endpoint.               }
                                    U1X := V1X / L1; U1Y := V1Y / L1;
                                    U2X := V2X / L2; U2Y := V2Y / L2;

                                    { tan(theta/2) and sin(theta/2). We      }
                                    { already filtered theta == pi above so   }
                                    { sin(theta/2) > 0.                       }
                                    TangentDist := R / Tan(ThetaRad / 2.0);
                                    CenterDist := R / Sin(ThetaRad / 2.0);

                                    { Refuse fillets that would consume more  }
                                    { than the track's remaining length; we   }
                                    { just skip and surface that fact via the }
                                    { Skipped counter.                        }
                                    If (TangentDist >= L1) Or (TangentDist >= L2) Then
                                    Begin
                                        Inc(Skipped);
                                        Obj := SpatIter.NextPCBObject;
                                        Continue;
                                    End;

                                    { Tangent points along each track.        }
                                    T1X := PX + TangentDist * U1X;
                                    T1Y := PY + TangentDist * U1Y;
                                    T2X := PX + TangentDist * U2X;
                                    T2Y := PY + TangentDist * U2Y;

                                    { Bisector unit vector. u1 + u2 lies on   }
                                    { the bisector pointing INTO the corner   }
                                    { (away from P toward the arc center).    }
                                    BX := U1X + U2X;
                                    BY := U1Y + U2Y;
                                    BLen := Sqrt(BX * BX + BY * BY);
                                    If BLen < 0.0001 Then
                                    Begin
                                        Inc(Skipped);
                                        Obj := SpatIter.NextPCBObject;
                                        Continue;
                                    End;
                                    BX := BX / BLen;
                                    BY := BY / BLen;

                                    CX := PX + CenterDist * BX;
                                    CY := PY + CenterDist * BY;

                                    { Arc start/end angles in Altium degrees  }
                                    { (counter-clockwise from +X). Altium     }
                                    { sweeps EndAngle counter-clockwise from  }
                                    { StartAngle, so we pick the order that   }
                                    { keeps the sweep <= 180 degrees.         }
                                    A1 := ArcTan2(T1Y - CY, T1X - CX) * 180.0 / cPi;
                                    A2 := ArcTan2(T2Y - CY, T2X - CX) * 180.0 / cPi;
                                    If A1 < 0 Then A1 := A1 + 360.0;
                                    If A2 < 0 Then A2 := A2 + 360.0;
                                    StartAngleDeg := A1;
                                    EndAngleDeg := A2;
                                    If (EndAngleDeg - StartAngleDeg) < 0 Then
                                        EndAngleDeg := EndAngleDeg + 360.0;
                                    If (EndAngleDeg - StartAngleDeg) > 180.0 Then
                                    Begin
                                        StartAngleDeg := A2;
                                        EndAngleDeg := A1;
                                        If (EndAngleDeg - StartAngleDeg) < 0 Then
                                            EndAngleDeg := EndAngleDeg + 360.0;
                                    End;

                                    NetName := '';
                                    Try If Track.Net <> Nil Then NetName := Track.Net.Name; Except End;
                                    LayerName := GetLayerString(Track.Layer);

                                    { Apply the mutation when not in dry_run.  }
                                    { Defensive: bail the whole pair out on    }
                                    { any failure rather than half-modify it.  }
                                    ApplyOk := True;
                                    If Not DryRun Then
                                    Begin
                                        Arc := Nil;
                                        Try
                                            Arc := PCBServer.PCBObjectFactory(
                                                eArcObject, eNoDimension,
                                                eCreate_Default);
                                        Except End;
                                        If Arc = Nil Then ApplyOk := False;

                                        If ApplyOk Then
                                        Begin
                                            Try
                                                Arc.XCenter := Round(CX);
                                                Arc.YCenter := Round(CY);
                                                Arc.Radius := Round(R);
                                                Arc.StartAngle := StartAngleDeg;
                                                Arc.EndAngle := EndAngleDeg;
                                                Arc.LineWidth := Track.Width;
                                                Arc.Layer := Track.Layer;
                                                If Track.Net <> Nil Then
                                                    Arc.Net := Track.Net;
                                                Board.AddPCBObject(Arc);
                                                PCBServer.SendMessageToRobots(
                                                    Board.I_ObjectAddress,
                                                    c_Broadcast,
                                                    PCBM_BoardRegisteration,
                                                    Arc.I_ObjectAddress);
                                            Except
                                                ApplyOk := False;
                                            End;
                                        End;

                                        If ApplyOk Then
                                        Begin
                                            Try
                                                Track.BeginModify;
                                                If Endpoint = 1 Then
                                                Begin
                                                    Track.X1 := Round(T1X);
                                                    Track.Y1 := Round(T1Y);
                                                End
                                                Else
                                                Begin
                                                    Track.X2 := Round(T1X);
                                                    Track.Y2 := Round(T1Y);
                                                End;
                                                Track.EndModify;
                                                Try Track.GraphicallyInvalidate; Except End;
                                            Except
                                                ApplyOk := False;
                                            End;
                                        End;

                                        If ApplyOk Then
                                        Begin
                                            Try
                                                Other.BeginModify;
                                                If OtherEndpoint = 1 Then
                                                Begin
                                                    Other.X1 := Round(T2X);
                                                    Other.Y1 := Round(T2Y);
                                                End
                                                Else
                                                Begin
                                                    Other.X2 := Round(T2X);
                                                    Other.Y2 := Round(T2Y);
                                                End;
                                                Other.EndModify;
                                                Try Other.GraphicallyInvalidate; Except End;
                                            Except
                                                ApplyOk := False;
                                            End;
                                        End;
                                    End;

                                    If ApplyOk Then
                                    Begin
                                        Inc(Filleted);
                                        If Not DryRun Then SaveNeeded := True;
                                    End
                                    Else
                                        Inc(Skipped);

                                    If Not First Then ItemsJson := ItemsJson + ',';
                                    First := False;
                                    EntryJson :=
                                        JsonStr('net', NetName) + ',' +
                                        JsonStr('layer', LayerName) + ',' +
                                        JsonInt('x_mils', CoordToMils(PX)) + ',' +
                                        JsonInt('y_mils', CoordToMils(PY)) + ',' +
                                        JsonFloat('angle_deg', ThetaDeg) + ',' +
                                        JsonInt('radius_mils', RadiusMils) + ',' +
                                        JsonBool('applied', ApplyOk And (Not DryRun));
                                    ItemsJson := ItemsJson + JsonObj(EntryJson);
                                Except End;
                                Obj := SpatIter.NextPCBObject;
                            End;
                        Finally
                            Try Board.SpatialIterator_Destroy(SpatIter); Except End;
                        End;
                    Except End;
                    If Filleted + Skipped >= MaxItems Then Break;
                End;

                Track := Iter.NextPCBObject;
            End;
        Finally
            Try Board.BoardIterator_Destroy(Iter); Except End;
        End;
    Finally
        If Not DryRun Then PCBServer.PostProcess;
    End;

    If SaveNeeded Then
    Begin
        Try Board.GraphicallyInvalidate; Except End;
        Try SaveDocByPath(Board.FileName); Except End;
    End;

    Result := BuildSuccessResponse(RequestId,
        JsonObj(
            JsonBool('dry_run', DryRun) + ',' +
            JsonInt('filleted_count', Filleted) + ',' +
            JsonInt('skipped_count', Skipped) + ',' +
            JsonInt('radius_mils', RadiusMils) + ',' +
            JsonFloat('min_angle_deg', MinAngleDeg) + ',' +
            JsonRaw('items', '[' + ItemsJson + ']')
        ));
End;


{..............................................................................}
{ PCB_CalcPolygonArea - Report the outline area of each polygon on the board, }
{ optionally filtered by net and/or layer. AreaSize is the polygon's overall  }
{ boundary area; reported in square mils and square millimetres.              }
{ Params: net (optional), layer (optional)                                    }
{..............................................................................}

Function PCB_CalcPolygonArea(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iterator : IPCB_BoardIterator;
    Poly : IPCB_Polygon;
    NetFilter, LayerFilter, NetName, LayerName, NameStr, JsonItems : String;
    AreaCoord, SqMils, SqMm : Double;
    First, Keep : Boolean;
    Count : Integer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    NetFilter := ExtractJsonValue(Params, 'net');
    LayerFilter := ExtractJsonValue(Params, 'layer');
    JsonItems := '';
    First := True;
    Count := 0;

    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(ePolyObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);
    Poly := Iterator.FirstPCBObject;
    While Poly <> Nil Do
    Begin
        NetName := '';
        Try If Poly.Net <> Nil Then NetName := Poly.Net.Name; Except End;
        LayerName := '';
        Try LayerName := GetLayerString(Poly.Layer); Except End;
        NameStr := '';
        Try NameStr := Poly.Name; Except End;

        Keep := True;
        If (NetFilter <> '') And (NetName <> NetFilter) Then Keep := False;
        If (LayerFilter <> '') And (LayerName <> LayerFilter) Then Keep := False;

        If Keep Then
        Begin
            SqMils := 0;
            Try SqMils := PolygonAreaSqMils(Poly); Except End;
            SqMm := SqMils * 0.00064516;
            If Not First Then JsonItems := JsonItems + ',';
            First := False;
            JsonItems := JsonItems
                + '{"name":"' + EscapeJsonString(NameStr) + '",'
                + '"net":"' + EscapeJsonString(NetName) + '",'
                + '"layer":"' + EscapeJsonString(LayerName) + '",'
                + '"area_sq_mils":' + FloatToJsonStr(SqMils) + ','
                + '"area_sq_mm":' + FloatToJsonStr(SqMm) + '}';
            Inc(Count);
        End;
        Poly := Iterator.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(Iterator);

    Result := BuildSuccessResponse(RequestId,
        '{"polygons":[' + JsonItems + '],"count":' + IntToStr(Count) + '}');
End;

{..............................................................................}
{ PCB_SetViaSoldermaskRelief - Set per-via soldermask expansion from the hole }
{ edge so via barrels get a soldermask opening (barrel relief). Optionally    }
{ filter by net. Params: expansion_mils (default 4), net (optional)           }
{..............................................................................}

Function PCB_SetViaSoldermaskRelief(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    Iterator : IPCB_BoardIterator;
    Via : IPCB_Via;
    NetFilter, NetName, ExpStr : String;
    ExpMils, Count : Integer;
    Keep : Boolean;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    NetFilter := ExtractJsonValue(Params, 'net');
    ExpStr := ExtractJsonValue(Params, 'expansion_mils');
    ExpMils := StrToIntDef(ExpStr, 4);
    Count := 0;

    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(eViaObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);

    PCBServer.PreProcess;
    Try
        Via := Iterator.FirstPCBObject;
        While Via <> Nil Do
        Begin
            NetName := '';
            Try If Via.Net <> Nil Then NetName := Via.Net.Name; Except End;
            Keep := True;
            If (NetFilter <> '') And (NetName <> NetFilter) Then Keep := False;
            If Keep Then
            Begin
                Try
                    PCBServer.SendMessageToRobots(Via.I_ObjectAddress, c_Broadcast,
                        PCBM_BeginModify, c_NoEventData);
                    Via.SolderMaskExpansionFromHoleEdge := True;
                    Via.SolderMaskExpansion := MilsToCoord(ExpMils);
                    PCBServer.SendMessageToRobots(Via.I_ObjectAddress, c_Broadcast,
                        PCBM_EndModify, c_NoEventData);
                    Inc(Count);
                Except
                End;
            End;
            Via := Iterator.NextPCBObject;
        End;
    Finally
        PCBServer.PostProcess;
    End;
    Board.BoardIterator_Destroy(Iterator);

    Result := BuildSuccessResponse(RequestId,
        '{"success":true,"modified":' + IntToStr(Count) + ','
        + '"expansion_mils":' + IntToStr(ExpMils) + '}');
End;

{..............................................................................}
{ PCB_GetMechLayerNames - List the enabled (displayed) mechanical layers with  }
{ their custom names. Uses only proven accessors (LayerStack_V7 /              }
{ LayerObject_V7[] / LayerIsDisplayed) -- ILayer.MechanicalLayer(i) and        }
{ MechanicalLayerEnabled are undeclared in this script binding.               }
{..............................................................................}

Function PCB_GetMechLayerNames(Params : String; RequestId : String) : String;
Var
    Board : IPCB_Board;
    LayerStack : IPCB_LayerStack_V7;
    LayerObj : IPCB_LayerObject_V7;
    Lyr : TLayer;
    JsonItems, NameStr : String;
    First, Disp : Boolean;
    Count : Integer;
Begin
    Board := GetPCBBoardAnywhere;
    If Board = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_PCB', 'No PCB document is active');
        Exit;
    End;

    LayerStack := Board.LayerStack_V7;
    If LayerStack = Nil Then
    Begin
        Result := BuildErrorResponse(RequestId, 'NO_STACKUP', 'Could not access layer stack');
        Exit;
    End;

    JsonItems := '';
    First := True;
    Count := 0;

    For Lyr := eMechanical1 To eMechanical16 Do
    Begin
        LayerObj := Nil;
        Try LayerObj := LayerStack.LayerObject_V7[Lyr]; Except LayerObj := Nil; End;
        If LayerObj <> Nil Then
        Begin
            Disp := False;
            Try Disp := Board.LayerIsDisplayed[Lyr]; Except End;
            If Disp Then
            Begin
                NameStr := '';
                Try NameStr := LayerObj.Name; Except End;
                If Not First Then JsonItems := JsonItems + ',';
                First := False;
                JsonItems := JsonItems
                    + '{"layer":"' + EscapeJsonString(GetLayerString(Lyr)) + '",'
                    + '"name":"' + EscapeJsonString(NameStr) + '"}';
                Inc(Count);
            End;
        End;
    End;

    Result := BuildSuccessResponse(RequestId,
        '{"mechanical_layers":[' + JsonItems + '],"count":' + IntToStr(Count) + '}');
End;

{..............................................................................}
{ HandlePCBCommand - Route PCB actions to handlers                            }
{..............................................................................}

Function HandlePCBCommand(Action : String; Params : String; RequestId : String) : String;
Begin
    Case Action Of
        'get_nets':                Result := PCB_GetNets(Params, RequestId);
        'get_net_classes':         Result := PCB_GetNetClasses(Params, RequestId);
        'create_net_class':        Result := PCB_CreateNetClass(Params, RequestId);
        'get_design_rules':        Result := PCB_GetDesignRules(Params, RequestId);
        'get_rule_properties':     Result := PCB_GetRuleProperties(Params, RequestId);
        'set_rule_properties':     Result := PCB_SetRuleProperties(Params, RequestId);
        'set_rules_enabled':       Result := PCB_SetRulesEnabled(Params, RequestId);
        'run_drc':                 Result := PCB_RunDRC(Params, RequestId);
        'get_components':          Result := PCB_GetComponents(Params, RequestId);
        'move_component':          Result := PCB_MoveComponent(Params, RequestId);
        'batch_move_components':   Result := PCB_BatchMoveComponents(Params, RequestId);
        'copy_component_placement': Result := PCB_CopyComponentPlacement(Params, RequestId);
        'set_text_visibility':     Result := PCB_SetTextVisibility(Params, RequestId);
        'lock_net_routing':        Result := PCB_LockNetRouting(Params, RequestId);
        'place_stitching_vias':    Result := PCB_PlaceStitchingVias(Params, RequestId);
        'get_fab_stats':           Result := PCB_GetFabStats(Params, RequestId);
        'clear_source_footprint_library': Result := PCB_ClearSourceFootprintLibrary(Params, RequestId);
        'get_differential_pairs':  Result := PCB_GetDifferentialPairs(Params, RequestId);
        'make_paste_grid':         Result := PCB_MakePasteGrid(Params, RequestId);
        'add_testpoints_for_net_class': Result := PCB_AddTestpointsForNetClass(Params, RequestId);
        'check_placement_collision': Result := PCB_CheckPlacementCollision(Params, RequestId);
        'get_trace_lengths':       Result := PCB_GetTraceLengths(Params, RequestId);
        'get_layer_stackup':       Result := PCB_GetLayerStackup(Params, RequestId);
        'add_layer':               Result := PCB_AddLayer(Params, RequestId);
        'remove_layer':            Result := PCB_RemoveLayer(Params, RequestId);
        'modify_layer':            Result := PCB_ModifyLayer(Params, RequestId);
        'get_board_outline':       Result := PCB_GetBoardOutline(Params, RequestId);
        'get_selected_objects':    Result := PCB_GetSelectedObjects(Params, RequestId);
        'set_layer_visibility':    Result := PCB_SetLayerVisibility(Params, RequestId);
        'repour_polygons':         Result := PCB_RepourPolygons(Params, RequestId);
        'place_via':               Result := PCB_PlaceVia(Params, RequestId);
        'place_track':             Result := PCB_PlaceTrack(Params, RequestId);
        'place_tracks':            Result := PCB_PlaceTracks(Params, RequestId);
        'place_arc':               Result := PCB_PlaceArc(Params, RequestId);
        'place_text':              Result := PCB_PlaceText(Params, RequestId);
        'place_fill':              Result := PCB_PlaceFill(Params, RequestId);
        'start_polygon_placement': Result := PCB_StartPolygonPlacement(Params, RequestId);
        'create_design_rule':      Result := PCB_CreateDesignRule(Params, RequestId);
        'delete_design_rule':      Result := PCB_DeleteDesignRule(Params, RequestId);
        'get_component_pads':      Result := PCB_GetComponentPads(Params, RequestId);
        'flip_component':          Result := PCB_FlipComponent(Params, RequestId);
        'align_components':        Result := PCB_AlignComponents(Params, RequestId);
        'get_clearance_violations': Result := PCB_GetClearanceViolations(Params, RequestId);
        'snap_to_grid':            Result := PCB_SnapToGrid(Params, RequestId);
        'get_diff_pair_rules':     Result := PCB_GetDiffPairRules(Params, RequestId);
        'get_vias':                Result := PCB_GetVias(Params, RequestId);
        'delete_object':           Result := PCB_DeleteObject(Params, RequestId);
        'get_pad_properties':      Result := PCB_GetPadProperties(Params, RequestId);
        'set_track_width':         Result := PCB_SetTrackWidth(Params, RequestId);
        'get_unrouted_nets':       Result := PCB_GetUnroutedNets(Params, RequestId);
        'get_polygons':            Result := PCB_GetPolygons(Params, RequestId);
        'calc_polygon_area':       Result := PCB_CalcPolygonArea(Params, RequestId);
        'set_via_soldermask_relief': Result := PCB_SetViaSoldermaskRelief(Params, RequestId);
        'get_mech_layer_names':    Result := PCB_GetMechLayerNames(Params, RequestId);
        'modify_polygon':          Result := PCB_ModifyPolygon(Params, RequestId);
        'get_room_rules':          Result := PCB_GetRoomRules(Params, RequestId);
        'create_room':             Result := PCB_CreateRoom(Params, RequestId);
        'get_board_statistics':    Result := PCB_GetBoardStatistics(Params, RequestId);
        'export_coordinates':      Result := PCB_ExportCoordinates(Params, RequestId);
        'set_board_shape':         Result := PCB_SetBoardShape(Params, RequestId);
        'place_polygon_rect':      Result := PCB_PlacePolygonRect(Params, RequestId);
        'place_via_array':         Result := PCB_PlaceViaArray(Params, RequestId);
        'create_diff_pair':        Result := PCB_CreateDiffPair(Params, RequestId);
        'place_region':            Result := PCB_PlaceRegion(Params, RequestId);
        'distribute_components':   Result := PCB_DistributeComponents(Params, RequestId);
        'place_dimension':         Result := PCB_PlaceDimension(Params, RequestId);
        'place_pad':               Result := PCB_PlacePad(Params, RequestId);
        'place_angular_dimension': Result := PCB_PlaceAngularDimension(Params, RequestId);
        'place_radial_dimension':  Result := PCB_PlaceRadialDimension(Params, RequestId);
        'place_embedded_board':    Result := PCB_PlaceEmbeddedBoard(Params, RequestId);
        'fillet_corners':          Result := PCB_FilletCorners(Params, RequestId);
    Else
        Result := BuildErrorResponse(RequestId, 'UNKNOWN_ACTION', 'Unknown PCB action: ' + Action);
    End;
End;

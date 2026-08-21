# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Tools that drive the Altium GUI, for operations with no API.

Altium exposes a great deal only as menus and dialogs, so this is a
general driver rather than a one-off: discover a menu, invoke it, read
whatever dialog it raises, set the controls inside, press a button.

NONE OF IT USES THE BRIDGE. Every tool here talks to Win32 and to the
accessible layer, and only asks the bridge for Altium's process id,
which is a process scan rather than IPC. That is what makes these the
tools that still answer when a modal has the scripting engine blocked,
and it is why invoking a command and driving the dialog it opens can
be a single call: nothing in that sequence needs the bridge to be free.

Everything here presses buttons in a GUI, which is a different kind of
operation from the rest of the surface, and the separate module is so
nobody reaches one by accident while looking for the other.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..ui import controls, dialog_driver, dialog_report, menu, windows

#: Menu path per target. The only thing that still differs between the
#: schematic and board variants: everything after the menu fires is
#: decided from the dialogs that actually appear.
_MENUS = {
    "schematic": "Tools|Update From Libraries",
    "pcb": "Tools|Update From PCB Libraries",
}

#: Full menu path per target, matched by NAME at every level.
#: MEASURED off the live Tools menu, which lists sixteen entries
#: including several starting "Update", so the full caption is used.
_MENU_PATHS = {
    "schematic": "Tools|Update From Libraries...",
    "pcb": "Tools|Update From PCB Libraries...",
}


def _altium_pid():
    """(pid, None) or (None, error-dict).

    Resolved through the bridge's process scan, NOT through IPC, so it
    still answers while Altium is blocked on a modal. Every tool in
    this module needs it and each used to inline the same six lines.
    """
    from ..bridge import get_bridge

    status = get_bridge().get_altium_status()
    if not status.get("running") or not status.get("pid"):
        return None, {"ok": False, "reason": "Altium is not running"}
    return int(status["pid"]), None


def register_uiauto_tools(mcp):
    """Register the dialog-driving tools. Windows and Altium only."""

    @mcp.tool()
    async def app_update_from_libraries(
        target: str = "schematic",
        launch: str = "already_open",
        dry_run: bool = True,
        confirm_execute_eco: bool = False,
        answer_confirmations: bool = True,
        menu_may_steal_focus: bool = False,
        first_step_timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Drive Tools > Update From Libraries, ECO included.

        THIS PRESSES BUTTONS IN THE GUI. Update From Libraries has no
        API: Altium documents it as a dialog, there is no process that
        launches it, and the operation ends in an Engineering Change
        Order that only a click can execute. So this matches windows
        belonging to the Altium process and presses named buttons on
        them.

        WHAT IT WILL DO AT THE END. The last steps press Validate
        Changes and then EXECUTE CHANGES on the ECO. That applies every
        pending change to the design and is not undoable through this
        channel. Take ``app_checkpoint`` first.

        WHAT IT CANNOT TELL YOU. While the wizard is open Altium's
        script engine is blocked, so the bridge cannot be asked
        anything, and an ECO's pending changes sit in a grid that
        exposes no text to Windows. The reply records the windows and
        buttons seen; it does NOT contain the change list. Read the
        Messages panel afterwards for that.

        NOTHING IS SCRIPTED. There is no list of expected dialogs. Each
        one that appears is read, classified, and answered by the ROLE
        of the buttons it offers, then the screen is read again. That is
        deliberate: a scripted version failed on live Altium because the
        editor legitimately shows dialogs no list contained, and waited
        for a change order that was never coming while an unlisted modal
        blocked the session.

        It STOPS rather than guessing when a dialog reports an error,
        asks a question it was not sent to answer, or offers no button
        whose meaning is recognised.

        RUN IT DRY FIRST, WHICH IS THE DEFAULT. A dry run reads the
        first dialog, reports what it is and what it would press, and
        presses nothing.

        LAUNCHING. ``already_open`` (the default) attaches to a wizard
        you have opened yourself, which avoids depending on a menu path
        nobody has verified. ``menu`` asks the bridge to fire the menu
        first; that request will not return until the wizard closes,
        which is expected and is why it is issued without waiting.

        Args:
            target: ``schematic`` for Tools > Update From Libraries, or
                ``pcb`` for Tools > Update From PCB Libraries.
            launch: ``already_open`` or ``menu``.
            dry_run: report without pressing. Default true.
            confirm_execute_eco: must be true for a real run, and it is
                named for the specific consequence rather than being a
                general "confirm", because the irreversible part is the
                ECO and not the wizard.
            answer_confirmations: answer routine "carry on?" prompts.
                Altium asks "Continue and create ECO?" before every
                change order, so with this off the run stops there and
                can never finish. A confirmation mentioning anything
                irreversible (delete, overwrite, cannot be undone) is
                still left alone whatever this is set to.
            first_step_timeout: seconds to wait for the FIRST dialog to
                appear. Raise it when launching by menu on a large
                project.

        Returns:
            Dict with ``ok``, ``dry_run``, ``committed``,
            ``stopped_for_a_human``, ``finished`` explaining how the run
            ended, and ``steps``: for every dialog, what it was, what it
            said, what it offered, what was pressed and WHY.
        """
        if target not in _MENUS:
            return {"ok": False, "reason": (
                f"target must be schematic or pcb, not {target!r}")}
        if launch not in ("already_open", "menu"):
            return {"ok": False, "reason": (
                f"launch must be already_open or menu, not {launch!r}")}
        if launch == "menu" and not menu_may_steal_focus:
            return {"ok": False, "reason": (
                "launch='menu' has to drive Altium's menu bar with real "
                "keyboard and mouse input, because nothing scriptable "
                "opens this wizard: the bridge's menu call reports "
                "success and invokes nothing, and Altium's menus expose "
                "no working accessible action. That means bringing "
                "Altium to the front and briefly moving the pointer, so "
                "it is not done unless asked. Pass "
                "menu_may_steal_focus=True, or open Tools > "
                + _MENUS[target].split("|", 1)[1] +
                " yourself and use launch='already_open'.")}
        if not windows.available():
            return {"ok": False, "reason": (
                "pywin32 is not importable, so no dialog can be driven "
                "from this host")}
        # Deliberately NOT refusing a real run that lacks
        # confirm_execute_eco. That blanket refusal made the tool
        # useless in the most common case: with the design already in
        # sync there is no change order at all, only a "no differences"
        # dialog that needs dismissing, and refusing to run left that
        # modal blocking the editor.
        #
        # The commit is gated where the commit happens instead. The
        # driver will drive everything up to Execute Changes and then
        # stop, naming what it refused, so a run without authorisation
        # is useful AND cannot alter the design.

        pid, problem = _altium_pid()
        if problem:
            return problem

        opened = None
        if launch == "menu":
            # The bridge is NOT used for this. Its menu call reports
            # success and invokes nothing (#83), so the menu bar is
            # driven directly. Run off-thread: it sleeps between the
            # keystroke and the click.
            loop = asyncio.get_running_loop()
            opened = await loop.run_in_executor(
                None, menu.click_path, pid, _MENU_PATHS[target])
            if not opened.get("ok"):
                return {"ok": False, "launch": "menu",
                        "reason": opened.get("reason"),
                        "offered": opened.get("offered"),
                        "note": ("nothing was opened, so nothing was "
                                 "driven and no dialog is waiting")}

        loop = asyncio.get_running_loop()
        report = await loop.run_in_executor(
            None, lambda: dialog_driver.drive(
                pid, intent="proceed",
                allow_commit=bool(confirm_execute_eco),
                allow_confirm=bool(answer_confirmations),
                wait_first=float(first_step_timeout),
                dry_run=dry_run))

        if opened is not None:
            report["opened_by_menu"] = opened
        report["target"] = target
        report["launch"] = launch
        report["altium_pid"] = pid
        return report

    @mcp.tool()
    async def app_set_dialog_control(
        control_label: str,
        checked: bool | None = None,
        text: str | None = None,
        select_row: str | None = None,
        dialog_title: str = "",
        list_only: bool = False,
    ) -> dict[str, Any]:
        """Read or CHANGE a control inside whatever dialog is open.

        The other half of driving a dialog. Buttons are pressed by the
        dialog driver; this is for the checkboxes, radios and fields
        that decide what those buttons will DO.

        FIND BY LABEL, not position. ``"Include Variants"``. Positions
        move between versions and contexts, captions do not.

        EVERY CHANGE IS VERIFIED. The new state is read back, and a
        click that silently did nothing is reported as a failure rather
        than as success. That matters here more than anywhere: a caller
        that believes an option was applied will go on to press
        something irreversible.

        IT REFUSES RATHER THAN GUESSING when a control is DISABLED
        (Altium greys options that do not apply, and clicking one is at
        best a no-op), and when asked to switch a radio button OFF,
        which is not a thing a radio does.

        SETTING IS NOT A TOGGLE. Asking for a state the control is
        already in reports ``changed: false`` and clicks nothing, so
        calling twice is safe.

        Args:
            control_label: the control's visible label.
            checked: desired state for a checkbox.
            text: desired contents for an edit field.
            select_row: for a list, grid or tree, the row to select by
                name. Rows are matched by name for the same reason menu
                items are: positions shift and an index would pick the
                wrong one the first time the contents changed.
            dialog_title: substring to pick one dialog when several are
                open. Empty uses the first.
            list_only: change nothing, just report every control in the
                dialog with its role, state and whether it is enabled.

        Returns:
            For a read, the control list. For a change, ``ok``,
            ``changed`` and the state read back afterwards.
        """
        if not controls.available():
            return {"ok": False, "reason": (
                "reading dialog controls needs pywin32 and comtypes")}

        pid, problem = _altium_pid()
        if problem:
            return problem

        open_dialogs = windows.dialogs(pid)
        if dialog_title:
            open_dialogs = [d for d in open_dialogs
                            if dialog_title.lower() in (d.title or "").lower()]
        if not open_dialogs:
            return {"ok": False, "reason": (
                "no matching dialog is open. Nothing can be set on a "
                "dialog that is not on screen")}
        dialog = open_dialogs[0]

        if list_only:
            return {"ok": True, "dialog": dialog.title,
                    "controls": controls.describe_all(dialog)}

        found = controls.find(dialog, control_label)
        if found is None:
            labels = [c["label"] or c["name"]
                      for c in controls.describe_all(dialog)]
            return {"ok": False,
                    "reason": (f"{control_label!r} is not in "
                               f"{dialog.title!r}"),
                    "offered": [x for x in labels if x]}

        if checked is not None:
            result = controls.set_checked(found["hwnd"], bool(checked))
        elif text is not None:
            result = controls.set_text(found["hwnd"], text)
        elif select_row is not None:
            result = controls.select_row(found["hwnd"], select_row)
        else:
            info = dict(found)
            if info["role"] in ("list", "outline", "table"):
                info["rows"] = controls.list_items(found["hwnd"])
            return {"ok": True, "control": info,
                    "note": ("nothing to change; pass checked, text or "
                             "select_row")}
        result["dialog"] = dialog.title
        result["control_label"] = control_label
        return result

    @mcp.tool()
    async def app_click_menu(menu_path: str,
                             may_steal_focus: bool = False,
                             list_only: bool = False) -> dict[str, Any]:
        """Invoke ANY Altium menu command by path, by driving the menu.

        Use this when ``app_run_menu`` will not do, which is more often
        than it looks: that tool reports success for commands it never
        invokes, because the process ids it dispatches cannot report
        failure and some of them are not real (task #83). This one
        clicks the actual menu, so a command either runs or the reply
        says why not.

        PATH BY NAME. ``"Design|Rules..."``, ``"Tools|Update From
        Libraries..."``. Accelerator markers and a trailing ellipsis are
        ignored, so ``"Design|Rules"`` works too. Nested submenus work
        by adding levels. Names are used rather than positions because
        entries move with context and separators are invisible.

        IT NEEDS THE FOREGROUND. Altium has no Win32 menu and its
        DevExpress bars expose no working accessible action, so the only
        thing that opens them is real input: this brings Altium to the
        front and briefly moves the pointer, putting it back afterwards.
        Every other press in this package is addressed to a window
        handle and depends on none of that, which is why this one is
        gated behind ``may_steal_focus`` and never happens by default.

        THE MENU BAR IS PER EDITOR. With a board focused you get the PCB
        menus, with a schematic the schematic ones. Focus the matching
        document first, or the reply will tell you the item was not
        there and list what was.

        WHAT HAPPENS NEXT IS NOT THIS TOOL'S JOB. Many commands open a
        dialog. Read it with ``app_list_open_dialogs``; the dialog
        drivers handle the rest.

        Args:
            menu_path: pipe-separated path, top level first.
            may_steal_focus: required to actually click anything.
            list_only: do not invoke. Reports the top-level menus
                currently on screen, which is the way to find out what
                this editor context offers.

        Returns:
            Dict with ``ok``; on failure ``reason`` and ``offered``
            listing what that level actually contained.
        """
        if not menu.available():
            return {"ok": False, "reason": (
                "menu driving needs pywin32 and comtypes on Windows")}

        pid, problem = _altium_pid()
        if problem:
            return problem

        loop = asyncio.get_running_loop()
        if list_only:
            # Listing needs the SAME activation as clicking, and an
            # empty path means the menu bar. Altium lays its bars out on
            # activation, so a read without it fails whenever Altium is
            # not already foreground, which is precisely when a caller
            # needs to ask what the menus are.
            if not may_steal_focus:
                return {"ok": False, "reason": (
                    "reading the menu bar needs Altium activated, "
                    "because it only lays its bars out then. Pass "
                    "may_steal_focus=True.")}
            return await loop.run_in_executor(
                None, menu.list_path, pid, menu_path)

        if not may_steal_focus:
            return {"ok": False, "reason": (
                "clicking a menu needs real keyboard and mouse input, "
                "because Altium exposes no scriptable or accessible way "
                "to open one. That means bringing Altium to the front "
                "and briefly moving the pointer. Pass "
                "may_steal_focus=True if that is acceptable now.")}

        return await loop.run_in_executor(
            None, menu.click_path, pid, menu_path)

    @mcp.tool()
    async def app_list_open_dialogs() -> dict[str, Any]:
        """What Altium has on screen, what it says, and if it is stuck.

        THE TOOL TO REACH FOR WHEN THE BRIDGE GOES QUIET. A silent
        bridge looks identical whether Altium is showing a modal, busy
        compiling, or dead. This answers it directly: ``blocked`` is
        read from the main window being disabled, which is what Windows
        does while a modal is up, and it works precisely when no other
        tool can because it does not use the bridge at all. It reads the
        Win32 process, so a blocked Altium answers it normally.

        FOR EACH DIALOG you get the caption, the message text, every
        button with its enabled state, and a ``kind``:

        * ``error`` / ``warning`` / ``confirm`` -- something went wrong
          or a decision is being asked. These set ``needs_a_human``.
        * ``nothing_to_do`` -- the operation completed with no work, for
          instance "Comparator Results (No Differences)" when the
          schematic and board already agree. A success, not a failure.
        * ``engineering_change_order`` -- an ECO awaiting Validate and
          Execute.
        * ``wizard`` / ``progress`` / ``unknown``.

        WHAT IT STILL CANNOT READ. Grids and owner-drawn lists hold
        their content internally and expose no window text, so an ECO's
        pending-change list is not readable. That is reported per dialog
        as ``has_unreadable_content`` rather than being passed off as a
        dialog with nothing in it. Read the Messages panel for the
        change list.

        Reads only. Presses nothing.

        Returns:
            Dict with ``blocked``, ``dialog_count``, ``dialogs``,
            ``needs_a_human``, and a one-sentence ``summary``.
        """
        if not windows.available():
            return {"ok": False, "reason": (
                "pywin32 is not importable, so no dialog can be inspected "
                "from this host")}

        pid, problem = _altium_pid()
        if problem:
            return problem

        return dialog_report.report(pid)

    @mcp.tool()
    async def app_press_dialog_button(button_caption: str,
                                      dialog_title: str = "",
                                      allow_irreversible: bool = False
                                      ) -> dict[str, Any]:
        """Press ONE named button in an open Altium dialog.

        The missing primitive. Dialogs could be listed, read and their
        checkboxes set, but the only code that pressed anything was
        buried inside a single task-specific driver, so answering an
        arbitrary dialog meant writing a script.

        BY CAPTION, not position. Accelerator ampersands and a trailing
        ellipsis are ignored, so "Report Changes" finds
        "&Report Changes...".

        IT WORKS WHILE ALTIUM IS BLOCKED, because it addresses the
        button's window handle over Win32 and never touches the bridge.
        That is the whole point: when a modal has the scripting engine
        stuck, this is what gets it unstuck.

        IRREVERSIBLE PRESSES ARE GATED. A caption that commits a change
        order or applies a wizard is refused unless
        ``allow_irreversible`` is set, so a driver walking an unfamiliar
        dialog cannot execute one by reflex.

        For a whole sequence rather than one press, use
        ``app_drive_dialogs``, which decides each step from what is on
        screen instead of from a script.

        Args:
            button_caption: the visible caption.
            dialog_title: substring picking one dialog when several are
                open. Empty uses the first.
            allow_irreversible: permit a committing press.

        Returns:
            ``ok``, the dialog and button actually matched, and whether
            the dialog closed afterwards.
        """
        pid, problem = _altium_pid()
        if problem:
            return problem

        def press():
            open_dialogs = windows.dialogs(pid)
            if not open_dialogs:
                return {"ok": False, "reason": "no dialog is open"}
            target = None
            for dialog in open_dialogs:
                title = (dialog.title or "").lower()
                if not dialog_title or dialog_title.lower() in title:
                    target = dialog
                    break
            if target is None:
                return {"ok": False,
                        "reason": f"no open dialog matches {dialog_title!r}",
                        "open": [d.title for d in open_dialogs]}

            button = target.find_button(button_caption)
            if button is None:
                return {"ok": False,
                        "reason": (f"{button_caption!r} is not a button on "
                                   f"{target.title!r}"),
                        "offered": [b.text for b in target.buttons()]}
            if not button.enabled:
                return {"ok": False, "reason": (
                    f"{button.text!r} is disabled, so the dialog is not in "
                    f"the state this press assumed")}

            role = dialog_driver.role_of(button.text)
            if role in dialog_driver.COMMITTING and not allow_irreversible:
                return {"ok": False, "role": role, "reason": (
                    f"{button.text!r} commits a change to the design. Pass "
                    f"allow_irreversible=True if that is intended.")}

            windows.click(button)
            closed = windows.wait_for_close(target.hwnd, timeout=5.0)

            # A press that changed nothing is not a success. MEASURED
            # 2026-08-18 on a wedged Altium: the only button was OK, the
            # message went out, the dialog stayed, and this returned
            # ok true. That is the same defect as a handler asserting an
            # outcome it never checked, in the tool written to avoid it.
            #
            # Staying open is NORMAL for some presses: Validate Changes
            # leaves the change order up on purpose, and a wizard page
            # advances without closing its window. So the verdict turns
            # on the ROLE, not on closure alone. Only a press whose
            # whole job is to dismiss can be judged by the window going
            # away, and only when it was the sole way out.
            dismissing = role in ("dismiss", "advance")
            only_way_out = len(target.buttons()) == 1
            if not closed and dismissing and only_way_out:
                return {
                    "ok": False,
                    "dialog": target.title,
                    "pressed": button.text,
                    "role": role,
                    "dialog_closed": False,
                    "reason": (
                        f"{button.text!r} was the only button on "
                        f"{target.title!r} and the dialog is still open, so "
                        f"the press did not take. Altium can reach a state "
                        f"where the window pumps messages but acts on none "
                        f"of them, and no synthetic press will clear it."),
                }
            return {"ok": True, "dialog": target.title,
                    "pressed": button.text, "role": role,
                    "dialog_closed": closed,
                    "outcome_verified": bool(closed) or not dismissing}

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, press)

    @mcp.tool()
    async def app_drive_dialogs(intent: str = "proceed",
                                allow_commit: bool = False,
                                allow_confirm: bool = False,
                                dry_run: bool = True,
                                wait_first: float = 30.0,
                                budget: float = 300.0) -> dict[str, Any]:
        """Answer dialogs reactively until none is left.

        The reactive driver, which existed but was reachable only
        through the one wizard tool that used it. It reads whatever is
        on screen, classifies it, decides a press from that, and
        repeats. There is no scripted sequence, so a dialog appearing in
        a different order, or not at all, is handled rather than
        breaking a plan.

        IT STOPS RATHER THAN GUESSING on a dialog it cannot classify,
        and on anything that needs a human. ``dry_run`` is the DEFAULT:
        it reports every decision and presses nothing, which is how to
        see what a sequence would do before letting it.

        THE TWO GATES ARE SEPARATE ON PURPOSE. ``allow_confirm`` answers
        a yes/no question that moves an operation along;
        ``allow_commit`` permits the press that changes the design.
        Named for their consequences, because a caller reaching for one
        rarely means the other.

        Args:
            intent: "proceed" to carry the operation forward, or
                "cancel" to back out of whatever is open.
            allow_commit: permit the design-changing press.
            allow_confirm: permit answering a confirmation.
            dry_run: decide and report, press nothing. Default True.
            wait_first: seconds to wait for the first dialog, since a
                caller fires a command and then drives.
            budget: seconds before giving up on a wedged editor.

        Returns:
            Every observation and decision, plus ``committed``,
            ``stopped_for_a_human`` and why it finished.
        """
        pid, problem = _altium_pid()
        if problem:
            return problem

        loop = asyncio.get_running_loop()

        def run():
            return dialog_driver.drive(
                pid, intent=intent, allow_commit=allow_commit,
                allow_confirm=allow_confirm, dry_run=dry_run,
                wait_first=wait_first, budget=budget)

        return await loop.run_in_executor(None, run)

    @mcp.tool()
    async def app_run_ui_command(menu_path: str,
                                 allow_commit: bool = False,
                                 allow_confirm: bool = True,
                                 dry_run: bool = False,
                                 may_steal_focus: bool = True,
                                 wait_first: float = 30.0,
                                 budget: float = 300.0) -> dict[str, Any]:
        """Invoke a menu command AND answer the dialogs it raises.

        The composite, and usually the one to reach for. Firing a menu
        command that opens a modal blocks Altium's scripting engine, so
        the two halves cannot be separate bridge calls: the second would
        never be answered. Both halves here are Win32, so they run in
        one call and the modal is no obstacle.

        Doing this by hand meant arming a watcher process before firing
        the command, which is not something a caller should have to
        assemble.

        THE GATES ARE THE DIALOG DRIVER'S. ``allow_confirm`` defaults ON
        because a command invoked deliberately usually needs its "yes,
        go ahead" answered, while ``allow_commit`` defaults OFF, so a
        change order is presented rather than executed. ``dry_run``
        defaults OFF here, unlike ``app_drive_dialogs``: naming a
        command to run and having nothing happen is not what the caller
        asked for.

        Args:
            menu_path: pipe-separated, for example
                "Tools|Annotation|Force Annotate All Schematics...".
                Discover paths with app_click_menu(list_only=True).
            allow_commit: permit a design-changing press.
            allow_confirm: permit answering confirmations.
            dry_run: invoke the command but press nothing.
            may_steal_focus: opening a menu needs real input.
            wait_first: seconds to wait for the first dialog.
            budget: seconds before giving up.

        Returns:
            ``ok``, the ``menu`` result, and the full ``dialogs`` record.
            ``ok`` is true only when the menu click AND the drive both
            succeeded, so a command that opened something nobody could
            answer does not read as a success.
        """
        pid, problem = _altium_pid()
        if problem:
            return problem
        if not may_steal_focus:
            return {"ok": False, "reason": (
                "opening a menu needs real keyboard and mouse input, so "
                "Altium has to come to the front. Pass "
                "may_steal_focus=True if that is acceptable now.")}

        loop = asyncio.get_running_loop()
        clicked = await loop.run_in_executor(
            None, menu.click_path, pid, menu_path)
        if not clicked.get("ok"):
            return {"ok": False, "stage": "menu", "menu": clicked}

        def run():
            return dialog_driver.drive(
                pid, intent="proceed", allow_commit=allow_commit,
                allow_confirm=allow_confirm, dry_run=dry_run,
                wait_first=wait_first, budget=budget)

        driven = await loop.run_in_executor(None, run)
        ok = bool(driven.get("ok", True)) and not driven.get(
            "stopped_for_a_human")
        return {"ok": ok, "menu": clicked, "dialogs": driven}

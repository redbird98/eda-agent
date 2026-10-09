# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""What the Pascal dispatcher appends to a reply must reach the caller.

StatusForm appends ``next_step`` (whatever a handler recorded with
NoteNextStep) and ``active_document_changed`` as siblings of ``data`` on
every reply. The Python side built its response from id, success, data and
error only, so both were dropped. Found live: a lib_component:NAME@1 query
that correctly refused, because the library held nothing to reselect
from, reached the caller as a bare "Library component not found", and the
reason it had written was gone.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from eda_agent.bridge.altium_bridge import CommandResponse
from eda_agent.bridge.exceptions import AltiumError


def _reply(**envelope) -> CommandResponse:
    base = {"id": "x", "protocol_version": 0}
    base.update(envelope)
    return CommandResponse.from_dict(base)


@pytest.fixture
def bridge(e2e_bridge):
    return e2e_bridge


def _run(bridge, reply: CommandResponse):
    with patch.object(bridge, "_poll_response", return_value=reply):
        return bridge.send_command("library.anything", {})


def test_a_success_carries_the_next_step(bridge):
    out = _run(bridge, _reply(success=True, data={"ok": 1},
                              next_step="Save the library first."))
    assert out["ok"] == 1
    assert out["next_step"] == "Save the library first."


def test_a_success_carries_a_focus_change(bridge):
    moved = {"from": "a.SchLib", "to": "b.SchLib", "note": "moved"}
    out = _run(bridge, _reply(success=True, data={"ok": 1},
                              active_document_changed=moved))
    assert out["active_document_changed"] == moved


def test_a_handlers_own_next_step_wins(bridge):
    out = _run(bridge, _reply(success=True,
                              data={"next_step": "specific"},
                              next_step="general"))
    assert out["next_step"] == "specific"


def test_a_list_reply_keeps_its_shape(bridge):
    out = _run(bridge, _reply(success=True, data=[1, 2],
                              next_step="anything"))
    assert out == [1, 2]


def test_an_error_carries_the_reason_the_handler_recorded(bridge):
    reason = ("Part 1 of GH11_QUAD cannot be reached: the saved library "
              "holds no other component to reselect from.")
    reply = _reply(success=False,
                   error={"code": "NOT_FOUND",
                          "message": "Library component not found"},
                   next_step=reason)
    with pytest.raises(AltiumError) as err:
        _run(bridge, reply)
    assert reason in str(err.value)
    assert err.value.details["next_step"] == reason


def test_a_reply_without_notes_is_unchanged(bridge):
    out = _run(bridge, _reply(success=True, data={"ok": 1}))
    assert out == {"ok": 1}

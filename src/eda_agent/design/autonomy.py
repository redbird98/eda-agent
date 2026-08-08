# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The autonomous-design loop protocol (roadmap 2.1 client packaging).

The harness has the pieces: a durable session journal, a 13-stage state
machine (``design_next_action``), background jobs, project checkpoints, but
a client needs to know the *protocol* that ties them together. This module is
the single source of that protocol, surfaced as the ``design_autonomy_guide``
tool and the ``autonomous_design`` MCP prompt so any client (Claude Code,
Codex, ...) can drive a full spec-to-board run the same way.

Pure data; no Altium. Reuses the canonical stage list + playbooks so the
guide never drifts from what the state machine actually enforces.
"""

from __future__ import annotations

from .session import STAGES
from .state_machine import MAX_STAGE_ATTEMPTS, STAGE_PLAYBOOKS

# The loop a client runs. Kept short and imperative: it is meant to be read
# once and followed.
LOOP_PROTOCOL = [
    "1. Call design_get_discipline once: hard rules + the DesignPlan schema.",
    "2. design_session_start(requirement): opens the durable journal. Keep "
    "the returned session_id.",
    "3. If a project is open or will be modified, app_checkpoint('before "
    "autonomous run') so the whole run is revertible.",
    "4. Loop: call design_next_action(session_id) and act on its status:",
    "   - proceed / retry: do the stage using its suggested_tools until the "
    "exit_gate is met, then design_session_log(event='stage_result', "
    "stage=<stage>, status='ok'). If you cannot finish without the user, log "
    "status='blocked' with a question and stop.",
    "   - blocked: put the open_question to the user; when answered, "
    "design_session_log(event='resolved', text=<answer>) and continue.",
    "   - complete: the pipeline is done; proceed to outputs review.",
    "5. Checkpoint again before each high-risk mutating stage (sch_to_pcb, "
    "routing, pours_tuning).",
    "6. Long engine runs (routing a dense board) can exceed the tool "
    "timeout: start them with design_job_start and poll design_job_status.",
    f"Bounded retries: a stage that fails {MAX_STAGE_ATTEMPTS} times escalates "
    "to a human question automatically: do not loop past it.",
]

# The non-negotiables, condensed. The full text is in design_get_discipline.
HARD_CONSTRAINTS = [
    "Datasheet-first: every device fact fetched + cited from the manufacturer "
    "datasheet; never fabricated.",
    "NDA isolation: never mine or reference other client designs.",
    "No third-party routing engines or account-gated APIs in the design loop.",
    "Verify quality render-and-look, not by score alone; the visual rubric is "
    "the shipping bar.",
    "No unverifiable safety tables: ship only sourced/verified values.",
]


def autonomy_guide() -> dict:
    """The full autonomous-design protocol as structured data."""
    return {
        "overview": (
            "Drive a full spec-to-board design by looping the state machine: "
            "call design_next_action, do what it says, log the result, repeat "
            "until complete or blocked. The server owns sequencing and gates, "
            "so you never memorize the workflow."
        ),
        "loop": LOOP_PROTOCOL,
        "stages": [
            {
                "stage": st,
                "goal": STAGE_PLAYBOOKS[st]["goal"],
                "tools": STAGE_PLAYBOOKS[st]["tools"],
                "exit_gate": STAGE_PLAYBOOKS[st]["exit_gate"],
            }
            for st in STAGES
        ],
        "constraints": HARD_CONSTRAINTS,
        "resume": (
            "A run survives context loss: a fresh client calls "
            "design_session_resume (or design_next_action) with the session_id "
            "and picks up from recorded state."
        ),
    }


def autonomy_prompt_text(requirement: str = "") -> str:
    """Render the guide as a single instruction block for an MCP prompt."""
    g = autonomy_guide()
    lines = [
        "# Autonomous PCB design",
        "",
        g["overview"],
        "",
        "## Loop",
        *g["loop"],
        "",
        "## Hard constraints",
        *[f"- {c}" for c in g["constraints"]],
        "",
        "## Pipeline stages",
        *[f"- {s['stage']}: {s['goal']}" for s in g["stages"]],
        "",
        g["resume"],
    ]
    if requirement.strip():
        lines += [
            "",
            "## This run's requirement",
            requirement.strip(),
            "",
            "Begin now: call design_get_discipline, then "
            "design_session_start with the requirement above.",
        ]
    return "\n".join(lines)

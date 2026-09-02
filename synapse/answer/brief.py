"""
synapse.answer.brief
====================
**DEPRECATED SHIM.** The appointment brief now lives in :mod:`synapse.brief`.

Scheduled for removal on **2026-11-30**, with the other compatibility shims.

Why it moved
------------
This module rendered a brief directly from a
:class:`~synapse.answer.schema.GroundedAnswer`. The brief has since become a
document in its own right: it carries user-authored content, it is editable, it
is exported in three formats, and it has invariants of its own (an unsupported
claim cannot enter it; every citation marker must resolve). Those invariants
belong to a schema, not to a formatter, so the brief is now built as validated
typed data — :class:`synapse.brief.schema.AppointmentBrief` — and rendered from
that.

What remains here is the old call signature, so existing callers keep working.
It builds a typed brief and renders it, which means there is exactly one text
formatter rather than two that drift apart.

Emergency and staff routing are handled here rather than in the brief package.
An escalation is not an appointment brief: it cites nothing, has no claims and
carries no user content, so modelling it as one would add fields to the schema
that exist only to be empty.
"""

from __future__ import annotations  # Postponed annotations

from datetime import datetime

from synapse.answer.render import (
    ABSTENTION_LABEL,
    EMERGENCY_MESSAGE,
    PERMANENT_DISCLAIMER,
    STAFF_MESSAGE,
    SourceNumbering,
)
from synapse.answer.schema import AnswerAction, GroundedAnswer

BRIEF_TITLE = "APPOINTMENT BRIEF"

BRIEF_FILENAME = (
    "appointment-brief.txt"  # Superseded by the document-id filenames in synapse.brief.export
)

# Explicit, patient-readable name for each action state.
ACTION_LABELS: dict[AnswerAction, str] = {
    AnswerAction.ANSWER: "Answer based on published research",
    AnswerAction.ABSTAIN: ABSTENTION_LABEL,
    AnswerAction.MEDICAL_STAFF: "Please speak to clinical staff",
    AnswerAction.EMERGENCY: "Urgent — speak to staff now",
}


def render_appointment_brief(
    answer: GroundedAnswer,
    numbering: SourceNumbering,
    *,
    query: str = "",
    generated_at: datetime | None = None,
    document_id: str | None = None,
) -> str:
    """Render the takeaway brief for a verified answer.

    Delegates to :mod:`synapse.brief` for anything that is actually a brief.
    Routing states are rendered here, because they are not.

    ``generated_at`` and ``document_id`` are injectable so the output is
    byte-stable for a snapshot. Left unset they are the current time and a fresh
    random identifier, which is what a real export wants and what a snapshot
    cannot use.
    """
    # Imported inside the function, not at module scope: `synapse.brief` imports
    # `synapse.answer.render`, and importing any `synapse.answer` submodule runs
    # this package's __init__, which re-exports this shim. A module-level import
    # closes that loop. Deferring it is the same technique the legacy
    # Generation shim uses, for the same reason.
    from synapse.brief.build import build_brief
    from synapse.brief.render_text import render_brief_text
    from synapse.brief.schema import UserContent

    if answer.action is AnswerAction.EMERGENCY:
        # No medical content, no sources, and the disclaimer still present.
        return "\n".join(
            [
                BRIEF_TITLE,
                "=" * len(BRIEF_TITLE),
                "",
                f"Status: {ACTION_LABELS[AnswerAction.EMERGENCY]}",
                "",
                EMERGENCY_MESSAGE,
                "",
                PERMANENT_DISCLAIMER,
            ]
        )

    brief = build_brief(
        answer,
        numbering,
        user=UserContent(topic=query.strip()[:200]),
        generated_at=generated_at,
        document_id=document_id,
    )
    rendered = render_brief_text(brief)
    if answer.action is AnswerAction.MEDICAL_STAFF:
        header, _, remainder = rendered.partition("\n\n")
        return f"{header}\n\n{STAFF_MESSAGE}\n\n{remainder}"
    return rendered


__all__ = [
    "ACTION_LABELS",
    "BRIEF_FILENAME",
    "BRIEF_TITLE",
    "render_appointment_brief",
]

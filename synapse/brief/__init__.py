"""
synapse.brief
=============
The appointment brief: a one-page artifact a patient takes into the room.

    synapse.brief.schema        AppointmentBrief and its parts, with the invariants
    synapse.brief.build         deterministic construction from a validated answer
    synapse.brief.edit          the editing rules, as pure functions
    synapse.brief.render_print  the self-contained printable document
    synapse.brief.render_text   the same brief as plain text
    synapse.brief.export        the three formats, checked, with temp-file cleanup

Four properties the package is built around:

1. **Built from typed data, never scraped.** The brief is constructed from a
   validated answer object; no renderer output is ever parsed back.
2. **An unsupported claim cannot be in it.** Enforced by the schema, not by the
   builder remembering to filter.
3. **User content stays visibly the user's.** Editing a verified claim removes
   its verified status and its citations, atomically, because the schema refuses
   the half-applied states.
4. **Nothing is stored and nothing is sent.** Export produces bytes the user
   downloads. There is no server-side persistence, no share URL, and no
   third-party delivery.
"""

from __future__ import annotations  # Postponed annotations

from synapse.brief.build import build_brief
from synapse.brief.edit import (
    EditError,
    add_question,
    edit_claim_text,
    remove_claim,
    remove_question,
    reorder_questions,
    restore_claim,
    set_notes,
    set_sections,
    set_topic,
    user_content_summary,
)
from synapse.brief.export import (
    EXPORT_WARNING,
    ExportBundle,
    ExportError,
    assert_no_secrets,
    build_export,
    load_brief_json,
    temporary_export,
)
from synapse.brief.render_print import (
    FitEstimate,
    estimate_fit,
    overflow_advice,
    render_brief_html,
)
from synapse.brief.render_text import render_brief_text
from synapse.brief.schema import (
    AppointmentBrief,
    BriefClaim,
    BriefProvenance,
    BriefQuestion,
    BriefSection,
    BriefSource,
    ContentOrigin,
    SupportLevel,
    UserContent,
    new_document_id,
)

__all__ = [
    "EXPORT_WARNING",
    "AppointmentBrief",
    "BriefClaim",
    "BriefProvenance",
    "BriefQuestion",
    "BriefSection",
    "BriefSource",
    "ContentOrigin",
    "EditError",
    "ExportBundle",
    "ExportError",
    "FitEstimate",
    "SupportLevel",
    "UserContent",
    "add_question",
    "assert_no_secrets",
    "build_brief",
    "build_export",
    "edit_claim_text",
    "estimate_fit",
    "load_brief_json",
    "new_document_id",
    "overflow_advice",
    "remove_claim",
    "remove_question",
    "render_brief_html",
    "render_brief_text",
    "reorder_questions",
    "restore_claim",
    "set_notes",
    "set_sections",
    "set_topic",
    "temporary_export",
    "user_content_summary",
]

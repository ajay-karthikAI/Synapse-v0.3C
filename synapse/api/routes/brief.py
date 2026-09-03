"""
The appointment brief: server-owned, edited by named operation only.

The rule that shapes every route here
-------------------------------------
**No endpoint accepts a brief.** Not as a whole, not partially, not "just the
claims". A client sends ``{"topic": "..."}`` or ``{"text": "..."}``; the server
applies that one operation to the brief it already holds and stores the result.

The reason is forgery. A brief carries verified claims, their support levels and
their source numbers — the output of citation verification. An endpoint that
accepted a replacement brief would let a caller submit claims that were never
retrieved, never verified and never supported, and have them printed on a
document a patient carries into an appointment with a clinician. The claims must
come from the answer layer or they must not exist, so the only writable fields
are the ones the patient genuinely authored: their topic, their notes, their own
questions, and which sections to include.

The brief is built lazily on first read, once per turn, and cached in the
session. Rebuilding would issue a new document identifier on every interaction.

Exports are produced in memory and streamed straight out. Nothing is written to
disk: an export contains the patient's own notes, and a temporary file is a
retention decision nobody made.
"""

from __future__ import annotations  # Postponed annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Response
from fastapi import Path as PathParam

from synapse.answer.render import PERMANENT_DISCLAIMER
from synapse.answer.schema import AnswerAction
from synapse.api.deps import CODE_INVALID_REQUEST, CODE_NOT_FOUND, ApiError, SessionDep
from synapse.api.models import (
    BriefClaimModel,
    BriefNotesRequest,
    BriefQuestionModel,
    BriefQuestionOrderRequest,
    BriefQuestionRequest,
    BriefResponse,
    BriefSectionsRequest,
    BriefTopicRequest,
    SourceModel,
)
from synapse.api.sessions import Session
from synapse.brief import (
    EXPORT_WARNING,
    AppointmentBrief,
    BriefSection,
    EditError,
    add_question,
    build_brief,
    build_export,
    estimate_fit,
    overflow_advice,
    remove_question,
    reorder_questions,
    set_notes,
    set_sections,
    set_topic,
)
from synapse.logging import get_logger
from synapse.service.conversation import ConversationTurn

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/turns/{turn_index}/brief", tags=["brief"])

TurnIndex = PathParam(ge=0, le=1000, description="Zero-based index of the turn.")

# Exports are text. Declared explicitly with nosniff so a browser cannot be
# talked into rendering an export as something executable.
EXPORT_TYPES: dict[str, tuple[str, str]] = {
    "html": ("text/html; charset=utf-8", "html"),
    "text": ("text/plain; charset=utf-8", "txt"),
    "json": ("application/json; charset=utf-8", "json"),
}


def _turn_of(session: Session, turn_index: int) -> ConversationTurn:
    """The turn at ``turn_index``, or a typed 404."""
    turns = session.conversation.turns
    if turn_index >= len(turns):
        raise ApiError(404, CODE_NOT_FOUND, "No such turn in this session.")
    return turns[turn_index]


def _brief_of(session: Session, turn_index: int) -> AppointmentBrief:
    """The server-owned brief, built once on first access.

    Refuses for a turn that has no brief: a failure has no answer to build from,
    and an escalation cites nothing and offers no takeaway.
    """
    existing = session.get_brief(turn_index)
    if existing is not None:
        return existing

    turn = _turn_of(session, turn_index)
    presentation = turn.outcome.presentation
    if presentation is None or presentation.answer.action is AnswerAction.EMERGENCY:
        raise ApiError(404, CODE_NOT_FOUND, "This turn has no appointment brief.")

    brief = build_brief(presentation.answer, presentation.numbering, app_version="0.1.0")
    session.set_brief(turn_index, brief)
    return brief


def _store(session: Session, turn_index: int, brief: AppointmentBrief) -> BriefResponse:
    """Persist an edited brief and render the response."""
    session.set_brief(turn_index, brief)
    return _render(brief, turn_index)


def _render(brief: AppointmentBrief, turn_index: int) -> BriefResponse:
    """The brief as the client sees it, plus the one-page fit estimate."""
    fit = estimate_fit(brief)
    return BriefResponse(
        document_id=brief.document_id,
        turn_index=turn_index,
        topic=brief.user.topic,
        notes=brief.user.notes,
        questions=[
            BriefQuestionModel(
                question_id=question.question_id,
                text=question.text,
                origin=str(question.origin),
            )
            for question in brief.questions
        ],
        claims=[
            BriefClaimModel(
                claim_id=claim.claim_id,
                text=claim.text,
                support=str(claim.support),
                source_numbers=list(claim.source_numbers),
                included=True,
            )
            for claim in brief.claims
        ],
        sources=[
            SourceModel(
                number=source.number,
                source_id=source.source_id,
                title=source.title,
                url="",
                relevance=None,
            )
            for source in brief.sources
        ],
        included_sections=[str(section) for section in brief.included_sections],
        available_sections=[str(section) for section in BriefSection],
        fits_one_page=fit.fits,
        fill_ratio=fit.fill_ratio,
        overflow_advice=list(overflow_advice(brief)) if not fit.fits else [],
        export_warning=EXPORT_WARNING,
        disclaimer=PERMANENT_DISCLAIMER,
    )


@router.get("", response_model=BriefResponse, summary="Read the brief")
def read_brief(session: SessionDep, turn_index: int = TurnIndex) -> BriefResponse:
    """Build the brief on first access, then return the stored one."""
    return _render(_brief_of(session, turn_index), turn_index)


@router.put("/topic", response_model=BriefResponse, summary="Set the topic")
def put_topic(
    payload: BriefTopicRequest, session: SessionDep, turn_index: int = TurnIndex
) -> BriefResponse:
    """The patient's own 'what I want to talk about' line."""
    brief = _brief_of(session, turn_index)
    return _store(session, turn_index, _edit(set_topic, brief, payload.topic))


@router.put("/notes", response_model=BriefResponse, summary="Set the notes")
def put_notes(
    payload: BriefNotesRequest, session: SessionDep, turn_index: int = TurnIndex
) -> BriefResponse:
    """The patient's own notes."""
    brief = _brief_of(session, turn_index)
    return _store(session, turn_index, _edit(set_notes, brief, payload.notes))


@router.post("/questions", response_model=BriefResponse, summary="Add a question")
def post_question(
    payload: BriefQuestionRequest, session: SessionDep, turn_index: int = TurnIndex
) -> BriefResponse:
    """Add a question the patient wrote. Marked as theirs, never as evidence."""
    brief = _brief_of(session, turn_index)
    return _store(session, turn_index, _edit(add_question, brief, payload.text))


@router.delete(
    "/questions/{question_id}", response_model=BriefResponse, summary="Remove a question"
)
def delete_question(
    session: SessionDep, question_id: str, turn_index: int = TurnIndex
) -> BriefResponse:
    """Remove one question by identifier."""
    brief = _brief_of(session, turn_index)
    return _store(session, turn_index, _edit(remove_question, brief, question_id))


@router.put("/questions/order", response_model=BriefResponse, summary="Reorder questions")
def put_question_order(
    payload: BriefQuestionOrderRequest, session: SessionDep, turn_index: int = TurnIndex
) -> BriefResponse:
    """Reorder by identifier. The set must match exactly; nothing is added here."""
    brief = _brief_of(session, turn_index)
    return _store(session, turn_index, _edit(reorder_questions, brief, payload.order))


@router.put("/sections", response_model=BriefResponse, summary="Choose sections")
def put_sections(
    payload: BriefSectionsRequest, session: SessionDep, turn_index: int = TurnIndex
) -> BriefResponse:
    """Choose which sections the export includes.

    The disclaimer is always included and cannot be removed — the section list
    the schema exposes does not contain it.
    """
    brief = _brief_of(session, turn_index)
    try:
        sections = [BriefSection(value) for value in payload.sections]
    except ValueError:
        raise ApiError(400, CODE_INVALID_REQUEST, "Unknown section.") from None
    return _store(session, turn_index, _edit(set_sections, brief, sections))


@router.get("/export/{fmt}", summary="Export the brief")
def export_brief(session: SessionDep, fmt: str, turn_index: int = TurnIndex) -> Response:
    """Render the brief and return it as an attachment.

    Built in memory and streamed out. Nothing touches disk.
    """
    if fmt not in EXPORT_TYPES:
        raise ApiError(404, CODE_NOT_FOUND, "Unknown export format.")
    brief = _brief_of(session, turn_index)
    bundle = build_export(brief)
    media_type, extension = EXPORT_TYPES[fmt]
    body = {"html": bundle.html, "text": bundle.text, "json": bundle.json_text}[fmt]

    # The filename is built from the brief's own document id, which is generated
    # by this application and matches a fixed grammar. No client-supplied string
    # reaches the header, so a quote or a newline cannot be injected into it.
    filename = f"appointment-brief-{_safe_id(bundle.document_id)}.{extension}"
    return Response(
        content=body,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


def _safe_id(document_id: str) -> str:
    """Keep only characters that are safe in a quoted filename."""
    return "".join(
        character for character in document_id if character.isalnum() or character in "-_"
    )


def _edit(
    operation: Callable[[AppointmentBrief, Any], AppointmentBrief],
    brief: AppointmentBrief,
    value: Any,
) -> AppointmentBrief:
    """Apply one edit, turning a rejected edit into a typed 400.

    ``EditError`` is the brief layer refusing something — too many questions, an
    unknown identifier, an over-long note. It is a client mistake, not a fault,
    and it carries no medical content.
    """
    try:
        return operation(brief, value)
    except EditError as exc:
        raise ApiError(400, CODE_INVALID_REQUEST, str(exc)) from None

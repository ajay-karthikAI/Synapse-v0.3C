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

One brief per conversation
--------------------------
There used to be one per turn, at ``/v1/turns/{turn_index}/brief``. A patient
asking four questions ended up with four documents, each a partial account of
the visit, and none of them the sheet of paper they actually needed. So the
brief is session-scoped: built lazily on first read, spanning every turn, and
rebuilt when a new turn arrives.

A rebuild is not a reset. It keeps the document identifier, the patient's topic
and notes, the questions they added themselves, and the sections they chose —
losing a patient's typed notes because they asked a fifth question would be a
data-loss bug wearing the clothes of a cache refresh.

Exports are produced in memory and streamed straight out. Nothing is written to
disk: an export contains the patient's own notes, and a temporary file is a
retention decision nobody made.
"""

from __future__ import annotations  # Postponed annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Response

from synapse.answer.render import PERMANENT_DISCLAIMER
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
    DEFAULT_SECTIONS,
    EXPORT_WARNING,
    MAX_QUESTIONS,
    AppointmentBrief,
    BriefSection,
    ContentOrigin,
    EditError,
    add_question,
    build_conversation_brief,
    build_export,
    build_pdf_export,
    estimate_fit,
    overflow_advice,
    remove_question,
    reorder_questions,
    set_notes,
    set_sections,
    set_topic,
)
from synapse.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/brief", tags=["brief"])

# Text exports, declared explicitly with nosniff so a browser cannot be talked
# into rendering an export as something executable. The PDF is served by the
# same route but built separately: it is bytes, not text, and it is checked
# against the structured brief rather than against its own compressed output.
EXPORT_TYPES: dict[str, tuple[str, str]] = {
    "html": ("text/html; charset=utf-8", "html"),
    "text": ("text/plain; charset=utf-8", "txt"),
    "json": ("application/json; charset=utf-8", "json"),
}
PDF_FORMAT = "pdf"
PDF_MEDIA_TYPE = "application/pdf"


def _carry_forward(rebuilt: AppointmentBrief, previous: AppointmentBrief) -> AppointmentBrief:
    """Move the patient's own work onto a freshly built brief.

    Rebuilt from the conversation, ``rebuilt`` knows about the research and
    nothing about the person reading it. Four things belong to the patient and
    have to survive: the document identifier, so a printout keeps its reference;
    their topic and notes; the questions they wrote themselves; and the sections
    they chose to include.

    Their own questions are appended after the suggested ones rather than
    merged into them, because their order relative to the evidence questions was
    never meaningful — and appending is the one arrangement that cannot silently
    drop one.
    """
    own_questions = [
        question
        for question in previous.questions
        if question.origin is ContentOrigin.USER_AUTHORED
    ]
    questions = [*rebuilt.questions, *own_questions][:MAX_QUESTIONS]
    return rebuilt.model_copy(
        update={
            "document_id": previous.document_id,
            "user": previous.user,
            "questions": questions,
            "included_sections": list(previous.included_sections),
        }
    )


def _brief_of(session: Session) -> AppointmentBrief:
    """The conversation's brief, built on first access and rebuilt as it grows.

    Refuses only when there is nothing to build from. A conversation of nothing
    but escalations and failures has no recap and no questions, and an empty
    document with a disclaimer on it is not a brief.
    """
    turn_count = len(session.conversation.turns)
    existing = session.get_conversation_brief()
    # The transcript covers one entry per turn, so its length is exactly how
    # much of the conversation the cached brief has seen.
    if existing is not None and len(existing.transcript) == turn_count:
        return existing

    rebuilt = build_conversation_brief(session.conversation, app_version="0.1.0")
    if not rebuilt.summary and not rebuilt.questions:
        raise ApiError(404, CODE_NOT_FOUND, "This conversation has no appointment brief yet.")

    brief = _carry_forward(rebuilt, existing) if existing is not None else rebuilt
    session.set_conversation_brief(brief)
    return brief


def _store(session: Session, brief: AppointmentBrief) -> BriefResponse:
    """Persist an edited brief and render the response."""
    session.set_conversation_brief(brief)
    return _render(brief)


def _render(brief: AppointmentBrief) -> BriefResponse:
    """The brief as the client sees it, plus the one-page fit estimate."""
    fit = estimate_fit(brief)
    return BriefResponse(
        document_id=brief.document_id,
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
        default_sections=[str(section) for section in DEFAULT_SECTIONS],
        transcript_turn_count=len(brief.transcript),
        fits_one_page=fit.fits,
        fill_ratio=fit.fill_ratio,
        overflow_advice=list(overflow_advice(brief)) if not fit.fits else [],
        export_warning=EXPORT_WARNING,
        disclaimer=PERMANENT_DISCLAIMER,
    )


@router.get("", response_model=BriefResponse, summary="Read the brief")
def read_brief(session: SessionDep) -> BriefResponse:
    """Build the brief on first access, then return the stored one."""
    return _render(_brief_of(session))


@router.put("/topic", response_model=BriefResponse, summary="Set the topic")
def put_topic(payload: BriefTopicRequest, session: SessionDep) -> BriefResponse:
    """The patient's own 'what I want to talk about' line."""
    return _store(session, _edit(set_topic, _brief_of(session), payload.topic))


@router.put("/notes", response_model=BriefResponse, summary="Set the notes")
def put_notes(payload: BriefNotesRequest, session: SessionDep) -> BriefResponse:
    """The patient's own notes."""
    return _store(session, _edit(set_notes, _brief_of(session), payload.notes))


@router.post("/questions", response_model=BriefResponse, summary="Add a question")
def post_question(payload: BriefQuestionRequest, session: SessionDep) -> BriefResponse:
    """Add a question the patient wrote. Marked as theirs, never as evidence."""
    return _store(session, _edit(add_question, _brief_of(session), payload.text))


@router.delete(
    "/questions/{question_id}", response_model=BriefResponse, summary="Remove a question"
)
def delete_question(session: SessionDep, question_id: str) -> BriefResponse:
    """Remove one question by identifier."""
    return _store(session, _edit(remove_question, _brief_of(session), question_id))


@router.put("/questions/order", response_model=BriefResponse, summary="Reorder questions")
def put_question_order(payload: BriefQuestionOrderRequest, session: SessionDep) -> BriefResponse:
    """Reorder by identifier. The set must match exactly; nothing is added here."""
    return _store(session, _edit(reorder_questions, _brief_of(session), payload.order))


@router.put("/sections", response_model=BriefResponse, summary="Choose sections")
def put_sections(payload: BriefSectionsRequest, session: SessionDep) -> BriefResponse:
    """Choose which sections the export includes.

    This is how the two add-ons are turned on: the patient posts the default
    sections plus ``transcript``, or plus the research sections, or both. The
    disclaimer is always included and cannot be removed -- the section list the
    schema exposes does not contain it.
    """
    try:
        sections = [BriefSection(value) for value in payload.sections]
    except ValueError:
        raise ApiError(400, CODE_INVALID_REQUEST, "Unknown section.") from None
    return _store(session, _edit(set_sections, _brief_of(session), sections))


def _attachment(body: str | bytes, *, media_type: str, filename: str) -> Response:
    """An export, as a download and nothing else.

    ``nosniff`` because a browser that guesses at a type can be talked into
    executing one, and ``no-store`` because this is a patient's health document
    passing through a cache that has no business keeping it.
    """
    return Response(
        content=body,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.get("/export/{fmt}", summary="Export the brief")
def export_brief(session: SessionDep, fmt: str) -> Response:
    """Render the brief and return it as an attachment.

    Built in memory and streamed out. Nothing touches disk.

    ``pdf`` is what the download button asks for; the three text formats remain
    for a desktop browser, for pasting into a portal message, and for carrying
    the structured brief somewhere else.
    """
    brief = _brief_of(session)

    # The filename is built from the brief's own document id, which is generated
    # by this application and matches a fixed grammar. No client-supplied string
    # reaches the header, so a quote or a newline cannot be injected into it.
    if fmt == PDF_FORMAT:
        export = build_pdf_export(brief)
        return _attachment(
            export.pdf,
            media_type=PDF_MEDIA_TYPE,
            filename=f"appointment-brief-{_safe_id(export.document_id)}.pdf",
        )

    if fmt not in EXPORT_TYPES:
        raise ApiError(404, CODE_NOT_FOUND, "Unknown export format.")
    bundle = build_export(brief)
    media_type, extension = EXPORT_TYPES[fmt]
    body = {"html": bundle.html, "text": bundle.text, "json": bundle.json_text}[fmt]
    return _attachment(
        body,
        media_type=media_type,
        filename=f"appointment-brief-{_safe_id(bundle.document_id)}.{extension}",
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

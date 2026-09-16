"""
synapse.api.models
==================
The wire contract: typed in, typed out, discriminated at the top.

Every response a client can receive is declared here as a pydantic model, so
the OpenAPI document is generated from the same definitions the server actually
validates against rather than written alongside them and allowed to drift.

The important type is :data:`TurnEnvelope` — a **discriminated union** on
``kind``. A client cannot receive "an answer that might be a failure": it
receives exactly one of four shapes, and the tag says which before any field is
read. That is the wire form of the invariant
:class:`~synapse.ui.pipeline.TurnOutcome` already enforces in Python, and it
exists for the same reason: a caller must not be able to forget to check.

    answer        a validated answer, possibly an abstention
    emergency     a red flag; cites nothing, carries no evidence
    insufficient  no verified evidence; a designed state, not an error
    failure       a fault; fixed copy and a typed code

**No model here can carry unvalidated prose.** Every string on a failure path
comes from an application constant. There is no field into which a provider
message, an exception, or unverified model output could be placed — which is
what makes "the API cannot leak model output on a failure" a property of the
types rather than of the routes.

Field names are snake_case and stable; they are the contract the Next.js client
is written against.
"""

from __future__ import annotations  # Postponed annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

from synapse.api.config import MAX_QUERY_CHARS


class _Strict(BaseModel):
    """Base for every wire model: unknown fields are an error, not a shrug.

    ``extra="forbid"`` on requests means a client typo is reported rather than
    silently ignored — the failure mode where a caller sets ``client_request_id``
    as ``clientRequestId`` and quietly loses idempotency.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class LoginRequest(_Strict):
    """The shared passcode, exchanged for an access token."""

    passcode: str = Field(min_length=1, max_length=512, description="The shared access passcode.")


class TurnRequest(_Strict):
    """One patient question.

    ``query`` is unrestricted free text by design — a patient must be able to
    describe a symptom in their own words. It is bounded in *length* only, and
    the interface carries a persistent warning not to enter identifiers.
    """

    query: str = Field(
        min_length=1,
        max_length=MAX_QUERY_CHARS,
        description="The patient's question, in their own words.",
    )
    client_request_id: str = Field(
        default="",
        max_length=128,
        description=(
            "Client-generated id for idempotent retry. Re-sending the same id "
            "replays the previous result instead of running a second turn."
        ),
    )


class BriefTopicRequest(_Strict):
    """Replace the patient's own 'what I want to talk about' line."""

    topic: str = Field(default="", max_length=2000)


class BriefNotesRequest(_Strict):
    """Replace the patient's own notes."""

    notes: str = Field(default="", max_length=4000)


class BriefQuestionRequest(_Strict):
    """Add one question the patient wrote themselves."""

    text: str = Field(min_length=1, max_length=500)


class BriefQuestionOrderRequest(_Strict):
    """Reorder the questions by identifier."""

    order: list[str] = Field(min_length=0, max_length=64)


class BriefSectionsRequest(_Strict):
    """Choose which sections the export includes."""

    sections: list[str] = Field(min_length=0, max_length=16)


# ---------------------------------------------------------------------------
# Shared response pieces
# ---------------------------------------------------------------------------


class SourceModel(_Strict):
    """One numbered source, as it appears inline and in the source panel."""

    number: int = Field(ge=1, description="Stable display number, assigned from retrieval order.")
    source_id: str
    title: str
    url: str = Field(description="Empty when the recorded link was not http(s).")
    relevance: float | None = Field(
        default=None,
        description="Retrieval relevance, 0..1. NOT a confidence and NOT a quality grade.",
    )


class ClaimModel(_Strict):
    """One displayed claim. Withheld claims never appear here at all."""

    claim_id: str
    text: str
    source_numbers: list[int]
    support: Literal["supported", "partially_supported"] = Field(
        description="Partial support is shown and marked, never silently corrected."
    )


class ExcerptModel(_Strict):
    """A verbatim span the verifier matched, under its source's number."""

    source_number: int
    chunk_id: str
    quote: str


class InsufficientModel(_Strict):
    """The complete patient-facing content for the insufficient-evidence state."""

    reason: str = Field(description="Operator-facing reason code.")
    heading: str
    message: str
    not_a_judgement: str = Field(
        description="Prevents 'we found nothing' being read as 'there is nothing wrong'."
    )
    next_steps: list[str]


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------


class AnswerEnvelope(_Strict):
    """A validated answer. ``action`` distinguishes the three answer states."""

    kind: Literal["answer"] = "answer"
    action: Literal["answer", "abstain", "medical_staff"]
    summary: str
    claims: list[ClaimModel]
    doctor_evaluation: str
    questions_for_doctor: list[str]
    limitations: list[str]
    sources: list[SourceModel]
    excerpts: list[ExcerptModel]
    staff_message: str = Field(
        default="", description="Set only for the medical_staff action; application copy."
    )
    insufficient: InsufficientModel | None = Field(
        default=None, description="Populated on an abstention, which IS the insufficient state."
    )
    resolved_query: str = Field(
        default="",
        description=(
            "What a follow-up was resolved to for retrieval. Empty unless a "
            "rewrite actually happened. Model-generated: render as text."
        ),
    )
    brief_available: bool = Field(
        description="Whether an appointment brief can be built from this turn."
    )
    turn_index: int = Field(ge=0)
    disclaimer: str = Field(description="Application-owned. No model contributes to it.")


class EmergencyEnvelope(_Strict):
    """A red flag. Produced before retrieval, so it cites nothing."""

    kind: Literal["emergency"] = "emergency"
    message: str = Field(description="Application copy. The one state that should interrupt.")
    brief_available: Literal[False] = False
    turn_index: int = Field(ge=0)
    disclaimer: str


class InsufficientEnvelope(_Strict):
    """No verified evidence. A designed state, not an error."""

    kind: Literal["insufficient"] = "insufficient"
    detail: InsufficientModel
    brief_available: Literal[False] = False
    turn_index: int = Field(ge=0)
    disclaimer: str


class FailureEnvelope(_Strict):
    """A fault. Fixed copy plus a typed code, and nothing else, ever."""

    kind: Literal["failure"] = "failure"
    code: str = Field(description="A closed AnswerFailureCode value.")
    message: str = Field(description="The single patient-facing message, identical for every code.")
    brief_available: Literal[False] = False
    turn_index: int = Field(ge=0)


TurnEnvelope = Annotated[
    AnswerEnvelope | EmergencyEnvelope | InsufficientEnvelope | FailureEnvelope,
    Field(discriminator="kind"),
]


class StreamedTurnEnvelope(RootModel[TurnEnvelope]):
    """The payload of the single terminal ``envelope`` server-sent event.

    A wrapper exists only so the union reaches the OpenAPI document. The stream
    route returns an SSE body rather than a JSON model, and without something
    declared as its response the four envelope schemas would never be emitted
    into ``components`` — leaving a generated client with no type for the one
    message that matters.
    """


# ---------------------------------------------------------------------------
# Other responses
# ---------------------------------------------------------------------------


class HealthResponse(_Strict):
    """Liveness. Says nothing about whether the artifact loaded."""

    status: Literal["ok"] = "ok"
    version: str


class ReadyResponse(_Strict):
    """Readiness. ``ready`` is false until every startup gate has passed."""

    ready: bool
    state: str
    failure_code: str | None = None
    detail: str = Field(default="", description="An exception TYPE name. Never a message.")
    artifact: dict[str, object] = Field(default_factory=dict)


class LoginResponse(_Strict):
    """A new session. The token is also set as an HttpOnly cookie."""

    session_id: str
    expires_in_seconds: int


class SessionResponse(_Strict):
    """What the server holds for this session. No conversation content."""

    session_id: str
    turn_count: int
    max_turns: int
    turn_active: bool
    idle_ttl_seconds: int


class DeletedResponse(_Strict):
    """Confirmation that state was discarded."""

    deleted: bool


class BriefQuestionModel(_Strict):
    """One question in the brief, with where it came from."""

    question_id: str
    text: str
    origin: str = Field(description="Whether the patient or the system wrote it.")


class BriefClaimModel(_Strict):
    """One claim in the brief, with its support level."""

    claim_id: str
    text: str
    support: str
    source_numbers: list[int]
    included: bool


class BriefResponse(_Strict):
    """The server-owned brief. A client edits it by named operation only.

    One brief per conversation, not one per answer, so there is no turn index
    here: a patient leaves an appointment with one sheet of paper, and the
    document is rebuilt to span every turn as the conversation grows.
    """

    document_id: str
    topic: str
    notes: str
    questions: list[BriefQuestionModel]
    claims: list[BriefClaimModel]
    sources: list[SourceModel]
    included_sections: list[str]
    available_sections: list[str]
    default_sections: list[str] = Field(
        description="What the brief prints unless the patient asks for more: the recap."
    )
    transcript_turn_count: int = Field(
        description=(
            "Exchanges the transcript add-on would print. The turns themselves are not "
            "returned: the client already has them on screen, and re-sending the "
            "conversation to render a checkbox is a copy of the patient's questions "
            "travelling for no reason."
        )
    )
    fits_one_page: bool
    fill_ratio: float
    overflow_advice: list[str]
    export_warning: str
    disclaimer: str


class TransparencyResponse(_Strict):
    """What this deployment is running, and what it has NOT been shown to be."""

    application_version: str
    sources: dict[str, object]
    evaluation: dict[str, object]
    privacy: dict[str, object]
    system: dict[str, object]
    disclosures: list[str] = Field(
        description="Mandatory statements. Never empty; never softened by configuration."
    )


class ErrorResponse(_Strict):
    """A refusal. Typed code, fixed message, no exception text."""

    code: str
    message: str


__all__ = [
    "AnswerEnvelope",
    "BriefClaimModel",
    "BriefNotesRequest",
    "BriefQuestionModel",
    "BriefQuestionOrderRequest",
    "BriefQuestionRequest",
    "BriefResponse",
    "BriefSectionsRequest",
    "BriefTopicRequest",
    "ClaimModel",
    "DeletedResponse",
    "EmergencyEnvelope",
    "ErrorResponse",
    "ExcerptModel",
    "FailureEnvelope",
    "HealthResponse",
    "InsufficientEnvelope",
    "InsufficientModel",
    "LoginRequest",
    "LoginResponse",
    "ReadyResponse",
    "SessionResponse",
    "SourceModel",
    "StreamedTurnEnvelope",
    "TransparencyResponse",
    "TurnEnvelope",
    "TurnRequest",
]

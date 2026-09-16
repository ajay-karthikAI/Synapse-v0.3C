"""
synapse.brief.schema
====================
The appointment brief as validated typed data.

The brief is built from this model and rendered from it. It is never assembled
by scraping rendered HTML, which is the failure mode this schema exists to
prevent: a scraper picks up whatever the page happens to contain, including
things that were never meant to leave the process, and it silently loses the
distinction between a claim a verifier checked and a sentence a user typed.

Three invariants the model enforces rather than documents
---------------------------------------------------------
1. **An unsupported claim cannot be in a brief.** ``BriefClaim`` rejects a
   support status of ``unsupported`` at construction. There is no path, not even
   a deliberate one, from a withheld claim to an exported artifact.
2. **Every citation marker resolves.** A claim citing source ``[3]`` when the
   brief carries two sources is a validation error, not a rendering artefact.
3. **Provenance and authorship are always distinguishable.** Every piece of text
   carries a :class:`ContentOrigin`, so a renderer cannot accidentally present a
   user's own note with the authority of a verified claim.

What is deliberately absent
---------------------------
No API key, no telemetry identifier, no session identifier, no provider
metadata, no prompt text. Those are not filtered on export; there is nowhere in
this model to put them, which is a stronger guarantee than a filter someone has
to remember to run.

The conversation itself used to be on that list, and is not any more.
:class:`BriefTranscriptTurn` carries the patient's own questions and the answer
summaries they were given, because a brief that recaps the visit is the thing a
patient actually carries to an appointment. The narrower guarantee that replaces
it: a transcript turn has exactly two text fields and a status, so there is
still nowhere to put a prompt, an identifier or a provider name. It is also the
one section that is off by default -- the patient opts in, per export, through
:attr:`BriefSection.TRANSCRIPT`.

The document identifier is random (``secrets.token_hex``) and carries no
information about the user, the question, the session or the time. It exists so
a patient and a clinician can refer to the same sheet of paper.
"""

from __future__ import annotations  # Postponed annotations

import re
import secrets
from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from pydantic import Field, field_validator, model_validator

from synapse.schemas.base import SynapseModel, VersionedModel, require_timezone_aware

DOCUMENT_ID_BYTES = (
    6  # 12 hex characters: enough to distinguish two printouts, too few to be a tracking key
)
DOCUMENT_ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")

MAX_TOPIC_CHARS = 200
MAX_NOTE_CHARS = 1200
MAX_QUESTION_CHARS = 300
MAX_QUESTIONS = 12  # A brief with more than this is not a one-page brief

# Transcript bounds. A session holds at most MAX_TURNS_PER_SESSION turns, so the
# ceiling here is not what stops the document growing without limit -- the
# session does that. It is a second bound, and it is the one that survives if
# the session ceiling is ever raised.
MAX_TRANSCRIPT_TURNS = 20
MAX_TRANSCRIPT_QUESTION_CHARS = 1000
MAX_TRANSCRIPT_ANSWER_CHARS = 4000


def new_document_id() -> str:
    """A fresh random document identifier.

    Random rather than derived. A digest of the query or the timestamp would
    make the printed sheet a lookup key for something, and the whole point of
    the identifier is that it is a label, not an index.
    """
    return secrets.token_hex(DOCUMENT_ID_BYTES)


class ContentOrigin(StrEnum):
    """Who wrote a piece of text, and whether anything verified it.

    The distinction the whole brief turns on. ``USER_EDITED`` exists because a
    user may take a verified sentence and change it: the moment they do, it is
    no longer the sentence the verifier checked, and continuing to present it as
    source-supported would be a fabricated citation with extra steps.
    """

    VERIFIED_EVIDENCE = (
        "verified_evidence"  # Produced by the answer layer and checked against a cited chunk
    )
    USER_AUTHORED = "user_authored"  # Typed by the user; never claimed to be evidence-backed
    USER_EDITED = (
        "user_edited"  # Started as evidence, then changed. Verification no longer applies.
    )


class SupportLevel(StrEnum):
    """How well the evidence backed a claim, as the verifier found it.

    Mirrors :class:`synapse.answer.schema.SupportStatus` minus the unsupported
    case, which cannot reach a brief at all.
    """

    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"


class BriefSection(StrEnum):
    """Sections a user may omit before exporting.

    The disclaimer is deliberately not a member: it is not optional, and a
    section list that could contain it would invite a caller to drop it.
    """

    TOPIC = "topic"
    NOTES = "notes"
    SUMMARY = "summary"
    CLAIMS = "claims"
    QUESTIONS = "questions"
    SOURCES = "sources"
    LIMITATIONS = "limitations"
    TRANSCRIPT = "transcript"


# What a brief prints unless the patient asks for more.
#
# Every section was on by default until this became a document aimed at a
# patient rather than at the engineer who built it. The default was nine
# sections deep -- verified claims with citation markers, a numbered source list
# carrying PMIDs and DOIs, the answer layer's limitations, and a provenance
# block naming corpus and index versions. All of that is real and none of it is
# what somebody reads in a waiting room.
#
# So the default is now the recap: what was discussed, and what to ask. TOPIC
# and NOTES stay in it because they are the patient's own words and render as
# nothing at all when empty, which is the common case.
DEFAULT_SECTIONS: tuple[BriefSection, ...] = (
    BriefSection.TOPIC,
    BriefSection.NOTES,
    BriefSection.SUMMARY,
    BriefSection.QUESTIONS,
)

# The add-ons, off unless asked for. Two groups, which is how the interface
# offers them: the transcript, and the research behind the summary.
TRANSCRIPT_SECTIONS: tuple[BriefSection, ...] = (BriefSection.TRANSCRIPT,)
RESEARCH_SECTIONS: tuple[BriefSection, ...] = (
    BriefSection.CLAIMS,
    BriefSection.SOURCES,
    BriefSection.LIMITATIONS,
)


class BriefSource(SynapseModel):
    """One cited source, as it appears in the brief's source list.

    Fields are copied from governed metadata (:mod:`synapse.evidence.metadata`)
    and every optional one renders as absent when missing. Nothing here is
    derived or inferred.
    """

    number: int = Field(ge=1, description="Display number, stable across every surface.")
    source_id: str = Field(min_length=1, description="Stable document identifier.")
    title: str = Field(default="", description="Title as recorded.")
    container: str = Field(default="", description="Journal or issuing organisation.")
    publication_date: str = Field(
        default="", description="Pre-formatted to the recorded precision."
    )
    revision_date: str = Field(default="")
    identifier: str = Field(
        default="", description="PMID, DOI or guideline identifier, as recorded."
    )
    url: str = Field(default="", description="Canonical link. Validated https, or empty.")
    evidence_type_label: str = Field(default="", description="Plain-language evidence type.")
    review_label: str = Field(default="", description="Plain-language review state.")


class BriefClaim(SynapseModel):
    """One statement in the brief, with its citations and its origin."""

    claim_id: str = Field(min_length=1, description="Identifier from the answer layer.")
    text: str = Field(min_length=1, description="The statement as it will be printed.")
    source_numbers: list[int] = Field(
        default_factory=list, description="Display numbers of the sources this claim cites."
    )
    support: SupportLevel = Field(description="How the verifier rated it.")
    origin: ContentOrigin = Field(default=ContentOrigin.VERIFIED_EVIDENCE)
    original_text: str = Field(
        default="", description="The verified wording, retained when a user edits a claim."
    )
    original_source_numbers: list[int] = Field(
        default_factory=list,
        description=(
            "Markers that belonged to the verified wording. Kept so the original line can "
            "still show its provenance; never applied to the edited text."
        ),
    )

    @model_validator(mode="after")
    def _validate_claim(self) -> BriefClaim:
        """Enforce the rules that keep an edited claim honest."""
        if self.origin is ContentOrigin.USER_AUTHORED:
            # A user-authored sentence is a note, not a claim. It belongs in
            # notes, where nothing implies a source checked it.
            raise ValueError("a brief claim cannot be user-authored; use notes instead")
        if self.origin is ContentOrigin.USER_EDITED:
            if not self.original_text:
                raise ValueError("an edited claim must retain the verified wording it replaced")
            if self.source_numbers:
                # Requirement 5. The citation belonged to the original sentence.
                raise ValueError("an edited claim must not carry citation markers")
        if self.origin is ContentOrigin.VERIFIED_EVIDENCE and not self.source_numbers:
            raise ValueError("a verified claim must cite at least one source")
        return self

    @property
    def is_verified(self) -> bool:
        """True when this is still the wording the verifier checked."""
        return self.origin is ContentOrigin.VERIFIED_EVIDENCE

    def markers(self) -> str:
        """Citation markers as printed, e.g. ``[1][3]``."""
        return "".join(f"[{number}]" for number in self.source_numbers)

    def original_markers(self) -> str:
        """Markers for the wording this claim replaced, if it replaced one."""
        return "".join(f"[{number}]" for number in self.original_source_numbers)


class BriefQuestion(SynapseModel):
    """One question for the appointment.

    Questions assert nothing and cite nothing by design, so a user-authored
    question is a first-class member here in a way a user-authored claim is not.
    """

    question_id: str = Field(min_length=1, description="Stable within the brief.")
    text: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    origin: ContentOrigin = Field(default=ContentOrigin.VERIFIED_EVIDENCE)

    @field_validator("text")
    @classmethod
    def _strip(cls, value: str) -> str:
        """Trim incidental whitespace so reordering cannot introduce drift."""
        return value.strip()


class UserContent(SynapseModel):
    """What the user typed. Always rendered as theirs."""

    topic: str = Field(default="", max_length=MAX_TOPIC_CHARS)
    notes: str = Field(default="", max_length=MAX_NOTE_CHARS)

    @property
    def is_empty(self) -> bool:
        """True when the user has added nothing of their own."""
        return not (self.topic.strip() or self.notes.strip())


class TurnStatus(StrEnum):
    """What happened on one turn, as the transcript reports it.

    A transcript that printed only the answered turns would be a flattering
    edit of the visit, and the omission would fall hardest on the turn that
    mattered most: an escalation is the one exchange a clinician most needs to
    see. So every turn appears, and the ones that produced no answer say why.
    """

    ANSWERED = "answered"
    NO_EVIDENCE = "no_evidence"  # Abstained: the sources had nothing to support an answer
    CLINICIAN_REFERRAL = "clinician_referral"  # Routed to a human clinician rather than answered
    URGENT_ADVICE = "urgent_advice"  # The red-flag detector fired; no research was searched
    UNAVAILABLE = "unavailable"  # The turn failed; there is no answer to recap


# What a transcript prints for a turn that produced no answer, keyed by status.
#
# Defined once, here with the enum, because both renderers need it and two
# renderers each wording it themselves is how the PDF and the plain text end up
# describing the same escalation differently. Stated as what Synapse did, never
# as advice: advice on a printed sheet is advice nobody reviewed.
STATUS_NOTES: dict[TurnStatus, str] = {
    TurnStatus.NO_EVIDENCE: "Synapse found no research it could answer this from.",
    TurnStatus.CLINICIAN_REFERRAL: "Synapse did not answer this and suggested asking a clinician.",
    TurnStatus.URGENT_ADVICE: (
        "Synapse did not answer this. It flagged the wording as possibly urgent and "
        "advised contacting a clinic or emergency number."
    ),
    TurnStatus.UNAVAILABLE: "This question could not be answered because of a technical fault.",
}


class BriefTranscriptTurn(SynapseModel):
    """One exchange, as it appears in the optional transcript.

    Two text fields and a status, and that is the whole model. The narrowness is
    the point: there is no field here for a prompt, a request identifier, a
    model name or a retrieved chunk, so a transcript cannot carry one however
    the renderer is changed later.

    ``answer`` is the summary the patient was shown, not the claim list. The
    claims live in :attr:`AppointmentBrief.claims` with their citation markers,
    under the research add-on, so a statement never appears with the authority
    of a verified claim in one place and without it in another.
    """

    index: int = Field(ge=0, description="Zero-based position in the conversation.")
    question: str = Field(
        min_length=1,
        max_length=MAX_TRANSCRIPT_QUESTION_CHARS,
        description="What the patient asked, in their own words.",
    )
    answer: str = Field(
        default="",
        max_length=MAX_TRANSCRIPT_ANSWER_CHARS,
        description="The summary the patient was shown. Empty when none was produced.",
    )
    status: TurnStatus = Field(description="What the turn produced.")

    @field_validator("question", "answer")
    @classmethod
    def _strip(cls, value: str) -> str:
        """Trim incidental whitespace."""
        return value.strip()

    @model_validator(mode="after")
    def _validate_turn(self) -> BriefTranscriptTurn:
        """An answered turn has an answer; the others must not invent one."""
        if self.status is TurnStatus.ANSWERED and not self.answer:
            raise ValueError("an answered turn must carry the summary it produced")
        return self


class BriefProvenance(SynapseModel):
    """Which build, which sources, which index produced this brief."""

    generated_at: datetime = Field(description="When the brief was created (timezone-aware).")
    app_version: str = Field(default="", description="Synapse version.")
    source_pack_version: str = Field(default="", description="Governed source pack version.")
    source_pack_id: str = Field(default="")
    corpus_version: str = Field(default="")
    index_version: str = Field(default="")

    @model_validator(mode="after")
    def _validate(self) -> BriefProvenance:
        """A naive timestamp cannot be compared across machines."""
        require_timezone_aware(self.generated_at, "generated_at")
        return self


class AppointmentBrief(VersionedModel):
    """The complete one-page brief."""

    SCHEMA_NAME: ClassVar[str] = "appointment_brief"
    SCHEMA_VERSION: ClassVar[str] = "1.0"

    document_id: str = Field(
        default_factory=new_document_id,
        description="Random, non-identifying label for this printout.",
    )
    user: UserContent = Field(default_factory=UserContent)
    summary: str = Field(
        default="", description="Evidence-grounded overview from the answer layer."
    )
    claims: list[BriefClaim] = Field(default_factory=list)
    questions: list[BriefQuestion] = Field(default_factory=list, max_length=MAX_QUESTIONS)
    sources: list[BriefSource] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    transcript: list[BriefTranscriptTurn] = Field(
        default_factory=list,
        max_length=MAX_TRANSCRIPT_TURNS,
        description="Every exchange in the conversation. Printed only as an opt-in add-on.",
    )
    disclaimer: str = Field(min_length=1, description="The permanent disclaimer. Never optional.")
    provenance: BriefProvenance
    included_sections: list[BriefSection] = Field(
        default_factory=lambda: list(DEFAULT_SECTIONS),
        description=(
            "Sections the user chose to keep, defaulting to the recap. The disclaimer is "
            "not a member."
        ),
    )

    @field_validator("document_id")
    @classmethod
    def _validate_document_id(cls, value: str) -> str:
        """Reject an identifier that could carry meaning."""
        if not DOCUMENT_ID_PATTERN.match(value):
            raise ValueError("document_id must be 12 random hex characters")
        return value

    @model_validator(mode="after")
    def _validate_brief(self) -> AppointmentBrief:
        """Enforce the cross-field invariants."""
        claim_ids = [claim.claim_id for claim in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("claim_id values must be unique within a brief")
        question_ids = [question.question_id for question in self.questions]
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("question_id values must be unique within a brief")

        available = {source.number for source in self.sources}
        for claim in self.claims:
            referenced = [*claim.source_numbers, *claim.original_source_numbers]
            missing = [number for number in referenced if number not in available]
            if missing:
                # A marker pointing at nothing is worse than no marker: it looks
                # like provenance and is not.
                raise ValueError(
                    f"claim '{claim.claim_id}' cites unknown source number(s) {missing}"
                )

        numbers = [source.number for source in self.sources]
        if len(numbers) != len(set(numbers)):
            raise ValueError("source numbers must be unique within a brief")

        indices = [turn.index for turn in self.transcript]
        if indices != sorted(set(indices)):
            # Out of order or duplicated means a turn was dropped, repeated or
            # reshuffled somewhere upstream. A transcript that misreports the
            # order of a conversation is worse than no transcript.
            raise ValueError("transcript turns must be unique and in conversation order")
        return self

    def includes(self, section: BriefSection) -> bool:
        """True when a section survives the user's omissions."""
        return section in self.included_sections

    @property
    def has_verified_content(self) -> bool:
        """True when the brief carries at least one verified claim.

        A brief with no verified claim is not necessarily invalid — a user may
        keep only their own notes and questions — but a renderer must not
        present it as an evidence summary.
        """
        return any(claim.is_verified for claim in self.claims)

    @property
    def edited_claim_count(self) -> int:
        """How many claims the user has changed."""
        return sum(1 for claim in self.claims if claim.origin is ContentOrigin.USER_EDITED)

    def visible_claims(self) -> list[BriefClaim]:
        """Claims that will print, honouring section omission."""
        return list(self.claims) if self.includes(BriefSection.CLAIMS) else []

    def visible_questions(self) -> list[BriefQuestion]:
        """Questions that will print, in their current order."""
        return list(self.questions) if self.includes(BriefSection.QUESTIONS) else []

    def visible_transcript(self) -> list[BriefTranscriptTurn]:
        """Transcript turns that will print. Empty unless the add-on is chosen."""
        return list(self.transcript) if self.includes(BriefSection.TRANSCRIPT) else []

    def cited_source_numbers(self) -> set[int]:
        """Numbers referenced by the claims that will print.

        Includes the markers on an edited claim's original wording: that line is
        printed, so the source it points at must stay in the list.
        """
        return {
            number
            for claim in self.visible_claims()
            for number in (*claim.source_numbers, *claim.original_source_numbers)
        }


__all__ = [
    "DEFAULT_SECTIONS",
    "DOCUMENT_ID_BYTES",
    "MAX_NOTE_CHARS",
    "MAX_QUESTIONS",
    "MAX_QUESTION_CHARS",
    "MAX_TOPIC_CHARS",
    "MAX_TRANSCRIPT_ANSWER_CHARS",
    "MAX_TRANSCRIPT_QUESTION_CHARS",
    "MAX_TRANSCRIPT_TURNS",
    "RESEARCH_SECTIONS",
    "TRANSCRIPT_SECTIONS",
    "AppointmentBrief",
    "BriefClaim",
    "BriefProvenance",
    "BriefQuestion",
    "BriefSection",
    "BriefSource",
    "BriefTranscriptTurn",
    "ContentOrigin",
    "SupportLevel",
    "TurnStatus",
    "UserContent",
    "new_document_id",
]

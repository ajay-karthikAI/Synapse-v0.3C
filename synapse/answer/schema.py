"""
synapse.answer.schema
=====================
The validated answer contract that replaces emoji-heading parsing.

What it replaces: the previous renderer recovered structure by running three
regexes over free-form model prose --

    re.search(r'📋 WHAT THE RESEARCH SAYS\\s*(.*?)(?=🔬|❓|⚠️|$)', ...)

That fails in every direction at once. A model that reorders, renames or omits
a heading silently drops a section; a truncated generation loses the trailing
disclaimer entirely; and nothing anywhere checks that the ``[Source 1]`` markers
in the prose correspond to anything that was actually retrieved.

Structure is now a *contract the model must satisfy*, not a pattern hoped for in
its output. Everything the interface renders comes from a typed field, so a
missing section is a validation error rather than an invisible omission.

Three fields carry most of the safety weight:

* ``support_status`` -- assigned by :mod:`synapse.answer.verify`, never by the
  model. A model asserting its own claim is supported is not evidence.
* ``supporting_excerpts`` -- verbatim spans that must be found in the cited
  chunk. This is what turns "is this citation real?" into a string comparison.
* ``action`` -- the top-level behaviour, so abstention is a first-class state
  rather than a sentence the model may or may not have written.
"""

from __future__ import annotations  # Postponed annotations

from enum import StrEnum
from typing import ClassVar

from pydantic import Field, model_validator

from synapse.identifiers import is_valid_chunk_id, is_valid_document_id
from synapse.schemas.base import SynapseModel, VersionedModel

MAX_CLAIMS = 24  # Bounds a runaway generation; a patient-facing pre-visit answer with more than this is malformed, not thorough
MAX_QUOTE_CHARS = 600  # A "supporting excerpt" longer than this is the whole chunk, which supports nothing in particular


class SupportStatus(StrEnum):
    """How well a claim is backed by the evidence actually retrieved.

    Assigned by the verifier, never by the model. A model's own assessment of
    whether it is grounded is exactly the thing under test.
    """

    SUPPORTED = "supported"  # Every cited source and chunk exists, and at least one excerpt was found verbatim
    PARTIALLY_SUPPORTED = "partially_supported"  # Some excerpts verified, others did not; or numbers in the claim are absent from the cited text
    UNSUPPORTED = "unsupported"  # Nothing verified. Never displayed to a patient.


class AnswerAction(StrEnum):
    """What the system does with a query.

    ``EMERGENCY`` is produced by the existing red-flag detector *before*
    retrieval, and is passed through unchanged by this redesign.
    """

    ANSWER = "answer"  # Grounded answer, safe to display
    ABSTAIN = "abstain"  # Evidence insufficient; no medical content shown
    MEDICAL_STAFF = "medical_staff"  # Route to a human member of clinical staff
    EMERGENCY = "emergency"  # Immediate escalation, ahead of any retrieval


class SupportingExcerpt(SynapseModel):
    """A verbatim span the claim rests on, and where it came from."""

    source_id: str = Field(min_length=1, description="Document identifier this quote came from.")
    chunk_id: str = Field(min_length=1, description="Chunk identifier this quote came from.")
    quote: str = Field(
        min_length=1,
        max_length=MAX_QUOTE_CHARS,
        description="Verbatim span copied from the chunk. Must be found in it after documented normalisation.",
    )

    @model_validator(mode="after")
    def _validate_identifiers(self) -> SupportingExcerpt:
        """Reject identifiers outside the grammar.

        A malformed identifier is the cheapest signal that the model invented a
        citation rather than copying one, so it is caught at the schema boundary
        before any verification runs.
        """
        if not is_valid_document_id(self.source_id):
            raise ValueError("source_id does not match the required '<scheme>:<key>' grammar")
        if not is_valid_chunk_id(self.chunk_id):
            raise ValueError(
                "chunk_id does not match the required '<scheme>:<key>#<ordinal>' grammar"
            )
        # A chunk always belongs to the document its identifier names; a
        # mismatch means the two were assembled independently, i.e. fabricated.
        if not self.chunk_id.startswith(f"{self.source_id}#"):
            raise ValueError("chunk_id must belong to source_id")
        return self


class GroundedClaim(SynapseModel):
    """One assertion, with the evidence it rests on and its verified status."""

    claim_id: str = Field(
        min_length=1,
        description="Identifier unique within the answer. Keys every verification result and UI annotation.",
    )
    text: str = Field(min_length=1, description="The claim as it would be shown to the patient.")
    source_ids: list[str] = Field(default_factory=list, description="Documents this claim cites.")
    supporting_excerpts: list[SupportingExcerpt] = Field(
        default_factory=list, description="Verbatim spans backing the claim."
    )
    support_status: SupportStatus = Field(
        default=SupportStatus.UNSUPPORTED,
        description="Set by the verifier. Defaults to UNSUPPORTED so a claim that never reaches verification is never displayed.",
    )

    @model_validator(mode="after")
    def _validate_claim(self) -> GroundedClaim:
        """Every excerpt must belong to a source the claim actually cites."""
        for source_id in self.source_ids:
            if not is_valid_document_id(source_id):
                raise ValueError("source_ids entries must be well-formed document identifiers")
        cited = set(self.source_ids)
        for excerpt in self.supporting_excerpts:
            if excerpt.source_id not in cited:
                # An excerpt attributed to a document the claim does not cite is
                # incoherent, and is a common shape of fabricated support.
                raise ValueError(
                    "every supporting excerpt must belong to a source listed in source_ids"
                )
        return self

    @property
    def is_displayable(self) -> bool:
        """True when this claim may be shown to a patient.

        Requirement 8, expressed as a property rather than a rule callers have
        to remember: an unsupported medical claim is never displayed.
        """
        return self.support_status is not SupportStatus.UNSUPPORTED


class GroundedAnswer(VersionedModel):
    """The complete validated answer.

    Field names and shape follow the required response contract exactly, so the
    same schema can be handed to a provider's structured-output mode and used
    as the internal type without a translation layer to drift.
    """

    SCHEMA_NAME: ClassVar[str] = "grounded_answer"
    SCHEMA_VERSION: ClassVar[str] = "1.0"

    summary: str = Field(
        default="",
        description="Plain-language overview. Displayed only when the answer is not abstaining.",
    )
    claims: list[GroundedClaim] = Field(
        default_factory=list,
        max_length=MAX_CLAIMS,
        description="Every medical assertion, each individually verifiable.",
    )
    doctor_evaluation: str = Field(
        default="",
        description="What the clinician will assess. Framing, not a claim about the patient.",
    )
    questions_for_doctor: list[str] = Field(
        default_factory=list,
        description="Questions for the appointment. Uncited by design -- they assert nothing.",
    )
    limitations: list[str] = Field(
        default_factory=list, description="What this answer does not establish."
    )
    disclaimer: str = Field(
        default="",
        description="Model-supplied disclaimer. NOT what the interface displays -- see synapse.answer.render, which always renders its own.",
    )
    action: AnswerAction = Field(
        default=AnswerAction.ABSTAIN,
        description="Top-level behaviour. Defaults to ABSTAIN so a malformed answer fails closed.",
    )

    @model_validator(mode="after")
    def _validate_answer(self) -> GroundedAnswer:
        """Enforce internal coherence."""
        claim_ids = [claim.claim_id for claim in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            # Verification results key on claim_id; duplicates would make the
            # mapping ambiguous.
            raise ValueError("claim_id values must be unique within an answer")
        if self.action is AnswerAction.ANSWER and not self.claims:
            # An "answer" with no claims has nothing to be grounded in.
            raise ValueError("action 'answer' requires at least one claim")
        return self

    @property
    def displayable_claims(self) -> list[GroundedClaim]:
        """Claims that may be shown to a patient."""
        return [claim for claim in self.claims if claim.is_displayable]

    @property
    def unsupported_claims(self) -> list[GroundedClaim]:
        """Claims withheld from display."""
        return [claim for claim in self.claims if not claim.is_displayable]


# JSON Schema handed to a provider's structured-output mode. Generated from the
# model rather than hand-written, so the contract the model is given and the
# contract the code enforces cannot drift apart.
def provider_json_schema() -> dict:
    """JSON Schema for the required response shape.

    ``support_status`` is excluded: it is assigned by the verifier, and asking
    the model for it would invite it to mark its own work.
    """
    schema = GroundedAnswer.model_json_schema()
    definitions = schema.get("$defs", {})
    if "GroundedClaim" in definitions:
        definitions["GroundedClaim"]["properties"].pop("support_status", None)
        definitions["GroundedClaim"]["required"] = [
            name
            for name in definitions["GroundedClaim"].get("required", [])
            if name != "support_status"
        ]
    schema.get("properties", {}).pop(
        "schema_version", None
    )  # Internal field; not the model's to set
    return schema


__all__ = [
    "MAX_CLAIMS",
    "MAX_QUOTE_CHARS",
    "AnswerAction",
    "GroundedAnswer",
    "GroundedClaim",
    "SupportStatus",
    "SupportingExcerpt",
    "provider_json_schema",
]

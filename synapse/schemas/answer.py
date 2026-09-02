"""
synapse.schemas.answer
======================
Retrieval and answer models: :class:`RetrievedEvidence`, :class:`AnswerClaim`
and :class:`StructuredAnswer`.

These are *schemas only* in this change — no generation, retrieval or
validation logic is implemented here. They exist now because the corpus and
manifest layer has to be built against the shape it will eventually serve.

The design point that matters is :class:`ClaimExcerpt`. Today the generator
asks the model for ``[Source 1]`` markers and nothing parses them: a patient
sees three PubMed links attached to an answer that may have used none of them
(docs/quality-architecture.md §1.3, H2). By requiring every claim to carry the
verbatim ``quote`` it relied on, plus the chunk that quote came from,
"is this citation correct?" becomes a string containment check against the
corpus rather than a model's opinion — deterministic, offline, and free.
"""

from __future__ import annotations  # Postponed annotations

from datetime import datetime
from typing import ClassVar

from pydantic import Field, field_validator, model_validator

from synapse.identifiers import is_valid_chunk_id, is_valid_document_id
from synapse.schemas.base import SynapseModel, VersionedModel, require_timezone_aware
from synapse.schemas.enums import AnswerDecision, ClaimType


class RetrievedEvidence(SynapseModel):
    """One retrieval hit: a chunk, its scores, and its rank.

    Mirrors the ``{chunk, score, rank}`` shape the existing BM25 and vector
    retrievers already agree on, so this model can wrap current behaviour
    without changing it — but keyed by ``chunk_id`` rather than by an object
    reference, so a result set can be serialised, logged and replayed.
    """

    chunk_id: str = Field(description="Identifier of the retrieved chunk.")
    document_id: str = Field(description="Identifier of the chunk's parent document.")
    rank: int = Field(ge=1, description="One-based rank in the result list.")
    score: float = Field(description="Fused retrieval score.")
    vector_score: float | None = Field(
        default=None,
        description="Normalised dense-retrieval component, when the fusion strategy exposes one.",
    )
    bm25_score: float | None = Field(
        default=None,
        description="Normalised sparse-retrieval component, when the fusion strategy exposes one.",
    )
    rerank_score: float | None = Field(
        default=None, description="Reranker score, when reranking ran."
    )
    retrieval_method: str = Field(
        min_length=1, description="Which strategy produced this hit, e.g. 'linear_fusion' or 'rrf'."
    )

    @field_validator("chunk_id")
    @classmethod
    def _validate_chunk_id(cls, value: str) -> str:
        """Reject malformed chunk identifiers."""
        if not is_valid_chunk_id(value):
            raise ValueError("chunk_id does not match the required grammar")
        return value

    @field_validator("document_id")
    @classmethod
    def _validate_document_id(cls, value: str) -> str:
        """Reject malformed document identifiers."""
        if not is_valid_document_id(value):
            raise ValueError("document_id does not match the required grammar")
        return value


class ClaimExcerpt(SynapseModel):
    """The verbatim span a claim relies on, and where it came from.

    ``quote`` must appear in the cited chunk's text. That check is what turns
    citation correctness into a deterministic comparison.
    """

    chunk_id: str = Field(description="Chunk the quote was taken from.")
    document_id: str = Field(description="Parent document of that chunk.")
    quote: str = Field(
        min_length=1, description="Verbatim span copied from the chunk, used to verify support."
    )
    char_start: int | None = Field(
        default=None,
        ge=0,
        description="Offset of the quote within the chunk, when the generator reported it.",
    )
    char_end: int | None = Field(
        default=None, gt=0, description="End offset (exclusive) of the quote within the chunk."
    )

    @field_validator("chunk_id")
    @classmethod
    def _validate_chunk_id(cls, value: str) -> str:
        """Reject malformed chunk identifiers."""
        if not is_valid_chunk_id(value):
            raise ValueError("chunk_id does not match the required grammar")
        return value

    @model_validator(mode="after")
    def _validate_offsets(self) -> ClaimExcerpt:
        """Offsets, when supplied, must describe a non-empty forward span."""
        if (
            self.char_start is not None
            and self.char_end is not None
            and self.char_end <= self.char_start
        ):
            raise ValueError("char_end must be greater than char_start")
        return self


class AnswerClaim(SynapseModel):
    """A single assertion made by an answer, with the evidence it rests on."""

    claim_id: str = Field(
        min_length=1,
        description="Identifier unique within its answer; every validation result and annotation keys on this.",
    )
    text: str = Field(min_length=1, description="The claim as shown to the patient.")
    claim_type: ClaimType = Field(description="Only 'factual' claims require citations.")
    section: str = Field(min_length=1, description="Which answer section the claim belongs to.")
    source_ids: list[str] = Field(
        default_factory=list, description="Documents cited by this claim."
    )
    excerpts: list[ClaimExcerpt] = Field(
        default_factory=list, description="Verbatim supporting spans, one or more per cited source."
    )

    @field_validator("source_ids")
    @classmethod
    def _validate_source_ids(cls, value: list[str]) -> list[str]:
        """Every cited source must be a well-formed document identifier."""
        for source_id in value:
            if not is_valid_document_id(source_id):
                raise ValueError("source_ids entries must be well-formed document identifiers")
        return value

    @model_validator(mode="after")
    def _validate_citation_requirements(self) -> AnswerClaim:
        """A factual claim must cite something, and its excerpts must match its citations."""
        if (
            self.claim_type is ClaimType.FACTUAL and not self.source_ids
        ):  # This is the structural half of citation completeness
            raise ValueError("a factual claim must cite at least one source")
        cited = set(self.source_ids)  # Cited documents, for cross-checking excerpts
        for excerpt in self.excerpts:  # An excerpt attributed to an uncited document is incoherent
            if excerpt.document_id not in cited:
                raise ValueError("every excerpt must belong to a document listed in source_ids")
        return self


class StructuredAnswer(VersionedModel):
    """A complete answer as structured data rather than free text."""

    SCHEMA_NAME: ClassVar[str] = "structured_answer"
    SCHEMA_VERSION: ClassVar[str] = "1.0"

    answer_id: str = Field(min_length=1, description="Identifier for this answer.")
    query_sha256: str = Field(
        min_length=1,
        description="Salted digest of the patient query. Raw query text is deliberately NOT part of this record.",
    )
    created_at: datetime = Field(description="When the answer was produced (timezone-aware).")

    claims: list[AnswerClaim] = Field(
        default_factory=list, description="Every assertion the answer makes."
    )
    questions: list[str] = Field(
        default_factory=list,
        description="Questions for the patient to ask their clinician. Uncited by design.",
    )
    boundary_statement: str = Field(
        default="", description="The explicit non-diagnosis disclaimer."
    )

    decision: AnswerDecision = Field(description="Outcome of the citation-validation ladder.")
    abstained: bool = Field(default=False, description="True when no medical content was produced.")
    abstain_reason: str | None = Field(
        default=None, description="Why the system abstained, when it did."
    )
    is_emergency: bool = Field(
        default=False, description="True when emergency routing fired ahead of retrieval."
    )

    retrieved: list[RetrievedEvidence] = Field(
        default_factory=list, description="The evidence considered, for auditability."
    )
    corpus_version: str | None = Field(default=None, description="Corpus the evidence came from.")
    index_id: str | None = Field(default=None, description="Index build the evidence came from.")
    generation_model: str | None = Field(
        default=None, description="Model that produced the answer."
    )
    prompt_id: str | None = Field(default=None, description="Versioned prompt template identifier.")

    @model_validator(mode="after")
    def _validate_answer_consistency(self) -> StructuredAnswer:
        """Enforce that the declared decision matches the content actually present."""
        claim_ids = [
            claim.claim_id for claim in self.claims
        ]  # Claim identifiers must be unique within an answer, since validation results key on them
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("claim_id values must be unique within an answer")
        if self.abstained and any(
            claim.claim_type is ClaimType.FACTUAL for claim in self.claims
        ):  # An abstention that still asserts medical facts is not an abstention
            raise ValueError("an abstained answer must not contain factual claims")
        if (
            self.decision is AnswerDecision.ABSTAIN and not self.abstained
        ):  # Decision and flag must agree, or downstream consumers disagree about what happened
            raise ValueError("decision 'abstain' requires abstained to be true")
        if (
            self.abstained and self.abstain_reason is None
        ):  # An unexplained abstention cannot be triaged later
            raise ValueError("an abstained answer must record abstain_reason")
        require_timezone_aware(self.created_at, "created_at")
        return self


__all__ = ["AnswerClaim", "ClaimExcerpt", "RetrievedEvidence", "StructuredAnswer"]

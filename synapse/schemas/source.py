"""
synapse.schemas.source
======================
Source-pack models: :class:`SourceDocument`, :class:`SourceReview` and
:class:`SourceReview`.

The governed source-pack manifest lives in :mod:`synapse.schemas.source_pack`.

The governance rule these models encode, stated once and enforced mechanically:

    **A document may not claim approval without an approval record.**

``approval_status = "approved"`` is only constructible alongside a
:class:`SourceReview` carrying a clinician reviewer, an ``approved`` decision
and a re-review due date. Conversely ``"unreviewed"`` forbids a review record
entirely, so a half-populated record cannot imply review by accident. Every
document migrated from the existing corpus lands as ``unreviewed`` with
``review=None``, because the repository contains no review metadata of any kind
and no other value would be truthful.

Reviewer identity is pseudonymous by construction: ``reviewer_id`` must match
``rev_[a-z0-9]{8}``, and the validator additionally rejects strings containing
'@' or whitespace. The mapping from pseudonym to person is held outside this
repository, so a clone of the corpus never carries reviewer identities.
"""

from __future__ import annotations  # Postponed annotations

from datetime import date, datetime  # Publication dates are calendar dates; timestamps are instants
from typing import ClassVar

from pydantic import Field, field_validator, model_validator

from synapse.hashing import is_sha256_hex
from synapse.identifiers import is_valid_document_id, is_valid_reviewer_id
from synapse.schemas.base import SynapseModel, VersionedModel, require_timezone_aware
from synapse.schemas.enums import (
    ApprovalStatus,
    DatePrecision,
    EvidenceType,
    EvidenceTypeProvenance,
    NoticeType,
    RetractionStatus,
    ReviewDecision,
    ReviewerRole,
    SourceType,
)

_ALLOWED_URL_SCHEMES = (
    "http://",
    "https://",
)  # Only network schemes are permitted; file:// or javascript: in a citation field would be a rendering and exfiltration hazard


class Author(SynapseModel):  # Nested value object, not versioned independently
    """A single author of a source document. Never fabricated: absent means an empty list.

    PubMed author lists mix two shapes: personal authors carrying
    ``<LastName>``/``<ForeName>``, and *collective* authors carrying only a
    ``<CollectiveName>`` such as "The SPRINT Research Group". Modelling both
    with one required family name would either drop study groups entirely or
    force a study-group name into a surname field.
    """

    family: str | None = Field(
        default=None, description="Family name as published. Null for a collective author."
    )
    given: str | None = Field(
        default=None, description="Given name(s) as published, when available."
    )
    initials: str | None = Field(default=None, description="Initials as published, when available.")
    collective_name: str | None = Field(
        default=None, description="Name of a study group or consortium credited as an author."
    )
    is_collective: bool = Field(
        default=False, description="True when this entry is a group rather than a person."
    )

    @model_validator(mode="after")
    def _validate_author_shape(self) -> Author:
        """An author is either a person or a collective, never both and never neither."""
        if self.is_collective:  # Collective authors carry a group name and no personal-name parts
            if not self.collective_name:
                raise ValueError("a collective author requires collective_name")
            if self.family or self.given or self.initials:
                raise ValueError("a collective author must not carry personal name parts")
        else:  # Personal authors require at least a family name to be citable
            if not self.family:
                raise ValueError("a personal author requires a family name")
            if self.collective_name:
                raise ValueError("a personal author must not carry collective_name")
        return self


class AbstractSection(SynapseModel):  # One <AbstractText> element from a structured abstract
    """A single labelled section of an abstract.

    The legacy pipeline called ``find(".//AbstractText")``, which returns only
    the *first* element. For a structured abstract that keeps BACKGROUND and
    discards CONCLUSIONS — the clinically actionable part. Measured on the
    shipped corpus: 312 of 847 documents retained under 400 characters of
    abstract text in total.
    """

    ordinal: int = Field(
        ge=0, description="Position of this section within the abstract, preserving source order."
    )
    label: str | None = Field(
        default=None,
        description="Publisher's section label, e.g. 'CONCLUSIONS'. Null for an unstructured abstract.",
    )
    nlm_category: str | None = Field(
        default=None,
        description="NLM's normalised category attribute, when the publisher supplied one.",
    )
    text: str = Field(
        min_length=1, description="Normalised section text with inline markup flattened."
    )


class RelatedNotice(SynapseModel):  # One <CommentsCorrections> relationship
    """A retraction, correction, or comment relationship declared by PubMed."""

    notice_type: NoticeType = Field(description="Relationship type, with direction preserved.")
    ref_source: str | None = Field(
        default=None, description="Citation string of the related article, as published."
    )
    pmid: str | None = Field(default=None, description="PMID of the related article, when given.")
    doi: str | None = Field(default=None, description="DOI of the related article, when given.")
    raw_ref_type: str = Field(
        min_length=1,
        description="Verbatim RefType attribute, retained so an unmapped value is never lost.",
    )


class DocumentIdentifiers(SynapseModel):  # External identifier bundle
    """External identifiers for a source document. All optional; all nullable."""

    pmid: str | None = Field(default=None, description="PubMed identifier.")
    pmcid: str | None = Field(default=None, description="PubMed Central identifier.")
    doi: str | None = Field(default=None, description="Digital Object Identifier.")
    guideline_id: str | None = Field(
        default=None, description="Issuing organisation's guideline identifier."
    )

    @field_validator("pmid")
    @classmethod
    def _validate_pmid(cls, value: str | None) -> str | None:
        """A PMID, when present, must be numeric — a non-numeric one means the upstream parse failed."""
        if value is not None and not value.isdigit():
            raise ValueError("pmid must be numeric")
        return value


class SourceContainer(SynapseModel):  # Journal or issuing-organisation metadata
    """Publication container: a journal, or the organisation issuing a guideline."""

    journal: str | None = Field(default=None, description="Journal title.")
    issuing_organization: str | None = Field(
        default=None, description="Organisation issuing a guideline."
    )
    issn: str | None = Field(default=None, description="ISSN, when available.")
    volume: str | None = Field(default=None)
    issue: str | None = Field(default=None)
    pages: str | None = Field(default=None)


class SourceReview(VersionedModel):
    """A review record. Its existence is the *only* thing that can license an approval claim."""

    SCHEMA_NAME: ClassVar[str] = "source_review"
    SCHEMA_VERSION: ClassVar[str] = "1.0"

    reviewer_id: str = Field(
        description="Pseudonymous reviewer identifier, 'rev_' + 8 lowercase alphanumerics."
    )
    reviewer_role: ReviewerRole = Field(
        description="Role of the reviewer; only 'clinician' may approve clinical content."
    )
    reviewed_at: datetime = Field(description="When the review was recorded (timezone-aware).")
    decision: ReviewDecision = Field(description="Outcome of the review.")
    review_due_date: date = Field(description="Date by which this decision must be revisited.")
    rubric_version: str = Field(min_length=1, description="Version of the review SOP applied.")
    comment: str = Field(
        default="", description="Reviewer notes. Must not contain identifying information."
    )
    signature: str | None = Field(
        default=None,
        description="Optional detached signature; the signing key is held outside this repository.",
    )

    @field_validator("reviewer_id")
    @classmethod
    def _validate_reviewer_pseudonymity(cls, value: str) -> str:
        """Enforce pseudonymity at the schema boundary, so an identity cannot be committed by accident."""
        if not is_valid_reviewer_id(
            value
        ):  # Pattern rejects names, emails, and anything with whitespace or punctuation
            raise ValueError(
                "reviewer_id must match 'rev_' followed by 8 lowercase alphanumeric characters"
            )
        return value

    @field_validator("comment")
    @classmethod
    def _validate_comment_has_no_contact_details(cls, value: str) -> str:
        """Reject the most common accidental identity leak: an email address in free text."""
        if (
            "@" in value
        ):  # Deliberately narrow: a targeted check that catches the realistic mistake without trying to be a general PII detector
            raise ValueError(
                "comment must not contain an '@'; reviewer identities are held outside this repository"
            )
        return value

    @model_validator(mode="after")
    def _validate_timestamps(self) -> SourceReview:
        """Reject naive timestamps, which are not verifiable across machines."""
        require_timezone_aware(self.reviewed_at, "reviewed_at")
        return self


class SourceDocument(VersionedModel):
    """A source document and its complete evidence-governance state."""

    SCHEMA_NAME: ClassVar[str] = "source_document"
    SCHEMA_VERSION: ClassVar[str] = (
        "1.1"  # 1.1 adds language, publication status, date variants, abstract sections and related notices; all optional, so 1.0 records still load
    )

    document_id: str = Field(description="Stable identifier, '<scheme>:<key>'.")
    source_type: SourceType = Field(description="Origin format.")
    source_url: str = Field(
        min_length=1, description="Exact resolved URL of the source, not a template."
    )
    identifiers: DocumentIdentifiers = Field(
        default_factory=DocumentIdentifiers, description="External identifiers."
    )

    title: str = Field(
        min_length=1, description="Full title, including any text inside nested markup."
    )
    authors: list[Author] = Field(
        default_factory=list,
        description="Authors as published; empty when unavailable. Never inferred.",
    )
    container: SourceContainer = Field(
        default_factory=SourceContainer, description="Journal or issuing organisation."
    )

    publication_date: date | None = Field(
        default=None,
        description="Canonical publication date: the earliest of the electronic and print dates, so recency is never overstated.",
    )
    publication_date_precision: DatePrecision = Field(
        default=DatePrecision.UNKNOWN,
        description="How precisely publication_date is known, so a year-only date is never compared as though exact.",
    )
    electronic_publication_date: date | None = Field(
        default=None,
        description="Electronic (ahead-of-print) date from <ArticleDate DateType='Electronic'>.",
    )
    print_publication_date: date | None = Field(
        default=None, description="Journal issue date from <JournalIssue><PubDate>."
    )
    medline_date_raw: str | None = Field(
        default=None,
        description="Verbatim <MedlineDate> free text, e.g. '2024 Jan-Feb', retained when no structured date was parseable.",
    )
    revision_date: date | None = Field(
        default=None, description="Latest revision date, for guidelines."
    )
    retrieved_at: datetime = Field(
        description="When Synapse fetched this document (timezone-aware)."
    )

    language: str | None = Field(
        default=None, description="Primary language code as published, e.g. 'eng'."
    )
    publication_status: str | None = Field(
        default=None,
        description="PubMed <PublicationStatus>, e.g. 'ppublish', 'epublish', 'aheadofprint'.",
    )
    abstract_sections: list[AbstractSection] = Field(
        default_factory=list,
        description="Every abstract section, in source order. Empty when the record has no abstract.",
    )
    related_notices: list[RelatedNotice] = Field(
        default_factory=list,
        description="Retraction, correction and comment relationships declared by PubMed.",
    )
    is_retraction_notice: bool = Field(
        default=False,
        description="True when this record IS a retraction or expression-of-concern notice, rather than the article being retracted.",
    )

    evidence_type: EvidenceType = Field(
        default=EvidenceType.UNCLASSIFIED, description="Study design / evidence class."
    )
    evidence_type_provenance: EvidenceTypeProvenance = Field(
        default=EvidenceTypeProvenance.UNKNOWN,
        description="How the evidence type was determined. Mandatory so a guess is never mistaken for a judgement.",
    )
    publication_types: list[str] = Field(
        default_factory=list, description="Raw upstream publication-type strings."
    )

    retraction_status: RetractionStatus = Field(
        default=RetractionStatus.UNCHECKED,
        description="Retraction screening state. 'unchecked' is distinct from 'none'.",
    )
    retraction_checked_at: datetime | None = Field(
        default=None, description="When retraction screening last ran."
    )

    approval_status: ApprovalStatus = Field(
        default=ApprovalStatus.UNREVIEWED,
        description="Governance state. Defaults to unreviewed and cannot be upgraded without a review record.",
    )
    review: SourceReview | None = Field(
        default=None, description="Review record. Required for approval; forbidden when unreviewed."
    )
    review_due_date: date | None = Field(
        default=None, description="Date the approval expires and must be revisited."
    )

    content_sha256: str = Field(description="SHA-256 of the document's normalised full text.")
    chunk_ids: list[str] = Field(
        default_factory=list, description="Chunks derived from this document, in emission order."
    )
    source_pack_version: str = Field(
        min_length=1, description="Version of the source pack this record belongs to."
    )
    notes: str = Field(
        default="", description="Operational notes. Must not contain clinical assertions."
    )

    @field_validator("document_id")
    @classmethod
    def _validate_document_id(cls, value: str) -> str:
        """Reject identifiers outside the grammar."""
        if not is_valid_document_id(value):
            raise ValueError("document_id does not match the required '<scheme>:<key>' grammar")
        return value

    @field_validator("source_url")
    @classmethod
    def _validate_url_scheme(cls, value: str) -> str:
        """Restrict URLs to http/https.

        The URL is rendered as a clickable citation in the patient-facing UI, so
        permitting other schemes would turn a corpus file into a link-injection
        vector.
        """
        if not value.startswith(_ALLOWED_URL_SCHEMES):
            raise ValueError("source_url must be an http:// or https:// URL")
        return value

    @field_validator("content_sha256")
    @classmethod
    def _validate_digest_shape(cls, value: str) -> str:
        """Reject digests that are not lowercase 64-character hex."""
        if not is_sha256_hex(value):
            raise ValueError("content_sha256 must be 64 lowercase hexadecimal characters")
        return value

    @model_validator(mode="after")
    def _validate_governance_invariants(self) -> SourceDocument:
        """Enforce the approval rules. This is the mechanical form of 'no approval claims without approval records'."""
        if (
            self.approval_status is ApprovalStatus.APPROVED
        ):  # The only status that asserts clinical sign-off
            if (
                self.review is None
            ):  # Approval without a record is exactly the claim this system must never make
                raise ValueError("approval_status 'approved' requires a review record")
            if (
                self.review.decision is not ReviewDecision.APPROVED
            ):  # A record that says "needs changes" cannot license approval
                raise ValueError(
                    "approval_status 'approved' requires review.decision == 'approved'"
                )
            if (
                self.review.reviewer_role is not ReviewerRole.CLINICIAN
            ):  # Non-clinical review does not license clinical approval
                raise ValueError("approval_status 'approved' requires a clinician reviewer")
            if (
                self.review_due_date is None
            ):  # An approval with no expiry silently becomes permanent
                raise ValueError("approval_status 'approved' requires review_due_date")
        if (
            self.approval_status is ApprovalStatus.UNREVIEWED and self.review is not None
        ):  # Prevents a half-populated record from implying review
            raise ValueError("approval_status 'unreviewed' must not carry a review record")
        if (
            self.retraction_status is not RetractionStatus.UNCHECKED
            and self.retraction_checked_at is None
        ):  # Asserting a screening result requires saying when it was screened
            raise ValueError(
                "a retraction_status other than 'unchecked' requires retraction_checked_at"
            )
        require_timezone_aware(self.retrieved_at, "retrieved_at")
        if self.retraction_checked_at is not None:
            require_timezone_aware(self.retraction_checked_at, "retraction_checked_at")
        return self

    @property
    def is_citable(self) -> bool:  # Convenience predicate used by later retrieval-eligibility logic
        """True when this document is not retracted or withdrawn.

        Note this deliberately does NOT require approval: enforcing approval
        today would empty the corpus, since nothing has been reviewed. The
        eligibility policy is an explicit, recorded decision made by the caller,
        not an implicit default buried here.
        """
        return (
            self.retraction_status
            not in (RetractionStatus.RETRACTED, RetractionStatus.EXPRESSION_OF_CONCERN)
            and self.approval_status is not ApprovalStatus.WITHDRAWN
        )


__all__ = [
    "AbstractSection",
    "Author",
    "DocumentIdentifiers",
    "RelatedNotice",
    "SourceContainer",
    "SourceDocument",
    "SourceReview",
]

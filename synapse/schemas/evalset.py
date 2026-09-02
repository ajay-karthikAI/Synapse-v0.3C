"""
synapse.schemas.evalset
=======================
Evaluation-dataset models.

The rule this module enforces, and the reason it exists as a schema rather than
a convention:

    **A case cannot claim review it did not receive, and an unreviewed case
    cannot gate a release.**

Every field that would let a case influence a release decision — annotation
status, reviewer identities, disagreement resolution, redaction state — is
constrained so that the permissive value requires evidence. A synthetic case
is *terminally* synthetic; there is no field a caller can set to promote it.

Reviewer identifiers are pseudonyms matching ``rev_[a-z0-9]{8}``, issued
out-of-band. Nothing here generates one, and nothing here records a signature
or an approval.

Two vocabularies are new rather than reused. :class:`EvalCategory` isolates
failure modes rather than medical topics, and :class:`EvalExpectedBehavior`
separates routing from escalation. The older three-value ``ExpectedBehavior``
could not express "reject an unsafe instruction" at all.
"""

from __future__ import annotations  # Postponed annotations

import re  # Query normalisation and version validation
import unicodedata  # NFKC folding, so visually identical queries normalise together
from datetime import datetime
from typing import ClassVar

from pydantic import Field, field_validator, model_validator

from synapse.hashing import is_sha256_hex
from synapse.identifiers import is_valid_chunk_id, is_valid_document_id, is_valid_reviewer_id
from synapse.schemas.base import SynapseModel, VersionedModel, require_timezone_aware
from synapse.schemas.enums import (
    AnnotationStatus,
    DatasetSplit,
    DisagreementStatus,
    EvalCategory,
    EvalExpectedBehavior,
    PrivacyClass,
    RedactionStatus,
    ReviewerRole,
)

CASE_ID_PATTERN = re.compile(
    r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$"
)  # Lowercase kebab-case, e.g. 'syn-emergency-0001'; stable and safe as a filename fragment

DATASET_VERSION_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"
)  # Strict MAJOR.MINOR.PATCH so versions order unambiguously

MAX_RELEVANCE_GRADE = 3  # Graded relevance runs 0-3. Graded rather than binary because nDCG needs gradations to distinguish "perfect" from "adequate".

_PUNCTUATION = re.compile(r"[^\w\s]")  # Everything that is not a word character or whitespace
_WHITESPACE = re.compile(r"\s+")

# Privacy classes whose text must be redacted before a case may gate a release.
# UNKNOWN is included deliberately: unestablished provenance is not a licence to
# assume safety.
REDACTION_REQUIRED_CLASSES = frozenset({PrivacyClass.PATIENT_DERIVED, PrivacyClass.UNKNOWN})

# Annotation statuses that may participate in release gating. SYNTHETIC and
# PENDING_REVIEW are absent by construction, which is the whole point.
GATING_ELIGIBLE_STATUSES = frozenset({AnnotationStatus.REVIEWED, AnnotationStatus.ADJUDICATED})


def normalize_query(query: str) -> str:
    """Canonical form of a query, for duplicate and near-duplicate detection.

    NFKC folding first, so full-width and composed characters compare equal to
    their plain forms — otherwise two visually identical queries pasted from
    different sources would count as distinct cases.

    Deliberately conservative: no stemming and no stopword removal. An
    aggressive normaliser merges genuinely different questions, and in this
    dataset "should I stop metformin" and "should I start metformin" must never
    collapse into one.
    """
    folded = unicodedata.normalize("NFKC", query).strip().lower()
    without_punctuation = _PUNCTUATION.sub(
        " ", folded
    )  # Punctuation to spaces, not deleted, so "well-controlled" does not become "wellcontrolled"
    return _WHITESPACE.sub(" ", without_punctuation).strip()


# ---------------------------------------------------------------------------
# Relevance and citation expectations
# ---------------------------------------------------------------------------


class GradedRelevance(SynapseModel):
    """A graded relevance judgement for one document or chunk."""

    target_id: str = Field(description="Document or chunk identifier this grade applies to.")
    grade: int = Field(
        ge=0,
        le=MAX_RELEVANCE_GRADE,
        description="0 irrelevant · 1 marginal · 2 useful · 3 fully answers the query.",
    )
    rationale: str = Field(
        default="",
        description="Why this grade. Optional, but the only way a later reviewer can audit the judgement.",
    )

    @field_validator("target_id")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        """Accept a document or a chunk identifier, and nothing else."""
        if not (is_valid_document_id(value) or is_valid_chunk_id(value)):
            raise ValueError("target_id must be a well-formed document or chunk identifier")
        return value

    @property
    def is_relevant(
        self,
    ) -> (
        bool
    ):  # Grade 0 records "considered and judged irrelevant", which is not the same as absent
        """True when the grade is above zero."""
        return self.grade > 0


class CitationRequirement(SynapseModel):
    """What an answer must cite for this case to count as correct."""

    min_citations: int = Field(
        default=1, ge=0, description="Minimum distinct sources a passing answer must cite."
    )
    must_cite_documents: list[str] = Field(
        default_factory=list, description="Documents an answer is required to cite."
    )
    must_not_cite_documents: list[str] = Field(
        default_factory=list,
        description="Documents an answer must NOT cite, e.g. retracted or out-of-scope sources.",
    )
    require_verbatim_excerpt: bool = Field(
        default=True,
        description="Whether each citation must carry a verbatim supporting excerpt, making citation correctness a string comparison rather than a judgement.",
    )

    @field_validator("must_cite_documents", "must_not_cite_documents")
    @classmethod
    def _validate_document_ids(cls, value: list[str]) -> list[str]:
        """Every referenced document must be a well-formed identifier."""
        for document_id in value:
            if not is_valid_document_id(document_id):
                raise ValueError(
                    "citation requirements must reference well-formed document identifiers"
                )
        return value

    @model_validator(mode="after")
    def _validate_no_contradiction(self) -> CitationRequirement:
        """A document cannot be both required and forbidden."""
        overlap = set(self.must_cite_documents) & set(self.must_not_cite_documents)
        if (
            overlap
        ):  # Silently preferring one list would make the case unpassable in a way nobody could see
            raise ValueError("a document cannot be both required and forbidden as a citation")
        return self


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


class CaseReview(SynapseModel):
    """One reviewer's independent judgement of one case.

    Reviews are stored as a *list* on the case rather than folded into it,
    because inter-annotator agreement is only computable while the individual
    judgements are still separable. Collapsing them into a single "the label"
    destroys the evidence that reviewers ever disagreed.
    """

    reviewer_id: str = Field(
        description="Pseudonymous reviewer identifier, 'rev_' + 8 lowercase alphanumerics. Supplied by an operator; never generated here."
    )
    reviewer_role: ReviewerRole = Field(
        description="Reviewer's role, recorded so a non-clinical review is never mistaken for a clinical one."
    )
    reviewed_at: datetime = Field(description="When the review was recorded (timezone-aware).")
    protocol_version: str = Field(
        min_length=1,
        description="Version of the labelling protocol applied, so a judgement can be read against the standard in force at the time.",
    )

    # -- the reviewer's independent judgement -------------------------------
    category: EvalCategory = Field(description="Category this reviewer assigns.")
    expected_behavior: EvalExpectedBehavior = Field(
        description="Behaviour this reviewer expects the system to produce."
    )
    relevance_grades: list[GradedRelevance] = Field(
        default_factory=list, description="This reviewer's graded relevance judgements."
    )

    confidence: int = Field(
        default=3,
        ge=1,
        le=5,
        description="Reviewer's own confidence, 1 low to 5 high. A low-confidence agreement is weaker evidence than a high-confidence one.",
    )
    notes: str = Field(
        default="", description="Reviewer's notes. Must not contain identifying information."
    )

    @field_validator("reviewer_id")
    @classmethod
    def _validate_reviewer_pseudonymity(cls, value: str) -> str:
        """Enforce pseudonymity at the schema boundary."""
        if not is_valid_reviewer_id(value):
            raise ValueError(
                "reviewer_id must match 'rev_' followed by 8 lowercase alphanumeric characters"
            )
        return value

    @field_validator("notes")
    @classmethod
    def _validate_notes_have_no_contact_details(cls, value: str) -> str:
        """Reject the most common accidental identity leak: an email address."""
        if (
            "@" in value
        ):  # Narrow and targeted: catches the realistic mistake without pretending to be a general PII detector
            raise ValueError(
                "notes must not contain an '@'; reviewer identities are held outside this repository"
            )
        return value

    @model_validator(mode="after")
    def _validate_review(self) -> CaseReview:
        """Reject naive timestamps, which are not verifiable across machines."""
        require_timezone_aware(self.reviewed_at, "reviewed_at")
        return self


class Adjudication(SynapseModel):
    """Resolution of a disagreement between reviewers."""

    adjudicator_id: str = Field(description="Pseudonymous identifier of the adjudicator.")
    adjudicated_at: datetime = Field(
        description="When the adjudication was recorded (timezone-aware)."
    )
    resolved_category: EvalCategory = Field(
        description="The category that stands after adjudication."
    )
    resolved_expected_behavior: EvalExpectedBehavior = Field(
        description="The expected behaviour that stands after adjudication."
    )
    rationale: str = Field(
        min_length=1,
        description="Why this resolution. An adjudication without reasoning is an assertion, and gives a later auditor nothing.",
    )
    disagreeing_reviewers: list[str] = Field(
        default_factory=list,
        description="Reviewers whose judgements were overridden, recorded so the disagreement is not erased.",
    )

    @field_validator("adjudicator_id")
    @classmethod
    def _validate_adjudicator(cls, value: str) -> str:
        """The adjudicator must also be a pseudonymous identifier."""
        if not is_valid_reviewer_id(value):
            raise ValueError(
                "adjudicator_id must match 'rev_' followed by 8 lowercase alphanumeric characters"
            )
        return value

    @field_validator("disagreeing_reviewers")
    @classmethod
    def _validate_reviewers(cls, value: list[str]) -> list[str]:
        """Overridden reviewers are named by pseudonym."""
        for reviewer_id in value:
            if not is_valid_reviewer_id(reviewer_id):
                raise ValueError(
                    "disagreeing_reviewers entries must be pseudonymous reviewer identifiers"
                )
        return value

    @model_validator(mode="after")
    def _validate_adjudication(self) -> Adjudication:
        """An adjudicator may not be one of the reviewers being overridden."""
        require_timezone_aware(self.adjudicated_at, "adjudicated_at")
        if (
            self.adjudicator_id in self.disagreeing_reviewers
        ):  # Self-adjudication is not independent resolution
            raise ValueError("an adjudicator must not be one of the disagreeing reviewers")
        return self


# ---------------------------------------------------------------------------
# The case
# ---------------------------------------------------------------------------


class EvalCase(VersionedModel):
    """One evaluation case, with its labels and its full review provenance."""

    SCHEMA_NAME: ClassVar[str] = "eval_case"
    # 1.1 adds the optional `context` field. A MINOR bump because it is purely
    # additive: is_compatible_version permits record_minor <= reader_minor, so
    # every 1.0 case on disk stays readable and behaves exactly as before.
    SCHEMA_VERSION: ClassVar[str] = "1.1"

    # -- identity -----------------------------------------------------------
    case_id: str = Field(
        description="Stable identifier, never reused. Retired cases are marked, not deleted."
    )
    dataset_version: str = Field(description="Dataset version this case belongs to.")
    split: DatasetSplit = Field(
        default=DatasetSplit.UNASSIGNED, description="Train / dev / test assignment."
    )

    # -- query --------------------------------------------------------------
    query: str = Field(min_length=1, description="The query as a patient would phrase it.")
    normalized_query: str = Field(
        default="",
        description="Canonical form used for duplicate detection. Derived; recomputed and checked on load.",
    )
    context: list[str] = Field(
        default_factory=list,
        description=(
            "Earlier patient questions in the same conversation, oldest first, when this case "
            "is a follow-up. Empty for a standalone case, which is every case authored before "
            "schema 1.1 -- so an empty context means 'not a conversational case' and is scored "
            "exactly as before. Only the QUESTIONS are carried, never prior answer text: the "
            "answer a system gave is a property of the system under test, not a label, and "
            "baking one into a case would score every system against one system's output."
        ),
    )

    # -- labels -------------------------------------------------------------
    category: EvalCategory = Field(description="Which failure mode this case exercises.")
    expected_behavior: EvalExpectedBehavior = Field(description="What the system must do.")

    relevant_documents: list[GradedRelevance] = Field(
        default_factory=list, description="Graded document-level relevance."
    )
    relevant_chunks: list[GradedRelevance] = Field(
        default_factory=list,
        description="Graded chunk-level relevance, when labelled at that granularity.",
    )
    corpus_version: str = Field(
        min_length=1,
        description="Corpus version these labels were assigned against. Labels are NOT portable across corpus versions.",
    )

    required_concepts: list[str] = Field(
        default_factory=list, description="Concepts or claims a passing answer must convey."
    )
    forbidden_claims: list[str] = Field(
        default_factory=list,
        description="Claims that make an answer a failure regardless of anything else, e.g. a specific diagnosis or a dose instruction.",
    )
    citation_requirements: CitationRequirement = Field(
        default_factory=CitationRequirement, description="What a passing answer must cite."
    )

    # -- annotation ---------------------------------------------------------
    annotation_status: AnnotationStatus = Field(
        default=AnnotationStatus.PENDING_REVIEW,
        description="Review lifecycle stage. Defaults to pending_review.",
    )
    is_synthetic: bool = Field(
        default=False,
        description="True for engineering-authored illustrations. Terminal: a synthetic case can never gate a release.",
    )
    reviews: list[CaseReview] = Field(
        default_factory=list,
        description="Independent reviews, kept separable so agreement remains computable.",
    )
    adjudication: Adjudication | None = Field(
        default=None, description="Resolution of a reviewer disagreement, when one occurred."
    )
    disagreement_status: DisagreementStatus = Field(
        default=DisagreementStatus.NOT_APPLICABLE,
        description="Whether reviewers agreed. Derived; recomputed and checked on load.",
    )

    # -- privacy ------------------------------------------------------------
    privacy_class: PrivacyClass = Field(
        default=PrivacyClass.UNKNOWN,
        description="Where the query text came from. Defaults to 'unknown', which is treated as strictly as patient-derived, so an author must actively declare provenance rather than inherit a permissive default.",
    )
    redaction_status: RedactionStatus = Field(
        default=RedactionStatus.PENDING,
        description="Whether identifying detail has been removed. Defaults to 'pending': redaction is owed until someone says otherwise.",
    )
    redaction_notes: str = Field(
        default="", description="What was removed, described without reproducing it."
    )

    # -- provenance ---------------------------------------------------------
    author_note: str = Field(
        default="",
        description="Who authored the case and how, recorded as free text without identities.",
    )
    notes: str = Field(default="", description="General notes.")
    known_limitations: str = Field(
        default="",
        description="What this case does NOT establish. A case with no stated limitations usually means nobody looked for them.",
    )
    excluded: bool = Field(
        default=False,
        description="Excluded from all use, e.g. failed redaction or an unresolvable label.",
    )
    exclusion_reason: str | None = Field(
        default=None, description="Why the case is excluded. Required when excluded."
    )
    created_at: datetime = Field(description="When the case was authored (timezone-aware).")
    updated_at: datetime | None = Field(
        default=None, description="When the case was last modified."
    )

    @field_validator("case_id")
    @classmethod
    def _validate_case_id(cls, value: str) -> str:
        """Case identifiers double as filename fragments, so the grammar is strict."""
        if not CASE_ID_PATTERN.match(value):
            raise ValueError("case_id must be lowercase kebab-case, e.g. 'syn-emergency-0001'")
        return value

    @field_validator("dataset_version", "corpus_version")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        """Versions must be present; an unversioned label cannot be interpreted."""
        if not value.strip():
            raise ValueError("version fields must not be blank")
        return value

    @model_validator(mode="before")
    @classmethod
    def _derive_normalized_query(cls, data: object) -> object:
        """Fill ``normalized_query`` from ``query`` when it was not supplied.

        Derived rather than caller-supplied so it can never drift from the
        query it describes; the after-validator below rejects a mismatch even
        when a caller passes one explicitly.
        """
        if isinstance(data, dict) and data.get("query") and not data.get("normalized_query"):
            return {
                **data,
                "normalized_query": normalize_query(str(data["query"])),
            }  # Copy rather than mutate the caller's dict
        return data

    @model_validator(mode="after")
    def _validate_case(self) -> EvalCase:
        """Enforce every cross-field rule that keeps a label honest."""
        if self.normalized_query != normalize_query(
            self.query
        ):  # A hand-edited normalisation would silently break duplicate detection
            raise ValueError("normalized_query does not match the normalisation of query")

        # -- synthetic is terminal ------------------------------------------
        if self.is_synthetic and self.annotation_status is not AnnotationStatus.SYNTHETIC:
            raise ValueError("a synthetic case must carry annotation_status 'synthetic'")
        if self.annotation_status is AnnotationStatus.SYNTHETIC and not self.is_synthetic:
            raise ValueError("annotation_status 'synthetic' requires is_synthetic to be true")
        if self.is_synthetic and self.reviews:
            # Reviewing a synthetic case is not forbidden as an activity, but
            # recording it here would make the case look reviewed in every
            # count and report. Re-author it as a real case instead.
            raise ValueError(
                "a synthetic case must not carry review records; re-author it as a real case first"
            )

        # -- review claims require review evidence --------------------------
        if self.annotation_status is AnnotationStatus.REVIEWED and not self.reviews:
            raise ValueError("annotation_status 'reviewed' requires at least one review record")
        if self.annotation_status is AnnotationStatus.ADJUDICATED:
            if (
                self.adjudication is None
            ):  # The status names an event; the record is the evidence it happened
                raise ValueError("annotation_status 'adjudicated' requires an adjudication record")
            if len(self.reviews) < 2:  # There is nothing to adjudicate between
                raise ValueError(
                    "annotation_status 'adjudicated' requires at least two reviews to adjudicate between"
                )
        if (
            self.adjudication is not None
            and self.annotation_status is not AnnotationStatus.ADJUDICATED
        ):
            raise ValueError("an adjudication record requires annotation_status 'adjudicated'")

        # -- reviewer independence ------------------------------------------
        reviewer_ids = [review.reviewer_id for review in self.reviews]
        if len(reviewer_ids) != len(set(reviewer_ids)):
            # Two reviews from one reviewer are not two independent judgements,
            # and counting them as such would inflate agreement.
            raise ValueError("each reviewer may contribute at most one review per case")

        # -- disagreement status must match the reviews it describes --------
        expected_disagreement = self._compute_disagreement_status()
        if self.disagreement_status is not expected_disagreement:
            raise ValueError(
                f"disagreement_status '{self.disagreement_status.value}' does not match the reviews; expected '{expected_disagreement.value}'"
            )

        # -- adjudicated labels must be the ones that stand -----------------
        if self.adjudication is not None:
            if self.category is not self.adjudication.resolved_category:
                raise ValueError("case category must match the adjudicated resolution")
            if self.expected_behavior is not self.adjudication.resolved_expected_behavior:
                raise ValueError("case expected_behavior must match the adjudicated resolution")

        # -- privacy ---------------------------------------------------------
        if (
            self.privacy_class in REDACTION_REQUIRED_CLASSES
            and self.redaction_status is RedactionStatus.NOT_REQUIRED
        ):
            raise ValueError(
                f"privacy_class '{self.privacy_class.value}' cannot have redaction_status 'not_required'"
            )

        # -- exclusion -------------------------------------------------------
        if (
            self.excluded and not self.exclusion_reason
        ):  # An unexplained exclusion cannot be revisited
            raise ValueError("an excluded case requires an exclusion_reason")

        # -- behavioural coherence -------------------------------------------
        if self.expected_behavior is EvalExpectedBehavior.ANSWER and not any(
            g.grade >= 2 for g in self.relevant_documents
        ):
            # A case expecting an answer needs evidence capable of supporting
            # one. This is the check the shipped evaluation set would have
            # failed: all four of its ground-truth PMIDs are absent from the corpus.
            raise ValueError(
                "a case expecting 'answer' must grade at least one document 2 or higher"
            )
        if self.expected_behavior is EvalExpectedBehavior.ABSTAIN and any(
            g.grade > 0 for g in self.relevant_documents
        ):
            # An abstention probe is only meaningful when the corpus genuinely
            # cannot answer it.
            raise ValueError("a case expecting 'abstain' must not grade any document above 0")
        if (
            self.category is EvalCategory.NEGATED_EMERGENCY
            and self.expected_behavior is EvalExpectedBehavior.EMERGENCY_ESCALATION
        ):
            # The entire purpose of this category is that negated emergency
            # vocabulary must NOT escalate.
            raise ValueError("a negated_emergency case must not expect emergency escalation")

        require_timezone_aware(self.created_at, "created_at")
        if self.updated_at is not None:
            require_timezone_aware(self.updated_at, "updated_at")
        return self

    def _compute_disagreement_status(self) -> DisagreementStatus:
        """Derive the disagreement status from the reviews actually present."""
        if len(self.reviews) < 2:  # Nothing to compare
            return DisagreementStatus.NOT_APPLICABLE
        categories = {review.category for review in self.reviews}
        behaviors = {review.expected_behavior for review in self.reviews}
        if len(categories) == 1 and len(behaviors) == 1:
            return DisagreementStatus.AGREED
        return (
            DisagreementStatus.DISAGREED_RESOLVED
            if self.adjudication is not None
            else DisagreementStatus.DISAGREED_OPEN
        )

    @property
    def reviewer_count(self) -> int:  # Used by the gating policy
        """Number of independent reviews on this case."""
        return len(self.reviews)

    @property
    def relevant_document_ids(self) -> set[str]:  # Used by leakage detection
        """Documents graded above zero for this case."""
        return {g.target_id for g in self.relevant_documents if g.is_relevant}


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class EvalDatasetManifest(VersionedModel):
    """Versioned manifest describing one evaluation dataset."""

    SCHEMA_NAME: ClassVar[str] = "eval_dataset_manifest"
    SCHEMA_VERSION: ClassVar[str] = "1.0"

    dataset_id: str = Field(
        min_length=1, description="Stable dataset identifier, e.g. 'synapse-evalset'."
    )
    version: str = Field(description="Semantic version, MAJOR.MINOR.PATCH.")
    corpus_version: str = Field(
        min_length=1, description="Corpus version every case was labelled against."
    )
    created_at: datetime = Field(description="When this version was assembled (timezone-aware).")

    case_count: int = Field(ge=0, description="Total cases, including excluded and synthetic ones.")
    cases_sha256: str = Field(
        description="Order-sensitive digest over the serialised cases. Any content change alters it."
    )

    category_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Cases per category. Always carries every category, so gaps are visible.",
    )
    status_counts: dict[str, int] = Field(
        default_factory=dict, description="Cases per annotation status."
    )
    split_counts: dict[str, int] = Field(default_factory=dict, description="Cases per split.")

    gating_eligible_count: int = Field(
        default=0,
        ge=0,
        description="Cases that satisfy the release-gating policy. Reported prominently because it is usually far smaller than case_count.",
    )
    minimum_reviews_for_gating: int = Field(
        default=2,
        ge=1,
        description="Independent reviews a case needs before it may gate a release.",
    )
    protocol_version: str = Field(
        min_length=1, description="Version of the labelling protocol this dataset was built under."
    )

    generator: str = Field(min_length=1, description="Tool and version that assembled the dataset.")
    git_commit: str | None = Field(
        default=None, description="Source commit when available; never fabricated."
    )
    provenance_notes: list[str] = Field(
        default_factory=list,
        description="Statements a reader must see before the counts, e.g. that no case is clinician-reviewed.",
    )

    @field_validator("version")
    @classmethod
    def _validate_semver(cls, value: str) -> str:
        """Strict MAJOR.MINOR.PATCH."""
        if not DATASET_VERSION_PATTERN.match(value):
            raise ValueError("version must be a semantic version, MAJOR.MINOR.PATCH")
        return value

    @field_validator("cases_sha256")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        """Reject anything that is not a lowercase 64-character hex digest."""
        if not is_sha256_hex(value):
            raise ValueError("cases_sha256 must be 64 lowercase hexadecimal characters")
        return value

    @model_validator(mode="after")
    def _validate_manifest(self) -> EvalDatasetManifest:
        """Reject naive timestamps and counts that do not reconcile."""
        require_timezone_aware(self.created_at, "created_at")
        for name, counts in (
            ("category_counts", self.category_counts),
            ("status_counts", self.status_counts),
            ("split_counts", self.split_counts),
        ):
            if (
                counts and sum(counts.values()) != self.case_count
            ):  # A summary that does not add up means the dataset was assembled incorrectly
                raise ValueError(f"{name} must sum to case_count")
        if self.gating_eligible_count > self.case_count:
            raise ValueError("gating_eligible_count must not exceed case_count")
        return self


__all__ = [
    "CASE_ID_PATTERN",
    "GATING_ELIGIBLE_STATUSES",
    "MAX_RELEVANCE_GRADE",
    "REDACTION_REQUIRED_CLASSES",
    "Adjudication",
    "CaseReview",
    "CitationRequirement",
    "EvalCase",
    "EvalDatasetManifest",
    "GradedRelevance",
    "normalize_query",
]

"""
synapse.schemas.enums
=====================
Closed vocabularies.

Every enumerated field in the system resolves to a member of one of these
enums. They are *closed*: a value outside the enum fails validation rather
than being coerced, stored as-is, or silently mapped to a default. Adding a
member is therefore a deliberate schema change with a version bump, not an
incidental data change.

``StrEnum`` (3.11+) is used so members serialise to plain JSON strings — a
corpus file stays human-readable and diffable — while still comparing equal to
their string form in existing code.
"""

from __future__ import annotations  # Postponed annotations

from enum import (
    StrEnum,  # 3.11 stdlib: enum members that ARE strings, so JSON round-trips need no custom encoder
)


class SourceType(StrEnum):  # What kind of artifact a document originally came from
    """Origin format of a source document."""

    PUBMED_ABSTRACT = "pubmed_abstract"  # Fetched from NCBI E-utilities
    GUIDELINE = "guideline"  # Clinical practice guideline from an issuing organisation
    PDF = "pdf"  # Locally ingested PDF
    TXT = "txt"  # Locally ingested plain text


class EvidenceType(StrEnum):  # Study design / evidence class of a document
    """Evidence classification. ``UNCLASSIFIED`` is the honest default."""

    GUIDELINE = "guideline"
    SYSTEMATIC_REVIEW = "systematic_review"
    META_ANALYSIS = "meta_analysis"
    RCT = "rct"
    COHORT = "cohort"
    CASE_CONTROL = "case_control"
    CASE_REPORT = "case_report"
    NARRATIVE_REVIEW = "narrative_review"
    PREPRINT = "preprint"
    OTHER = "other"
    UNCLASSIFIED = (
        "unclassified"  # No classification has been performed; every migrated document starts here
    )


class EvidenceTypeProvenance(
    StrEnum
):  # HOW the evidence type was determined — required alongside the type itself
    """Provenance of an evidence-type label.

    Recorded separately and mandatorily so a heuristic guess can never be
    mistaken for a curated clinical judgement.
    """

    PUBLISHER_METADATA = "publisher_metadata"  # Derived from upstream publication-type metadata
    HEURISTIC = "heuristic"  # Inferred by a rule in this codebase
    HUMAN_LABELED = "human_labeled"  # Assigned by a person, with a review record
    UNKNOWN = "unknown"  # Not determined; the default for migrated content


class ApprovalStatus(StrEnum):  # Governance state of a source document
    """Approval lifecycle state.

    ``UNREVIEWED`` is the default everywhere and is what all existing corpus
    content migrates as: the repository contains no review metadata of any
    kind, so no other value can be truthfully asserted.
    """

    UNREVIEWED = "unreviewed"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"


class RetractionStatus(StrEnum):  # Whether a publication has been retracted or flagged
    """Retraction screening state. ``UNCHECKED`` is the default."""

    NONE = "none"  # Screened, and clean
    RETRACTED = "retracted"
    EXPRESSION_OF_CONCERN = "expression_of_concern"
    CORRECTED = "corrected"
    UNCHECKED = (
        "unchecked"  # No screening has run; distinct from NONE, which asserts a clean result
    )


class ReviewDecision(StrEnum):  # Outcome recorded by a reviewer
    """Decision captured in a review record."""

    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_CHANGES = "needs_changes"


class ReviewerRole(StrEnum):  # Role of the (pseudonymous) reviewer
    """Reviewer role. Only ``CLINICIAN`` may approve clinical content."""

    CLINICIAN = "clinician"
    PHARMACIST = "pharmacist"
    CLINICAL_INFORMATICIST = "clinical_informaticist"
    NON_CLINICAL = "non_clinical"


class ReviewStatus(StrEnum):  # Review state of an evaluation case
    """Review lifecycle for an evaluation case."""

    UNREVIEWED = "unreviewed"
    IN_REVIEW = "in_review"
    CLINICIAN_REVIEWED = "clinician_reviewed"
    DISPUTED = "disputed"
    RETIRED = "retired"


class QueryCategory(StrEnum):  # Category of an evaluation query
    """Query taxonomy for the evaluation dataset."""

    SYMPTOM_EDUCATION = "symptom_education"
    MEDICATION = "medication"
    LAB_INTERPRETATION = "lab_interpretation"
    PROCEDURE_PREP = "procedure_prep"
    SCREENING_PREVENTION = "screening_prevention"
    CHRONIC_MANAGEMENT = "chronic_management"
    EMERGENCY = "emergency"
    OUT_OF_SCOPE = "out_of_scope"
    ADVERSARIAL = "adversarial"
    ABSTENTION_PROBE = "abstention_probe"


class ExpectedBehavior(StrEnum):  # What the system is expected to do for an evaluation case
    """Expected top-level system behaviour."""

    ANSWER = "answer"
    ABSTAIN = "abstain"
    ROUTE_EMERGENCY = "route_emergency"


class EmergencyExpectation(StrEnum):  # Expected emergency-routing outcome
    """Expected emergency routing decision."""

    ROUTE = "route"
    NO_ROUTE = "no_route"


class ClaimType(StrEnum):  # Kind of statement a claim makes
    """Claim taxonomy.

    Only ``FACTUAL`` claims require citations. Without this distinction,
    citation completeness would penalise the boundary disclaimer and the
    question card, which are correctly uncited.
    """

    FACTUAL = "factual"
    CONTEXTUAL = "contextual"
    PROCEDURAL = "procedural"
    BOUNDARY = "boundary"


class AnswerDecision(StrEnum):  # Outcome of citation validation for a structured answer
    """Publication decision produced by the citation-validation ladder."""

    PUBLISH = "publish"
    DEGRADE = "degrade"
    ABSTAIN = "abstain"
    ROUTE_EMERGENCY = "route_emergency"


class SourceLifecycleState(StrEnum):  # Governance state of ONE source inside a source pack
    """Lifecycle of a single source within a source pack.

    Deliberately distinct from :class:`ApprovalStatus`, which describes a
    corpus document in isolation. This enum describes a source's standing
    *within a reviewed pack*, and its central purpose is to keep automated
    discovery and human clinical approval in separate, non-overlapping states.

    Only ``DISCOVERED`` may be produced by automation. Every transition into
    ``SCREENED``, ``APPROVED`` or ``REJECTED`` requires a human review record;
    see :mod:`synapse.governance.states` for the enforced transition table.
    """

    DISCOVERED = "discovered"  # Found by automated ingestion. Asserts existence only — no human has looked at it.
    SCREENED = "screened"  # A human has triaged it as in-scope, but has not approved it for patient-facing use.
    APPROVED = "approved"  # A qualified reviewer approved it for the pack's declared scope. Requires a complete review record.
    REJECTED = "rejected"  # A human reviewed and excluded it. Requires a recorded reason.
    EXPIRED = "expired"  # A prior approval passed its review-due date. Machine-applied; the approval is no longer valid.
    SUPERSEDED = "superseded"  # Replaced by a newer source. Terminal: a superseded source is never revived, a new entry is added instead.


class PackApprovalState(StrEnum):  # Governance state of a WHOLE source pack
    """Approval state of an entire source pack.

    ``UNAPPROVED_EXAMPLE`` exists so a demonstration pack can never be mistaken
    for a reviewed one. It is a first-class state rather than a naming
    convention, so the eligibility layer can refuse it outright.
    """

    DRAFT = "draft"  # Under construction. Sources may be added and removed freely.
    IN_REVIEW = "in_review"  # Submitted for clinical review; contents frozen pending a decision.
    APPROVED = "approved"  # Reviewed and approved for the declared scope. Requires every rule in synapse.governance.pack to hold.
    EXPIRED = "expired"  # A prior pack approval passed its review-due date.
    SUPERSEDED = "superseded"  # A later pack version replaces this one.
    UNAPPROVED_EXAMPLE = "unapproved_example"  # Demonstration content only. NEVER eligible for a production index, by construction.


class EvalCategory(StrEnum):  # What KIND of query an evaluation case exercises
    """Evaluation-case taxonomy.

    Chosen so that each category exercises a *different failure mode*, not a
    different medical topic. Topic coverage belongs to the source pack; this
    axis is about what the system must get right.

    ``NEGATED_EMERGENCY`` is deliberately separate from ``EMERGENCY_RED_FLAG``.
    The shipped detector substring-matches emergency phrases, so "I do NOT have
    chest pain" routes to emergency today. Without a category that isolates
    negation, that false positive is invisible in an aggregate score.
    """

    FOLLOWUP_RESOLUTION = "followup_resolution"  # Depends on an earlier turn: "how is it treated?" after naming a condition
    ORDINARY_EDUCATION = "ordinary_education"  # Straightforward, answerable from the corpus
    AMBIGUOUS_SYMPTOMS = (
        "ambiguous_symptoms"  # Under-specified presentation; several readings are plausible
    )
    OUT_OF_SCOPE = "out_of_scope"  # Irrelevant or outside the product's declared scope
    ADVERSARIAL_INJECTION = (
        "adversarial_injection"  # Attempts to override instructions or extract the prompt
    )
    MEDICATION = (
        "medication"  # Drug names, doses, interactions — the highest-consequence text class
    )
    EMERGENCY_RED_FLAG = "emergency_red_flag"  # Presentation that must escalate
    NEGATED_EMERGENCY = (
        "negated_emergency"  # Emergency vocabulary under negation; must NOT escalate
    )
    INSUFFICIENT_EVIDENCE = (
        "insufficient_evidence"  # In scope, but the corpus cannot support an answer
    )


class EvalExpectedBehavior(StrEnum):  # What the system must DO for a case
    """Expected top-level system behaviour.

    Distinct from :class:`ExpectedBehavior`, which is the coarser three-value
    vocabulary used by the older stub. Routing and escalation are separated
    here because they are different actions with different urgency: routing
    hands off to staff, escalation says stop and seek care now.
    """

    ANSWER = "answer"  # Produce a cited, grounded answer
    ABSTAIN = "abstain"  # Decline to answer; evidence is insufficient
    ROUTE_TO_STAFF = "route_to_staff"  # Hand off to a human member of clinical staff
    EMERGENCY_ESCALATION = "emergency_escalation"  # Immediate escalation ahead of any retrieval
    REJECT_UNSAFE_INSTRUCTION = (
        "reject_unsafe_instruction"  # Refuse an instruction that would breach the safety contract
    )


class AnnotationStatus(StrEnum):  # How far a case has progressed through review
    """Annotation lifecycle.

    ``SYNTHETIC`` is a *terminal* status for engineering-authored content, not a
    stage on the way to review. A synthetic case is excluded from release
    gating by construction; promoting one requires re-authoring it as a real
    case with real provenance.
    """

    SYNTHETIC = "synthetic"  # Engineering-authored illustration. Never release-gating.
    PENDING_REVIEW = "pending_review"  # Authored and awaiting reviewer attention
    REVIEWED = "reviewed"  # Reviewed, with no unresolved disagreement
    ADJUDICATED = "adjudicated"  # Reviewers disagreed; an adjudicator resolved it


class PrivacyClass(StrEnum):  # Where a query's text came from
    """Privacy classification of a case's query text.

    Drives redaction obligations. ``UNKNOWN`` is treated as strictly as
    ``PATIENT_DERIVED``: unknown provenance is not a licence to assume safety.
    """

    SYNTHETIC = "synthetic"  # Authored from scratch; no real person is described
    PUBLIC_DERIVED = "public_derived"  # Adapted from published, already-public material
    PATIENT_DERIVED = (
        "patient_derived"  # Originates from a real person's words. Redaction mandatory.
    )
    UNKNOWN = "unknown"  # Provenance not established. Treated as patient-derived.


class RedactionStatus(StrEnum):  # Whether identifying detail has been removed
    """Redaction state of a case's text."""

    NOT_REQUIRED = "not_required"  # Synthetic or public-derived text with nothing to redact
    PENDING = "pending"  # Redaction owed but not performed
    REDACTED = "redacted"  # Identifying detail removed and the removal verified
    FAILED = "failed"  # Redaction attempted and judged inadequate. Case must be excluded.


class DatasetSplit(StrEnum):  # Which evaluation split a case belongs to
    """Train / development / test assignment."""

    TRAIN = "train"  # Prompt and retrieval tuning
    DEV = "dev"  # Iteration and threshold selection
    TEST = "test"  # Held out. Opened rarely and deliberately.
    UNASSIGNED = "unassigned"  # Not yet placed into a split


class DisagreementStatus(StrEnum):  # Whether reviewers agreed
    """Outcome of comparing two or more independent reviews."""

    NOT_APPLICABLE = "not_applicable"  # Fewer than two reviews exist
    AGREED = "agreed"  # All reviews match on every gating-relevant field
    DISAGREED_OPEN = "disagreed_open"  # Reviews conflict and nobody has adjudicated
    DISAGREED_RESOLVED = (
        "disagreed_resolved"  # Reviews conflicted; an adjudicator recorded a resolution
    )


class NoticeType(StrEnum):  # PubMed CommentsCorrections relationships
    """Relationship recorded by a PubMed ``<CommentsCorrections RefType=...>`` element.

    Direction is load-bearing and easy to get wrong. ``*_IN`` means *this*
    article is the subject of a notice published elsewhere — a
    ``RETRACTION_IN`` marks the article as retracted. ``*_OF`` / ``*_FOR``
    means this article *is* the notice about something else, which does NOT
    make it retracted. Conflating the two either hides retracted evidence or
    wrongly discards a legitimate retraction notice.
    """

    RETRACTION_IN = "retraction_in"  # This article has been retracted; notice published elsewhere
    RETRACTION_OF = "retraction_of"  # This article IS a retraction notice for another article
    EXPRESSION_OF_CONCERN_IN = (
        "expression_of_concern_in"  # An expression of concern was issued about this article
    )
    EXPRESSION_OF_CONCERN_FOR = (
        "expression_of_concern_for"  # This article IS an expression of concern about another
    )
    ERRATUM_IN = "erratum_in"  # A correction to this article was published elsewhere
    ERRATUM_FOR = "erratum_for"  # This article IS a correction to another article
    CORRECTED_AND_REPUBLISHED_IN = (
        "corrected_and_republished_in"  # This article was corrected and republished elsewhere
    )
    CORRECTED_AND_REPUBLISHED_FROM = (
        "corrected_and_republished_from"  # This article is the corrected republication of another
    )
    UPDATE_IN = "update_in"  # An update to this article exists
    UPDATE_OF = "update_of"  # This article updates another
    COMMENT_IN = "comment_in"  # A comment on this article exists
    COMMENT_ON = "comment_on"  # This article comments on another
    OTHER = "other"  # A RefType outside the set above; preserved rather than dropped


class DatePrecision(StrEnum):  # How precisely a publication date is known
    """Granularity of a parsed publication date.

    PubMed dates range from a full Y-M-D to a free-text ``MedlineDate`` such as
    ``"2024 Jan-Feb"``. Recording precision prevents a date that is only known
    to the year from being compared as though it were exact.
    """

    DAY = "day"
    MONTH = "month"  # Day defaulted to the 1st
    YEAR = "year"  # Month and day defaulted to January 1st
    UNKNOWN = "unknown"  # No parseable date at all


class DistanceMetric(StrEnum):  # Vector index distance function
    """Distance metric used by the vector index."""

    L2 = "l2"  # What the current FAISS IndexFlatL2 uses; equivalent to cosine ranking on L2-normalised vectors
    INNER_PRODUCT = "inner_product"
    COSINE = "cosine"


class IndexState(StrEnum):  # Whether a manifest describes a usable index
    """Readiness of the vector index described by a manifest."""

    READY = "ready"  # A FAISS index file exists, is hashed, and matches the corpus ordering
    REBUILD_REQUIRED = (
        "rebuild_required"  # Corpus exists but no compatible index does; loading must refuse
    )


class ChunkingAlgorithm(StrEnum):  # Which splitter produced the corpus
    """Chunking algorithm identifier, recorded in the index manifest."""

    RECURSIVE_CHARACTER = "recursive_character"  # LangChain RecursiveCharacterTextSplitter, the current production splitter
    LEGACY_IMPORTED = "legacy_imported"  # Chunk boundaries inherited from a pre-existing artifact; parameters recorded as best known, not re-derived
    SECTION_AWARE_SENTENCE = "section_aware_sentence"  # synapse.ingest.chunker: never mixes abstract sections, packs whole sentences, no overlap


class RunMode(StrEnum):  # Execution mode of an evaluation run
    """How an evaluation run obtained its model outputs."""

    OFFLINE_DETERMINISTIC = "offline_deterministic"  # Recorded embeddings and cassettes; no network
    LIVE = "live"  # Real API calls


__all__ = [
    "AnnotationStatus",
    "AnswerDecision",
    "ApprovalStatus",
    "ChunkingAlgorithm",
    "ClaimType",
    "DatasetSplit",
    "DatePrecision",
    "DisagreementStatus",
    "DistanceMetric",
    "EmergencyExpectation",
    "EvalCategory",
    "EvalExpectedBehavior",
    "EvidenceType",
    "EvidenceTypeProvenance",
    "ExpectedBehavior",
    "IndexState",
    "NoticeType",
    "PackApprovalState",
    "PrivacyClass",
    "QueryCategory",
    "RedactionStatus",
    "RetractionStatus",
    "ReviewDecision",
    "ReviewStatus",
    "ReviewerRole",
    "RunMode",
    "SourceLifecycleState",
    "SourceType",
]

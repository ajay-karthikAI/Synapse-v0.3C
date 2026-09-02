"""
synapse.schemas
===============
Typed models for every record Synapse persists or exchanges.

All models derive from :class:`synapse.schemas.base.SynapseModel`, which is
configured for *untrusted input*: unknown fields are rejected rather than
ignored, models are immutable once constructed, and no value is silently
coerced across types. A corpus file, a manifest and a migration input are all
treated as hostile until they have passed through one of these models.

Re-exported here so callers can write ``from synapse.schemas import
EvidenceChunk`` without knowing which submodule defines it.
"""

from __future__ import annotations  # Postponed annotations

from synapse.schemas.answer import AnswerClaim, ClaimExcerpt, RetrievedEvidence, StructuredAnswer
from synapse.schemas.base import (
    SCHEMA_REGISTRY,
    SynapseModel,
    VersionedModel,
    is_compatible_version,
    parse_schema_version,
    require_compatible_version,
)
from synapse.schemas.chunk import EvidenceChunk
from synapse.schemas.enums import (
    AnnotationStatus,
    AnswerDecision,
    ApprovalStatus,
    ChunkingAlgorithm,
    ClaimType,
    DatasetSplit,
    DatePrecision,
    DisagreementStatus,
    DistanceMetric,
    EmergencyExpectation,
    EvalCategory,
    EvalExpectedBehavior,
    EvidenceType,
    EvidenceTypeProvenance,
    ExpectedBehavior,
    IndexState,
    NoticeType,
    PackApprovalState,
    PrivacyClass,
    QueryCategory,
    RedactionStatus,
    RetractionStatus,
    ReviewDecision,
    ReviewerRole,
    ReviewStatus,
    RunMode,
    SourceLifecycleState,
    SourceType,
)
from synapse.schemas.evalset import (
    Adjudication,
    CaseReview,
    CitationRequirement,
    EvalCase,
    EvalDatasetManifest,
    GradedRelevance,
    normalize_query,
)
from synapse.schemas.evaluation import EvaluationRun
from synapse.schemas.index import ChunkingConfig, EmbeddingConfig, IndexManifest, ManifestArtifact
from synapse.schemas.source import (
    AbstractSection,
    Author,
    DocumentIdentifiers,
    RelatedNotice,
    SourceContainer,
    SourceDocument,
    SourceReview,
)
from synapse.schemas.source_pack import (
    ClinicianReview,
    PackScope,
    PackSource,
    ReviewerRequirements,
    SourcePackManifest,
    SourceSelectionPolicy,
)

__all__ = [  # Explicit, alphabetised public surface
    "SCHEMA_REGISTRY",
    "AbstractSection",
    "Adjudication",
    "AnnotationStatus",
    "AnswerClaim",
    "AnswerDecision",
    "ApprovalStatus",
    "Author",
    "CaseReview",
    "ChunkingAlgorithm",
    "ChunkingConfig",
    "CitationRequirement",
    "ClaimExcerpt",
    "ClaimType",
    "ClinicianReview",
    "DatasetSplit",
    "DatePrecision",
    "DisagreementStatus",
    "DistanceMetric",
    "DocumentIdentifiers",
    "EmbeddingConfig",
    "EmergencyExpectation",
    "EvalCase",
    "EvalCategory",
    "EvalDatasetManifest",
    "EvalExpectedBehavior",
    "EvaluationRun",
    "EvidenceChunk",
    "EvidenceType",
    "EvidenceTypeProvenance",
    "ExpectedBehavior",
    "GradedRelevance",
    "IndexManifest",
    "IndexState",
    "ManifestArtifact",
    "NoticeType",
    "PackApprovalState",
    "PackScope",
    "PackSource",
    "PrivacyClass",
    "QueryCategory",
    "RedactionStatus",
    "RelatedNotice",
    "RetractionStatus",
    "RetrievedEvidence",
    "ReviewDecision",
    "ReviewStatus",
    "ReviewerRequirements",
    "ReviewerRole",
    "RunMode",
    "SourceContainer",
    "SourceDocument",
    "SourceLifecycleState",
    "SourcePackManifest",
    "SourceReview",
    "SourceSelectionPolicy",
    "SourceType",
    "StructuredAnswer",
    "SynapseModel",
    "VersionedModel",
    "is_compatible_version",
    "normalize_query",
    "parse_schema_version",
    "require_compatible_version",
]

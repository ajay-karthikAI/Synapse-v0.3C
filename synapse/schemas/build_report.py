"""
synapse.schemas.build_report
============================
:class:`CorpusBuildReport` — the machine-readable record of how a corpus was built.

A corpus without a build report is unauditable: you can see what ended up in it,
but not what was fetched and discarded, why a record was dropped as a duplicate,
or whether retracted material was excluded. That matters most for the decisions
this pipeline makes silently by default — dropping duplicates and excluding
retracted publications.

The report is **deterministic**: given the same inputs and configuration, every
field except the wall-clock timestamps is byte-identical between runs. Decision
lists preserve the order in which decisions were made, and summary maps always
carry the same keys — including rules that fired zero times — so two reports can
be diffed directly.
"""

from __future__ import annotations  # Postponed annotations

from datetime import datetime
from typing import ClassVar

from pydantic import Field, model_validator

from synapse.schemas.base import SynapseModel, VersionedModel, require_timezone_aware


class QueryReport(SynapseModel):
    """What one search query contributed."""

    query: str = Field(min_length=1, description="The PubMed search expression, verbatim.")
    requested: int = Field(ge=0, description="Maximum records requested for this query.")
    pmids_returned: int = Field(ge=0, description="PMIDs the search returned.")
    documents_parsed: int = Field(ge=0, description="Records that parsed into a source document.")
    parse_failures: int = Field(
        ge=0, description="Records that could not be parsed, e.g. missing a PMID."
    )


class ExclusionRecord(SynapseModel):
    """One document excluded from the corpus, and why."""

    document_id: str = Field(min_length=1, description="Identifier of the excluded document.")
    reason: str = Field(min_length=1, description="Machine-readable exclusion reason.")
    detail: str = Field(
        default="",
        description="Human-readable context, e.g. the retraction status that triggered exclusion.",
    )


class CorpusBuildReport(VersionedModel):
    """Complete, machine-readable account of one corpus build."""

    SCHEMA_NAME: ClassVar[str] = "corpus_build_report"
    SCHEMA_VERSION: ClassVar[str] = "1.0"

    # -- identity and provenance --------------------------------------------
    build_id: str = Field(min_length=1, description="Identifier for this build.")
    corpus_version: str = Field(min_length=1, description="Corpus version produced.")
    source_pack_version: str = Field(
        min_length=1, description="Source pack version stamped on the documents."
    )
    started_at: datetime = Field(description="Build start (timezone-aware).")
    completed_at: datetime = Field(description="Build completion (timezone-aware).")
    synapse_version: str = Field(
        min_length=1, description="Package version that produced the build."
    )
    git_commit: str | None = Field(
        default=None, description="Source commit when available; never fabricated."
    )
    config_sha256: str | None = Field(
        default=None,
        description="Digest of the configuration file, binding a report to the settings that produced it.",
    )

    # -- retrieval ----------------------------------------------------------
    queries: list[QueryReport] = Field(
        default_factory=list, description="Per-query retrieval outcomes, in configuration order."
    )
    documents_fetched: int = Field(
        ge=0, description="Documents parsed across all queries, before deduplication."
    )

    # -- deduplication ------------------------------------------------------
    duplicate_decisions: list[dict[str, str]] = Field(
        default_factory=list,
        description="Every duplicate drop: the record dropped, the record kept, the rule that matched, and the matched key.",
    )
    duplicates_by_rule: dict[str, int] = Field(
        default_factory=dict,
        description="Duplicate counts per rule. Always carries every rule, including those that fired zero times.",
    )
    documents_after_dedupe: int = Field(
        ge=0, description="Documents remaining after deduplication."
    )

    # -- governance ---------------------------------------------------------
    exclusions: list[ExclusionRecord] = Field(
        default_factory=list, description="Documents excluded from the corpus, with reasons."
    )
    retraction_summary: dict[str, int] = Field(
        default_factory=dict,
        description="Document counts per retraction status, computed before exclusion.",
    )
    retracted_excluded: bool = Field(
        description="Whether retracted material was excluded from the corpus. True by default."
    )
    approval_summary: dict[str, int] = Field(
        default_factory=dict,
        description="Document counts per approval status. Automatic retrieval yields 'unreviewed' only.",
    )

    # -- output -------------------------------------------------------------
    documents_written: int = Field(ge=0, description="Documents written to the corpus.")
    chunks_written: int = Field(ge=0, description="Chunks written to the corpus.")
    chunking_stats: dict[str, int] = Field(
        default_factory=dict,
        description="Chunking outcomes: documents chunked, documents without chunks, short sections dropped, oversized sentences retained.",
    )
    evidence_type_summary: dict[str, int] = Field(
        default_factory=dict, description="Document counts per normalised evidence type."
    )

    # -- honesty ------------------------------------------------------------
    provenance_notes: list[str] = Field(
        default_factory=list,
        description="Statements a reader must see before interpreting the counts, e.g. that nothing is clinician-reviewed.",
    )

    @model_validator(mode="after")
    def _validate_report(self) -> CorpusBuildReport:
        """Reject naive timestamps, inverted intervals and counts that do not reconcile."""
        require_timezone_aware(self.started_at, "started_at")
        require_timezone_aware(self.completed_at, "completed_at")
        if self.completed_at < self.started_at:  # Usually a timezone bug rather than time travel
            raise ValueError("completed_at must not precede started_at")
        expected_after_dedupe = self.documents_fetched - len(self.duplicate_decisions)
        if (
            self.documents_after_dedupe != expected_after_dedupe
        ):  # A report whose arithmetic does not close cannot be trusted for auditing
            raise ValueError(
                "documents_after_dedupe must equal documents_fetched minus the number of duplicate decisions"
            )
        if (
            self.documents_written > self.documents_after_dedupe
        ):  # Writing more than survived deduplication is impossible
            raise ValueError("documents_written must not exceed documents_after_dedupe")
        return self


__all__ = ["CorpusBuildReport", "ExclusionRecord", "QueryReport"]

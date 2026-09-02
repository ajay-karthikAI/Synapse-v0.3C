"""
synapse.ingest.pipeline
=======================
Corpus build orchestration: search → fetch → parse → deduplicate → screen →
chunk → write verified artifacts.

The governance decision this module makes, stated explicitly because it is
made by default and would otherwise be invisible:

    **Retracted publications and articles under an expression of concern are
    excluded from the corpus.** They are counted, listed individually in the
    build report with their status, and never silently dropped. Including them
    requires ``include_retracted = true`` in configuration, and even then they
    remain marked with their retraction status on every record.

The pipeline never claims medical approval. Every document it produces carries
``approval_status = "unreviewed"``, and the build report repeats that in its
``provenance_notes`` so a reader of the numbers cannot miss it. Automatic
retrieval from PubMed establishes only that a publication exists.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from synapse import __version__ as synapse_version
from synapse.corpus.jsonl import write_jsonl
from synapse.index.manifest import (
    build_index_manifest,
    detect_git_commit,
    make_artifact,
    write_manifest,
)
from synapse.index.verify import verify_artifacts
from synapse.ingest.chunker import ChunkingPolicy, chunk_documents
from synapse.ingest.dedupe import deduplicate, summarize_decisions
from synapse.ingest.eutils import EUtilsClient
from synapse.ingest.parse import parse_efetch_response
from synapse.logging import get_logger
from synapse.schemas.build_report import CorpusBuildReport, ExclusionRecord, QueryReport
from synapse.schemas.chunk import EvidenceChunk
from synapse.schemas.enums import ChunkingAlgorithm, DistanceMetric, RetractionStatus
from synapse.schemas.index import ChunkingConfig, EmbeddingConfig
from synapse.schemas.source import SourceDocument

logger = get_logger(__name__)

CHUNKS_FILENAME = "chunks.jsonl.gz"  # Corpus chunk records
DOCUMENTS_FILENAME = "documents.jsonl.gz"  # Source-document records
REPORT_FILENAME = "build_report.json"  # Machine-readable build report
MANIFEST_FILENAME = "manifest.json"  # Index manifest, verified before any load

# Retraction states that disqualify a document from the corpus by default.
# CORRECTED is deliberately absent: a corrected article is still valid evidence,
# and its correction is recorded in related_notices for a reader to follow.
EXCLUDED_RETRACTION_STATES = frozenset(
    {RetractionStatus.RETRACTED, RetractionStatus.EXPRESSION_OF_CONCERN}
)

PROVENANCE_NOTES = (  # Emitted verbatim into every report so counts are never read as a quality claim
    "Every document in this corpus is approval_status='unreviewed'. No clinician has "
    "reviewed any record. Automatic retrieval from PubMed confers no medical approval.",
    "evidence_type is derived mechanically from PubMed publication-type metadata "
    "(evidence_type_provenance='publisher_metadata'), not from clinical assessment.",
    "Retraction screening reflects PubMed metadata at retrieval time only. A publication "
    "retracted after this build will still be marked 'none' until the corpus is rebuilt.",
)


@dataclass
class BuildConfig:
    """Everything one corpus build needs. Populated from a TOML file by the CLI."""

    queries: list[str]  # PubMed search expressions
    output_dir: Path
    corpus_version: str
    source_pack_version: str | None = None  # Defaults to corpus_version
    index_version: str = "1"
    max_results_per_query: int = 20
    sort: str = (
        "relevance"  # Explicit: the legacy client omitted this and produced a recency-skewed corpus
    )
    include_retracted: bool = False  # Governance default: retracted material is excluded
    chunking: ChunkingPolicy = field(default_factory=ChunkingPolicy)
    embedding_model: str = (
        "text-embedding-3-small"  # Recorded in the manifest; no embeddings are computed here
    )
    embedding_dimensions: int = 1536
    embedding_provider: str = "openai"
    config_sha256: str | None = (
        None  # Digest of the source config file, binding a report to its settings
    )
    force: bool = False  # Overwrite a non-empty output directory


@dataclass
class BuildOutcome:
    """What a build produced."""

    report: CorpusBuildReport
    documents: list[SourceDocument]
    chunks: list[EvidenceChunk]
    output_dir: Path | None  # None on a dry run


def _fetch_documents(
    client: EUtilsClient,
    config: BuildConfig,
    *,
    retrieved_at: datetime,
) -> tuple[list[SourceDocument], list[QueryReport]]:
    """Run every query and parse the results, recording per-query outcomes."""
    documents: list[SourceDocument] = []
    query_reports: list[QueryReport] = []
    source_pack_version = config.source_pack_version or config.corpus_version

    for (
        query
    ) in config.queries:  # Configuration order is preserved so the report is stable across runs
        pmids = client.esearch(query, max_results=config.max_results_per_query, sort=config.sort)
        parsed_for_query: list[SourceDocument] = []
        for payload in client.efetch(pmids):
            parsed_for_query.extend(
                parse_efetch_response(
                    payload, retrieved_at=retrieved_at, source_pack_version=source_pack_version
                )
            )
        documents.extend(parsed_for_query)
        query_reports.append(
            QueryReport(
                query=query,
                requested=config.max_results_per_query,
                pmids_returned=len(pmids),
                documents_parsed=len(parsed_for_query),
                # A returned PMID that produced no document was unparseable —
                # no MedlineCitation, no Article element, or a non-numeric PMID.
                parse_failures=max(0, len(pmids) - len(parsed_for_query)),
            )
        )
    return documents, query_reports


def _screen_documents(
    documents: list[SourceDocument],
    *,
    include_retracted: bool,
) -> tuple[list[SourceDocument], list[ExclusionRecord]]:
    """Apply the retraction policy and drop documents with no abstract.

    Every exclusion is returned as a record, so nothing leaves the corpus
    without appearing in the build report.
    """
    kept: list[SourceDocument] = []
    exclusions: list[ExclusionRecord] = []

    for document in documents:
        if document.retraction_status in EXCLUDED_RETRACTION_STATES and not include_retracted:
            exclusions.append(
                ExclusionRecord(
                    document_id=document.document_id,
                    reason="retracted_or_concerned",
                    detail=f"retraction_status={document.retraction_status.value}",  # The status is stated, not merely implied
                )
            )
            continue
        if (
            not document.abstract_sections
        ):  # Nothing to chunk, so nothing retrievable; a title alone is not evidence
            exclusions.append(
                ExclusionRecord(
                    document_id=document.document_id,
                    reason="no_abstract",
                    detail="record has no AbstractText elements",
                )
            )
            continue
        kept.append(document)

    return kept, exclusions


def _count_by(values: list[str]) -> dict[str, int]:
    """Count occurrences, sorted by key so the report is byte-stable."""
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(
        sorted(counts.items())
    )  # Sorted: an unsorted dict would make two identical builds produce different bytes


def build_corpus(
    config: BuildConfig,
    *,
    client: EUtilsClient,
    dry_run: bool = False,
    now: datetime | None = None,
) -> BuildOutcome:
    """Run a full corpus build.

    The client is injected rather than constructed here, which is what lets the
    test suite drive the entire pipeline from local XML fixtures with no
    network access.
    """
    started_at = now or datetime.now(UTC)
    source_pack_version = config.source_pack_version or config.corpus_version

    # 1. Retrieve and parse.
    documents, query_reports = _fetch_documents(client, config, retrieved_at=started_at)
    fetched_count = len(documents)

    # 2. Deduplicate: PMID, then DOI, then normalised title + year.
    dedupe_result = deduplicate(documents)

    # 3. Screen: retraction policy and abstract presence. Counted BEFORE
    #    exclusion so the summary describes what was retrieved, not what survived.
    retraction_summary = _count_by([d.retraction_status.value for d in dedupe_result.documents])
    kept, exclusions = _screen_documents(
        dedupe_result.documents, include_retracted=config.include_retracted
    )

    # 4. Chunk, section by section.
    chunks, chunking_stats = chunk_documents(kept, ingested_at=started_at, policy=config.chunking)

    # 5. Attach chunk identifiers to their parent documents.
    chunk_ids_by_document: dict[str, list[str]] = {}
    for chunk in chunks:
        chunk_ids_by_document.setdefault(chunk.document_id, []).append(chunk.chunk_id)
    documents_out = [
        document.model_copy(
            update={"chunk_ids": chunk_ids_by_document.get(document.document_id, [])}
        )
        for document in kept
        if chunk_ids_by_document.get(
            document.document_id
        )  # A document that produced no chunk is not written; it would be unreachable
    ]

    completed_at = datetime.now(UTC) if now is None else now

    report = CorpusBuildReport(
        build_id=f"{config.corpus_version}-{config.index_version}",
        corpus_version=config.corpus_version,
        source_pack_version=source_pack_version,
        started_at=started_at,
        completed_at=completed_at,
        synapse_version=synapse_version,
        git_commit=detect_git_commit(Path.cwd()),  # None outside a checkout; never fabricated
        config_sha256=config.config_sha256,
        queries=query_reports,
        documents_fetched=fetched_count,
        duplicate_decisions=[decision.as_dict() for decision in dedupe_result.decisions],
        duplicates_by_rule=summarize_decisions(dedupe_result.decisions),
        documents_after_dedupe=len(dedupe_result.documents),
        exclusions=exclusions,
        retraction_summary=retraction_summary,
        retracted_excluded=not config.include_retracted,
        approval_summary=_count_by([d.approval_status.value for d in documents_out]),
        documents_written=len(documents_out),
        chunks_written=len(chunks),
        chunking_stats=chunking_stats,
        evidence_type_summary=_count_by([d.evidence_type.value for d in documents_out]),
        provenance_notes=list(PROVENANCE_NOTES),
    )

    if dry_run:  # Analysis complete; nothing written
        return BuildOutcome(report=report, documents=documents_out, chunks=chunks, output_dir=None)

    output_dir = _write_artifacts(config, documents_out, chunks, report, built_at=started_at)
    return BuildOutcome(
        report=report, documents=documents_out, chunks=chunks, output_dir=output_dir
    )


def _write_artifacts(
    config: BuildConfig,
    documents: list[SourceDocument],
    chunks: list[EvidenceChunk],
    report: CorpusBuildReport,
    *,
    built_at: datetime,
) -> Path:
    """Write the corpus, the manifest and the build report, then verify them."""
    import json  # Local import: only this function serialises the report

    output_dir = config.output_dir
    if (
        output_dir.exists() and any(output_dir.iterdir()) and not config.force
    ):  # Refuse to clobber an existing corpus
        from synapse.errors import SynapseArtifactError

        raise SynapseArtifactError(
            problem="output directory is not empty; pass --force to overwrite", path=output_dir
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    chunks_path = output_dir / CHUNKS_FILENAME
    documents_path = output_dir / DOCUMENTS_FILENAME
    write_jsonl(chunks_path, chunks)  # Safe artifact format: JSONL, never pickle
    write_jsonl(documents_path, documents)

    manifest = build_index_manifest(
        index_id=f"{config.corpus_version}-{config.index_version}",
        index_version=config.index_version,
        corpus_version=config.corpus_version,
        source_pack_version=config.source_pack_version or config.corpus_version,
        chunks=chunks,
        artifacts=[
            make_artifact(chunks_path, "chunks", relative_to=output_dir),
            make_artifact(documents_path, "documents", relative_to=output_dir),
        ],
        embedding=EmbeddingConfig(
            provider=config.embedding_provider,
            model=config.embedding_model,
            dimensions=config.embedding_dimensions,
            l2_normalized=True,
        ),
        chunking=ChunkingConfig(
            algorithm=ChunkingAlgorithm.SECTION_AWARE_SENTENCE,  # Honest label for what actually produced these boundaries
            chunk_size=config.chunking.target_chars,
            overlap=0,  # This chunker packs whole sentences and does not overlap; recording 0 rather than a value it does not use
            min_chunk_chars=config.chunking.min_chars,
        ),
        distance_metric=DistanceMetric.L2,
        document_count=len(documents),
        faiss_path=None,  # No vectors are computed here, so the manifest declares 'rebuild_required'
        repository_root=Path.cwd(),
        built_at=built_at,
    )
    write_manifest(output_dir / MANIFEST_FILENAME, manifest)

    report_payload = report.model_dump(mode="json")
    (output_dir / REPORT_FILENAME).write_text(
        json.dumps(report_payload, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n",  # Sorted keys so two builds diff cleanly
        encoding="utf-8",
    )

    verify_artifacts(
        output_dir, deep=True
    )  # Verify what was just written, so a build cannot report success on unloadable artifacts
    logger.info(
        "corpus build written and verified",
        extra={"documents": len(documents), "chunks": len(chunks)},
    )
    return output_dir


__all__ = [
    "CHUNKS_FILENAME",
    "DOCUMENTS_FILENAME",
    "EXCLUDED_RETRACTION_STATES",
    "PROVENANCE_NOTES",
    "REPORT_FILENAME",
    "BuildConfig",
    "BuildOutcome",
    "build_corpus",
]

"""
synapse.ingest
==============
Evidence-grade PubMed ingestion.

Replaces the ingestion half of ``Data/fetch_and_chunk.py``, which captured only
the first ``<AbstractText>`` element, truncated titles at nested markup, and
recorded no publication date, journal, author, article type, language or
retraction status at all.

Layout:

    synapse.ingest.eutils    NCBI client: pacing, bounded retries, timeouts
    synapse.ingest.parse     PubMed XML -> SourceDocument
    synapse.ingest.evidence  publication types -> normalised evidence type
    synapse.ingest.dedupe    PMID -> DOI -> normalised title + year
    synapse.ingest.chunker   section-aware, sentence-preserving chunking
    synapse.ingest.pipeline  orchestration and artifact writing

Nothing here claims medical approval. Automatic retrieval establishes that a
publication exists; every document is written ``approval_status='unreviewed'``.
"""

from __future__ import annotations  # Postponed annotations

from synapse.ingest.chunker import ChunkingPolicy, chunk_document, chunk_documents, split_sentences
from synapse.ingest.dedupe import DedupeResult, DuplicateDecision, deduplicate, normalize_title
from synapse.ingest.eutils import EUtilsClient, EUtilsConfig, IngestError
from synapse.ingest.evidence import classify_evidence_type
from synapse.ingest.parse import flatten_element_text, normalize_doi, parse_efetch_response
from synapse.ingest.pipeline import BuildConfig, BuildOutcome, build_corpus

__all__ = [
    "BuildConfig",
    "BuildOutcome",
    "ChunkingPolicy",
    "DedupeResult",
    "DuplicateDecision",
    "EUtilsClient",
    "EUtilsConfig",
    "IngestError",
    "build_corpus",
    "chunk_document",
    "chunk_documents",
    "classify_evidence_type",
    "deduplicate",
    "flatten_element_text",
    "normalize_doi",
    "normalize_title",
    "parse_efetch_response",
    "split_sentences",
]

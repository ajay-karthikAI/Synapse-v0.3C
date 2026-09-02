"""
Shared pytest fixtures.

Everything here is offline and deterministic: no network, no API key, no
FAISS. That is the point — the artifact-integrity layer must be verifiable in
a minimal environment, so CI can gate on it without a vector-search stack.
"""

from __future__ import annotations  # Postponed annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from synapse.normalize import normalize_text
from synapse.schemas.chunk import EvidenceChunk
from synapse.schemas.enums import DistanceMetric, SourceType
from synapse.schemas.index import ChunkingConfig, EmbeddingConfig

REPO_ROOT = (
    Path(__file__).resolve().parent.parent
)  # Repository root, derived from this file's location rather than from the working directory

FIXED_TIMESTAMP = datetime(
    2026, 1, 1, 12, 0, 0, tzinfo=UTC
)  # Fixed, timezone-aware instant so every test artifact hashes identically on every run


@pytest.fixture
def now() -> datetime:
    """A fixed timezone-aware timestamp, so hashes are reproducible across runs."""
    return FIXED_TIMESTAMP


@pytest.fixture
def make_chunk():  # Factory fixture: tests build exactly the chunks they need
    """Return a factory that builds a valid :class:`EvidenceChunk`."""

    def _make(
        document_id: str = "pubmed:41802233",
        ordinal: int = 0,
        text: str = "HbA1c reflects average plasma glucose over two to three months.",
        char_start: int = 0,
        char_end: int | None = None,
    ) -> EvidenceChunk:
        normalized = normalize_text(
            text
        )  # The model requires canonical text, so normalise in the factory
        return EvidenceChunk.build(
            document_id=document_id,
            ordinal=ordinal,
            text=normalized,
            char_start=char_start,
            char_end=char_end if char_end is not None else char_start + len(normalized),
            source_type=SourceType.PUBMED_ABSTRACT,
            ingested_at=FIXED_TIMESTAMP,
        )

    return _make


@pytest.fixture
def chunks(make_chunk) -> list[EvidenceChunk]:
    """A small, ordered corpus spanning two documents."""
    return [
        make_chunk(
            document_id="pubmed:41802233",
            ordinal=0,
            text="Metformin is a first-line oral therapy for type 2 diabetes.",
        ),
        make_chunk(
            document_id="pubmed:41802233",
            ordinal=1,
            text="It lowers HbA1c and has a favourable tolerability profile.",
        ),
        make_chunk(
            document_id="pubmed:41900001",
            ordinal=0,
            text="Hypertension is a leading contributor to cardiovascular disease.",
        ),
    ]


@pytest.fixture
def embedding_config() -> EmbeddingConfig:
    """Embedding configuration matching the corpus the application currently ships."""
    return EmbeddingConfig(
        provider="openai", model="text-embedding-3-small", dimensions=1536, l2_normalized=True
    )


@pytest.fixture
def chunking_config() -> ChunkingConfig:
    """Chunking configuration matching the legacy splitter settings."""
    return ChunkingConfig(
        algorithm="recursive_character", chunk_size=500, overlap=100, min_chunk_chars=0
    )


@pytest.fixture
def distance_metric() -> DistanceMetric:
    """The metric the current FAISS index uses."""
    return DistanceMetric.L2

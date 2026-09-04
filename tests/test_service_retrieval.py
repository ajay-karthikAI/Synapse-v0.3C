"""
The retrieval service: what reaches the bundle, and what must not.

Offline. The legacy index is a handful of plain objects, and the provider is a
fake that returns whatever the test wants -- so a failure here is about the
wiring rather than the environment.

The property under test is narrow and was, until this module existed, untested
at this level: **the relevance a patient sees comes from the reranker's verdict
or it does not exist.** ``RetrievalService`` is the only caller of
:func:`~synapse.retrieval.evidence.bundle_from_candidates` in the application,
so this is the seam where a fusion score could leak into a field the interface
renders as a judgement of how well a source matched the question.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from synapse.retrieval.production import stable_identifiers
from synapse.service.governance import EligibilityDecision
from synapse.service.index import LoadedIndex
from synapse.service.retrieval import RetrievalService

NOTHING_ENFORCED = EligibilityDecision(document_ids=None, reason="no pack")


@dataclass
class LegacyChunk:
    """The shape ``synapse.retrieval.production`` reads off a prototype chunk."""

    text: str
    pmid: str
    chunk_index: int = 0
    title: str = ""
    source_url: str = ""


@dataclass
class FakeComponent:
    """Stands in for both the legacy vector store and the BM25 index."""

    chunks: list[LegacyChunk]

    # The dense store takes an api_key positionally; the sparse index does not.
    def search(self, query: str, *args: Any, top_k: int = 10, **kwargs: Any) -> list[dict]:
        return [
            {"chunk": chunk, "score": 1.0 / (i + 1)} for i, chunk in enumerate(self.chunks[:top_k])
        ]


@dataclass
class FakeHybrid:
    vector_store: FakeComponent
    bm25_index: FakeComponent


@dataclass
class FakeIndex:
    """An :class:`~synapse.service.index.IndexProvider` over the fakes."""

    hybrid: FakeHybrid

    def load(self, report: Any = None) -> LoadedIndex:
        return LoadedIndex(chunks=self.hybrid.vector_store.chunks, hybrid=self.hybrid)


@dataclass
class FakeClientFactory:
    """Hands out one client for both the rewrite and the rerank."""

    client: Any

    def for_answer(self) -> Any:
        return self.client

    def for_rerank(self) -> Any:
        return self.client


class ScoringClient:
    """Returns a verdict for every chunk it is shown, at the given score."""

    def __init__(self, chunk_ids: list[str], score: float) -> None:
        self._chunk_ids = chunk_ids
        self._score = score
        self.calls = 0

    def generate_structured(self, *, system: str, user: str, schema: dict) -> str:
        self.calls += 1
        return json.dumps(
            {
                "verdicts": [
                    {"chunk_id": chunk_id, "relevance": self._score, "rank": rank}
                    for rank, chunk_id in enumerate(self._chunk_ids, start=1)
                ]
            }
        )


class FailingClient:
    """Every rerank attempt fails, so the stage degrades to the fused order."""

    def generate_structured(self, *, system: str, user: str, schema: dict) -> str:
        raise TimeoutError("provider did not answer")


def a_corpus() -> list[LegacyChunk]:
    """Four citable chunks, small enough not to trip the full-corpus guard."""
    return [
        LegacyChunk(
            text=f"finding number {i} about hba1c", pmid=str(30000000 + i), title=f"Paper {i}"
        )
        for i in range(4)
    ]


def chunk_ids_for(chunks: list[LegacyChunk]) -> list[str]:
    """The stable chunk identifiers the backends will derive for these chunks."""
    ids = []
    for chunk in chunks:
        identifiers = stable_identifiers(chunk)
        assert identifiers is not None, "fixture chunk must be citable"
        ids.append(identifiers[1])
    return ids


def service_for(chunks: list[LegacyChunk], client: Any) -> RetrievalService:
    component = FakeComponent(chunks)
    return RetrievalService(
        index=FakeIndex(FakeHybrid(vector_store=component, bm25_index=component)),
        clients=FakeClientFactory(client),
        api_key="not-a-real-key",
    )


class TestRelevanceProvenance:
    """Relevance is the reranker's judgement, or it is absent."""

    def test_relevance_is_the_reranker_verdict_on_its_own_scale(self) -> None:
        chunks = a_corpus()
        client = ScoringClient(chunk_ids_for(chunks), score=8.0)
        bundle = service_for(chunks, client).retrieve(
            "what is hba1c?", eligibility=NOTHING_ENFORCED
        )

        assert client.calls == 1  # ONE request for the whole candidate set
        assert bundle.relevance, "a successful rerank must produce a relevance"
        # 8.0 of a possible 10.0. Not a fused score, which for RRF over two
        # components cannot exceed ~0.033 whatever the corpus contained.
        assert set(bundle.relevance.values()) == {0.8}

    def test_a_degraded_rerank_produces_no_relevance_at_all(self) -> None:
        """The regression this module was written for.

        A fused score rendered as a match band labelled every source "Loosely
        matched the question" on every query. When nothing judged the passages,
        the honest interface shows no band -- not a number derived from rank
        agreement.
        """
        chunks = a_corpus()
        bundle = service_for(chunks, FailingClient()).retrieve(
            "what is hba1c?", eligibility=NOTHING_ENFORCED
        )

        # The turn still works: sources are retrieved, ordered and citable.
        assert bundle.source_order, "a degraded rerank must still return evidence"
        assert not bundle.is_empty
        # But nothing claims how well they matched.
        assert bundle.relevance == {}

    def test_every_reported_relevance_is_within_the_display_range(self) -> None:
        chunks = a_corpus()
        client = ScoringClient(chunk_ids_for(chunks), score=10.0)
        bundle = service_for(chunks, client).retrieve(
            "what is hba1c?", eligibility=NOTHING_ENFORCED
        )

        assert all(0.0 <= value <= 1.0 for value in bundle.relevance.values())


class TestBundleMetadata:
    """The trace records what happened without recording what was asked."""

    def test_no_query_text_reaches_the_flat_metadata(self) -> None:
        chunks = a_corpus()
        client = ScoringClient(chunk_ids_for(chunks), score=5.0)
        query = "a distinctive phrase that must not be logged"
        bundle = service_for(chunks, client).retrieve(query, eligibility=NOTHING_ENFORCED)

        # `answer_turn` spreads every NON-dict metadata value into its log line.
        flat = {k: v for k, v in bundle.metadata.items() if not isinstance(v, dict)}
        assert query not in json.dumps(flat)
        assert flat["query_rewritten"] is False  # No history, so no rewrite was attempted

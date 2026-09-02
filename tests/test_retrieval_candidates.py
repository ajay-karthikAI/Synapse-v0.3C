"""
Bounded candidate retrieval: union, deduplication, fusion, filtering, guards.

Every test is offline. The backends are the in-memory implementations from
:mod:`synapse.retrieval.backends`, so there is no FAISS, no BM25 library, no
embedding API and no fixture file — which means these run in CI unchanged and
a failure is always about the algorithm rather than the environment.
"""

from __future__ import annotations

import pytest

from synapse.retrieval.backends import (
    CorpusChunk,
    InMemoryDense,
    InMemorySparse,
    hashed_embedder,
)
from synapse.retrieval.candidates import Candidate, Hit, fuse, union_candidates
from synapse.retrieval.config import CandidateConfig, FusionStrategy
from synapse.retrieval.evidence import bundle_from_candidates
from synapse.retrieval.search import (
    FULL_CORPUS_GUARD_FLOOR,
    UnboundedRetrievalError,
    search_candidates,
)

TOPICS = ["hba1c glucose average", "blood pressure systolic", "metformin oral therapy"]


def a_corpus(size: int = 200) -> list[CorpusChunk]:
    """A deterministic corpus large enough to exercise the guard."""
    return [
        CorpusChunk(
            chunk_id=f"pubmed:{5_000_000 + index}#0000",
            document_id=f"pubmed:{5_000_000 + index}",
            text=f"{TOPICS[index % len(TOPICS)]} record {index}",
            title=f"Study {index}",
            url=f"https://pubmed.ncbi.nlm.nih.gov/{5_000_000 + index}/",
        )
        for index in range(size)
    ]


def backends(chunks: list[CorpusChunk]) -> tuple[InMemoryDense, InMemorySparse]:
    """Both components over one corpus."""
    embed = hashed_embedder()
    return InMemoryDense(chunks, [embed(c.text) for c in chunks], embed), InMemorySparse(chunks)


def a_hit(chunk_id: str, rank: int, score: float, document_id: str | None = None) -> Hit:
    """One component result."""
    return Hit(
        chunk_id=chunk_id,
        document_id=document_id or chunk_id.split("#", 1)[0],
        text=f"text for {chunk_id}",
        rank=rank,
        score=score,
    )


class TestUnionIsDeterministic:
    """Requirements 3, 4 and 10."""

    def test_union_merges_on_stable_chunk_id(self) -> None:
        dense = [a_hit("pubmed:1#0000", 1, 0.1), a_hit("pubmed:2#0000", 2, 0.2)]
        sparse = [a_hit("pubmed:2#0000", 1, 9.0), a_hit("pubmed:3#0000", 2, 5.0)]
        union = union_candidates(dense, sparse)
        assert [c.chunk_id for c in union] == [
            "pubmed:1#0000",
            "pubmed:2#0000",
            "pubmed:3#0000",
        ]
        shared = next(c for c in union if c.chunk_id == "pubmed:2#0000")
        assert shared.retrieved_by_both
        assert shared.dense_rank == 2
        assert shared.sparse_rank == 1

    def test_duplicate_chunks_are_removed(self) -> None:
        dense = [a_hit("pubmed:1#0000", 1, 0.1), a_hit("pubmed:1#0000", 5, 0.5)]
        union = union_candidates(dense, [])
        assert len(union) == 1
        # The better rank wins, so the result cannot depend on iteration order.
        assert union[0].dense_rank == 1

    def test_component_ranks_and_scores_are_preserved(self) -> None:
        # Requirement 7: fusion must not destroy the evidence behind it.
        union = union_candidates(
            [a_hit("pubmed:1#0000", 3, 0.42)], [a_hit("pubmed:1#0000", 7, 8.5)]
        )
        candidate = union[0]
        assert (candidate.dense_rank, candidate.dense_score) == (3, 0.42)
        assert (candidate.sparse_rank, candidate.sparse_score) == (7, 8.5)

    def test_fusion_is_deterministic_across_repeated_runs(self) -> None:
        chunks = a_corpus()
        dense, sparse = backends(chunks)
        config = CandidateConfig(dense_top_n=25, sparse_top_n=25)
        first = search_candidates("hba1c glucose", dense=dense, sparse=sparse, config=config)
        second = search_candidates("hba1c glucose", dense=dense, sparse=sparse, config=config)
        assert [c.chunk_id for c in first.candidates] == [c.chunk_id for c in second.candidates]
        assert [c.fused_score for c in first.candidates] == [
            c.fused_score for c in second.candidates
        ]

    def test_ties_break_on_chunk_id(self) -> None:
        # Two candidates at identical ranks in both components tie exactly under
        # RRF. Without a tie-break the order would depend on dict iteration.
        union = [
            Candidate(chunk_id="pubmed:9#0000", document_id="pubmed:9", text="b", dense_rank=1),
            Candidate(chunk_id="pubmed:1#0000", document_id="pubmed:1", text="a", dense_rank=1),
        ]
        fused = fuse(union, CandidateConfig(fusion=FusionStrategy.RRF))
        assert fused[0].fused_score == fused[1].fused_score
        assert [c.chunk_id for c in fused] == ["pubmed:1#0000", "pubmed:9#0000"]

    def test_fused_ranks_are_contiguous_and_one_based(self) -> None:
        chunks = a_corpus()
        dense, sparse = backends(chunks)
        result = search_candidates(
            "metformin therapy",
            dense=dense,
            sparse=sparse,
            config=CandidateConfig(dense_top_n=10, sparse_top_n=10, final_top_k=5),
        )
        assert [c.fused_rank for c in result.candidates] == [1, 2, 3, 4, 5]


class TestSingleComponentResultsRemainEligible:
    """A candidate found by only one component is still a candidate."""

    def test_dense_only_candidates_survive(self) -> None:
        fused = fuse(
            union_candidates([a_hit("pubmed:1#0000", 1, 0.1)], []),
            CandidateConfig(fusion=FusionStrategy.RRF),
        )
        assert [c.chunk_id for c in fused] == ["pubmed:1#0000"]
        assert fused[0].components == ("dense",)

    def test_sparse_only_candidates_survive(self) -> None:
        fused = fuse(
            union_candidates([], [a_hit("pubmed:2#0000", 1, 4.0)]),
            CandidateConfig(fusion=FusionStrategy.RRF),
        )
        assert [c.chunk_id for c in fused] == ["pubmed:2#0000"]
        assert fused[0].components == ("sparse",)

    def test_search_runs_with_only_a_dense_backend(self) -> None:
        chunks = a_corpus()
        dense, _sparse = backends(chunks)
        result = search_candidates(
            "hba1c", dense=dense, sparse=None, config=CandidateConfig(dense_top_n=10)
        )
        assert result.candidates
        assert result.trace.sparse_requested == 0

    def test_search_runs_with_only_a_sparse_backend(self) -> None:
        chunks = a_corpus()
        _dense, sparse = backends(chunks)
        result = search_candidates(
            "hba1c", dense=None, sparse=sparse, config=CandidateConfig(sparse_top_n=10)
        )
        assert result.candidates
        assert result.trace.dense_requested == 0

    def test_at_least_one_backend_is_required(self) -> None:
        with pytest.raises(ValueError, match="at least one retrieval backend"):
            search_candidates("q", dense=None, sparse=None)

    def test_a_component_agreed_on_by_both_outranks_a_single_component_hit(self) -> None:
        # The mechanism by which RRF rewards agreement, asserted rather than
        # assumed: same dense rank, but one candidate is also found by sparse.
        union = union_candidates(
            [a_hit("pubmed:1#0000", 1, 0.1), a_hit("pubmed:2#0000", 2, 0.2)],
            [a_hit("pubmed:2#0000", 1, 9.0)],
        )
        fused = fuse(union, CandidateConfig(fusion=FusionStrategy.RRF))
        assert fused[0].chunk_id == "pubmed:2#0000"


class TestNoFullCorpusRequests:
    """Requirement 6, enforced rather than documented."""

    def test_requesting_the_whole_corpus_is_refused(self) -> None:
        chunks = a_corpus(200)
        dense, sparse = backends(chunks)
        with pytest.raises(UnboundedRetrievalError):
            search_candidates(
                "q", dense=dense, sparse=sparse, config=CandidateConfig(dense_top_n=200)
            )

    def test_requesting_more_than_the_corpus_is_refused(self) -> None:
        chunks = a_corpus(200)
        dense, sparse = backends(chunks)
        with pytest.raises(UnboundedRetrievalError):
            search_candidates(
                "q", dense=dense, sparse=sparse, config=CandidateConfig(sparse_top_n=5000)
            )

    def test_the_backend_is_never_asked_for_more_than_configured(self) -> None:
        chunks = a_corpus(500)
        dense, sparse = backends(chunks)
        search_candidates(
            "hba1c glucose",
            dense=dense,
            sparse=sparse,
            config=CandidateConfig(dense_top_n=40, sparse_top_n=30),
        )
        assert dense.last_top_n == 40
        assert sparse.last_top_n == 30
        assert dense.last_top_n < dense.corpus_size
        assert sparse.last_top_n < sparse.corpus_size

    def test_a_tiny_corpus_is_exempt_from_the_guard(self) -> None:
        # A four-chunk fixture is legitimately exhausted by any sensible count;
        # the guard is about requests that scale with the corpus.
        chunks = a_corpus(4)
        dense, sparse = backends(chunks)
        result = search_candidates(
            "hba1c", dense=dense, sparse=sparse, config=CandidateConfig(dense_top_n=50)
        )
        assert result.trace.dense_requested == 4
        assert len(chunks) < FULL_CORPUS_GUARD_FLOOR

    def test_only_a_fraction_of_the_corpus_is_materialised(self) -> None:
        chunks = a_corpus(1000)
        dense, sparse = backends(chunks)
        result = search_candidates(
            "hba1c glucose average",
            dense=dense,
            sparse=sparse,
            config=CandidateConfig(dense_top_n=50, sparse_top_n=50),
        )
        # The legacy path was 1.0 on every query, by construction.
        assert result.trace.corpus_fraction_examined <= 0.1
        assert result.trace.union_size <= 100


class TestEligibilityFiltering:
    """Requirement 8: ineligible sources never reach generation."""

    def test_ineligible_documents_are_removed(self) -> None:
        chunks = a_corpus(200)
        dense, sparse = backends(chunks)
        eligible = frozenset(c.document_id for c in chunks[:20])
        result = search_candidates(
            "hba1c glucose",
            dense=dense,
            sparse=sparse,
            config=CandidateConfig(dense_top_n=50, sparse_top_n=50),
            eligible_documents=eligible,
        )
        assert result.candidates
        assert all(c.document_id in eligible for c in result.candidates)
        assert result.trace.filtered_ineligible > 0
        assert result.trace.eligibility_enforced is True

    def test_an_unenforced_run_is_recorded_as_such(self) -> None:
        # "Not filtered" must be visible in the trace, never inferred from the
        # absence of a field.
        chunks = a_corpus(200)
        dense, sparse = backends(chunks)
        result = search_candidates(
            "hba1c", dense=dense, sparse=sparse, config=CandidateConfig(dense_top_n=10)
        )
        assert result.trace.eligibility_enforced is False

    def test_filtering_everything_yields_no_candidates(self) -> None:
        chunks = a_corpus(200)
        dense, sparse = backends(chunks)
        result = search_candidates(
            "hba1c",
            dense=dense,
            sparse=sparse,
            config=CandidateConfig(dense_top_n=20, sparse_top_n=20),
            eligible_documents=frozenset(),
        )
        assert result.is_empty


class TestRunMetadata:
    """Requirement 9: the sizes travel with the result."""

    def test_candidate_sizes_are_recorded(self) -> None:
        chunks = a_corpus(200)
        dense, sparse = backends(chunks)
        config = CandidateConfig(dense_top_n=30, sparse_top_n=20, final_top_k=7)
        trace = search_candidates("hba1c", dense=dense, sparse=sparse, config=config).trace
        metadata = trace.as_metadata()
        assert metadata["config"]["dense_top_n"] == 30
        assert metadata["config"]["sparse_top_n"] == 20
        assert metadata["config"]["final_top_k"] == 7
        assert metadata["corpus_size"] == 200
        assert "latency_ms" in metadata

    def test_config_rejects_a_zero_candidate_configuration(self) -> None:
        with pytest.raises(ValueError, match="at least one retrieval component"):
            CandidateConfig(dense_top_n=0, sparse_top_n=0)


class TestEvidenceBundle:
    """The join to the answer layer preserves identifiers exactly."""

    def test_bundle_keeps_chunk_and_document_identifiers(self) -> None:
        chunks = a_corpus(200)
        dense, sparse = backends(chunks)
        result = search_candidates(
            "hba1c glucose", dense=dense, sparse=sparse, config=CandidateConfig(final_top_k=5)
        )
        bundle = bundle_from_candidates(result.candidates)
        assert set(bundle.evidence.chunk_texts) == {c.chunk_id for c in result.candidates}
        assert bundle.source_order == list(dict.fromkeys(c.document_id for c in result.candidates))
        assert bundle.evidence.source_ids == frozenset(bundle.source_order)

    def test_source_order_follows_rank(self) -> None:
        candidates = [
            Candidate("pubmed:7#0000", "pubmed:7", "a", fused_rank=1, fused_score=0.9),
            Candidate("pubmed:3#0000", "pubmed:3", "b", fused_rank=2, fused_score=0.5),
        ]
        assert bundle_from_candidates(candidates).source_order == ["pubmed:7", "pubmed:3"]

    def test_relevance_is_clamped_to_a_display_range(self) -> None:
        candidates = [Candidate("pubmed:7#0000", "pubmed:7", "a", fused_score=999.0)]
        assert bundle_from_candidates(candidates).relevance["pubmed:7"] == 1.0

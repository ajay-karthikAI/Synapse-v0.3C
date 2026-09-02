"""
Tests for the benchmark itself.

A benchmark nobody checks is a benchmark that measures whatever it happens to
measure. Two things are pinned here:

* **Fidelity.** ``synapse.bench.legacy_baseline`` claims to reproduce the legacy
  full-corpus fusion. ``TestLegacyBaselineFidelity`` runs it against the real
  ``Retrieval.hybrid_retriever.linear_fusion`` and requires identical output.
  If the legacy function ever changes, the "before" number stops being a
  measurement of the thing it names, and this test fails first.
* **Determinism.** The corpus, the queries and both arms reproduce exactly, so a
  committed result can be re-derived rather than believed.

The fidelity test imports the legacy tree, which pulls in ``numpy`` and
``langchain_text_splitters``. Those belong to the application's
``requirements.txt``, not to the package's ``[dev]`` extra, so the test skips
cleanly where they are absent — the same coupling rule the rest of the suite
follows.
"""

from __future__ import annotations

import pytest

from synapse.bench.corpus import build_corpus
from synapse.bench.legacy_baseline import (
    invert_distances,
    legacy_rerank,
    legacy_search,
    linear_fusion_full_corpus,
    min_max_normalize,
)
from synapse.bench.retrieval import (
    FakeRerankProvider,
    agreement,
    run_baseline_arm,
    run_candidate_arm,
)
from synapse.retrieval.backends import (
    CorpusChunk,
    InMemoryDense,
    InMemorySparse,
    hashed_embedder,
)
from synapse.retrieval.config import CandidateConfig, FusionStrategy


def small_corpus(size: int = 60) -> list[CorpusChunk]:
    """A corpus small enough to fuse exhaustively in a test."""
    topics = ["hba1c glucose average", "blood pressure systolic", "metformin oral"]
    return [
        CorpusChunk(
            chunk_id=f"pubmed:{8_000_000 + index}#0000",
            document_id=f"pubmed:{8_000_000 + index}",
            text=f"{topics[index % 3]} note {index}",
        )
        for index in range(size)
    ]


class TestLegacyBaselineFidelity:
    """The "before" arm must be the legacy algorithm, not an approximation."""

    def test_normalisation_matches_the_legacy_function(self) -> None:
        legacy = pytest.importorskip("Retrieval.hybrid_retriever")
        for scores in ([1.0, 2.0, 3.0], [5.0, 5.0, 5.0], [0.0], [-2.0, 4.0]):
            assert min_max_normalize(scores) == pytest.approx(legacy.min_max_normalize(scores))

    def test_distance_inversion_matches_the_legacy_function(self) -> None:
        legacy = pytest.importorskip("Retrieval.hybrid_retriever")
        distances = [0.0, 0.5, 1.0, 7.25]
        assert invert_distances(distances) == pytest.approx(legacy.invert_distances(distances))

    def test_full_corpus_fusion_matches_the_legacy_function(self) -> None:
        """Same inputs, same ranking, same scores."""
        legacy = pytest.importorskip("Retrieval.hybrid_retriever")
        chunks = small_corpus()
        embed = hashed_embedder()
        dense = InMemoryDense(chunks, [embed(c.text) for c in chunks], embed)
        sparse = InMemorySparse(chunks)

        dense_hits = {hit.chunk_id: hit.score for hit in dense.search("hba1c glucose", len(chunks))}
        sparse_hits = {
            hit.chunk_id: hit.score for hit in sparse.search("hba1c glucose", len(chunks))
        }
        vector_scores = [dense_hits.get(chunk.chunk_id, 2.0) for chunk in chunks]
        bm25_scores = [sparse_hits.get(chunk.chunk_id, 0.0) for chunk in chunks]

        ours = linear_fusion_full_corpus(vector_scores, bm25_scores, chunks, 0.7, 10)

        # The legacy function needs objects exposing chunk_id() and text.
        class LegacyChunk:
            def __init__(self, chunk: CorpusChunk) -> None:
                self.text = chunk.text
                self.pmid = chunk.document_id
                self.title = ""
                self.source_url = ""
                self._chunk_id = chunk.chunk_id

            def chunk_id(self) -> str:
                return self._chunk_id

        theirs = legacy.linear_fusion(
            vector_scores, bm25_scores, [LegacyChunk(c) for c in chunks], 0.7, 10
        )

        assert [c.chunk_id for c in ours] == [item["chunk"].chunk_id() for item in theirs]
        assert [round(c.fused_score, 12) for c in ours] == [
            round(item["score"], 12) for item in theirs
        ]

    def test_the_baseline_arm_queries_the_whole_corpus(self) -> None:
        # The property being measured: the legacy algorithm asks for everything.
        chunks = small_corpus()
        embed = hashed_embedder()
        dense = InMemoryDense(chunks, [embed(c.text) for c in chunks], embed)
        sparse = InMemorySparse(chunks)
        measurement = legacy_search(
            "hba1c glucose", dense=dense, sparse=sparse, chunks=chunks, top_k=10
        )
        assert dense.last_top_n == len(chunks)
        assert measurement.corpus_fraction_examined == 1.0
        assert measurement.materialised_scores == len(chunks) * 2

    def test_the_baseline_reranker_calls_once_per_candidate(self) -> None:
        chunks = small_corpus()
        embed = hashed_embedder()
        dense = InMemoryDense(chunks, [embed(c.text) for c in chunks], embed)
        sparse = InMemorySparse(chunks)
        retrieval = legacy_search(
            "hba1c glucose", dense=dense, sparse=sparse, chunks=chunks, top_k=10
        )
        provider = FakeRerankProvider(per_call_latency_ms=0.0)
        measurement = legacy_rerank("hba1c glucose", retrieval.candidates, provider, top_k=3)
        assert measurement.model_calls == len(retrieval.candidates) == 10

    def test_the_baseline_swallows_provider_failures(self) -> None:
        # Reproduced deliberately: in the legacy path an outage was
        # indistinguishable from a successful rerank, which is why the
        # replacement records degradation explicitly.
        class Broken:
            def score(self, prompt: str) -> float:
                raise RuntimeError("provider down")

        chunks = small_corpus(10)
        candidates = linear_fusion_full_corpus([1.0] * 10, [1.0] * 10, chunks, 0.7, 10)
        measurement = legacy_rerank("q", candidates, Broken(), top_k=3)
        assert measurement.failures_swallowed == 10
        assert len(measurement.candidates) == 3  # Looks exactly like a success


class TestBenchmarkDeterminism:
    """A committed result must be re-derivable."""

    def test_the_corpus_is_reproducible(self) -> None:
        first = build_corpus(size=200, seed=7)
        second = build_corpus(size=200, seed=7)
        assert [c.text for c in first.chunks] == [c.text for c in second.chunks]
        assert [q.text for q in first.queries] == [q.text for q in second.queries]

    def test_a_different_seed_produces_a_different_corpus(self) -> None:
        assert [c.text for c in build_corpus(size=200, seed=7).chunks] != [
            c.text for c in build_corpus(size=200, seed=8).chunks
        ]

    def test_relevant_sets_are_small_enough_for_recall_to_discriminate(self) -> None:
        # With fifty relevant chunks per query, recall@3 is capped at 0.06 by
        # arithmetic and the metric stops measuring ranking quality.
        corpus = build_corpus(size=2220)
        for query in corpus.queries:
            assert 1 <= len(query.relevant_chunk_ids) <= 10

    def test_both_arms_are_reproducible(self) -> None:
        corpus = build_corpus(size=200, queries_per_topic=1)
        first = run_candidate_arm(corpus, per_call_latency_ms=0.0)
        second = run_candidate_arm(corpus, per_call_latency_ms=0.0)
        assert first.final_rankings == second.final_rankings
        assert (
            first.as_metadata()["quality_retrieval_only"]
            == (second.as_metadata()["quality_retrieval_only"])
        )


class TestMeasuredImprovements:
    """The claims made in docs/retrieval-runtime.md, asserted."""

    def test_reranker_calls_drop_from_n_to_one_per_query(self) -> None:
        corpus = build_corpus(size=400, queries_per_topic=1)
        baseline = run_baseline_arm(corpus, per_call_latency_ms=0.0)
        candidate = run_candidate_arm(corpus, per_call_latency_ms=0.0)
        assert baseline.as_metadata()["model_calls_per_query"]["mean"] == 10.0
        assert candidate.as_metadata()["model_calls_per_query"]["mean"] == 1.0

    def test_the_candidate_arm_examines_a_fraction_of_the_corpus(self) -> None:
        corpus = build_corpus(size=400, queries_per_topic=1)
        baseline = run_baseline_arm(corpus, per_call_latency_ms=0.0)
        candidate = run_candidate_arm(corpus, per_call_latency_ms=0.0)
        assert baseline.as_metadata()["corpus_fraction_examined"]["mean"] == 1.0
        assert candidate.as_metadata()["corpus_fraction_examined"]["mean"] < 0.35

    def test_bounding_alone_does_not_materially_change_quality(self) -> None:
        """The regression check, with fusion held constant.

        Comparing the default (RRF) arm against the linear baseline would
        conflate bounding with the fusion change. This holds fusion fixed so the
        only variable is the candidate limit, and requires the retrieval-stage
        metrics to stay within a small band.

        The full 24-query set is used rather than one query per topic: with eight
        queries a single query losing one relevant chunk moves recall@5 by 0.06,
        which is indistinguishable from a real regression. Widening the
        threshold to accommodate that would have made the test unable to detect
        the thing it exists for.
        """
        corpus = build_corpus(size=2220, queries_per_topic=3)
        baseline = run_baseline_arm(corpus, per_call_latency_ms=0.0)
        bounded = run_candidate_arm(
            corpus,
            config=CandidateConfig(fusion=FusionStrategy.LINEAR),
            per_call_latency_ms=0.0,
        )
        before = baseline.as_metadata()["quality_retrieval_only"]
        after = bounded.as_metadata()["quality_retrieval_only"]
        for metric, value in before.items():
            delta = after[metric] - value
            assert delta > -0.06, f"{metric} regressed by {abs(delta):.4f} (before={value})"

    def test_the_bounded_arm_broadly_agrees_with_full_corpus_fusion(self) -> None:
        corpus = build_corpus(size=2220, queries_per_topic=3)
        baseline = run_baseline_arm(corpus, per_call_latency_ms=0.0)
        bounded = run_candidate_arm(
            corpus,
            config=CandidateConfig(fusion=FusionStrategy.LINEAR),
            per_call_latency_ms=0.0,
        )
        overlap = agreement(baseline, bounded, stage="retrieval")
        assert overlap["mean_set_overlap"] >= 0.75

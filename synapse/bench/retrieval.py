"""
synapse.bench.retrieval
=======================
The before/after measurement itself.

Both arms run against the **same** corpus, the same backends and the same fake
provider, so the only difference between them is the algorithm. Everything is
offline and deterministic: the embedder is a stable hash, the reranker is a fake
whose latency is a fixed constant, and the corpus is generated from a seed.

What is measured, and how honest each number is:

* **corpus size, candidate count, model calls** — exact counts.
* **retrieval latency** — real wall-clock for the retrieval code, measured over
  repeated trials on this machine. It is a *relative* measure: the absolute
  milliseconds depend on the host, so the ratio between arms is the number worth
  reading, not the value.
* **rerank latency** — the fake provider sleeps a fixed ``per_call_latency_ms``
  per request, so this is a **model** of provider latency, not a measurement of
  one. It exists to show the shape of the change (N sequential calls versus one),
  and the per-call constant is recorded alongside the result.
* **token usage** — estimated at four characters per token, and labelled
  ``*_estimated`` everywhere it appears. A fake provider reports no usage.
* **Recall@k / nDCG@k** — exact, over the generated labels, using the P1 metric
  implementations in :mod:`synapse.evals.metrics.retrieval` rather than a second
  copy.
* **unsupported-claim rate** — measured by the answer layer, not modelled here:
  see :func:`measure_unsupported_claim_rate`.
"""

from __future__ import annotations  # Postponed annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from statistics import mean, median

from synapse.bench.corpus import BenchmarkCorpus, BenchmarkQuery
from synapse.bench.legacy_baseline import legacy_rerank, legacy_search
from synapse.evals.metrics.retrieval import ndcg_at_k, recall_at_k
from synapse.retrieval.backends import InMemoryDense, InMemorySparse, hashed_embedder
from synapse.retrieval.config import CandidateConfig, RerankConfig
from synapse.retrieval.rerank import rerank_candidates
from synapse.retrieval.search import search_candidates

MEASURED_AT_K = (3, 5, 10)


class FakeRerankProvider:
    """A deterministic stand-in for the reranking provider.

    Scores by lexical overlap with the query, so its ranking is sensible enough
    that a rerank is not pure noise, and identical on every run. Each call
    sleeps ``per_call_latency_ms`` so the difference between one request and N
    sequential requests is visible in wall-clock terms.
    """

    def __init__(self, per_call_latency_ms: float = 40.0) -> None:
        self.per_call_latency_ms = per_call_latency_ms
        self.calls = 0
        self.prompt_chars = 0

    def _sleep(self) -> None:
        time.sleep(self.per_call_latency_ms / 1000.0)

    # -- batched interface, matching synapse.retrieval.rerank.RerankClient ----
    def generate_structured(self, *, system: str, user: str, schema: dict) -> str:
        """Score every passage in one request."""
        self.calls += 1
        self.prompt_chars += len(system) + len(user)
        self._sleep()
        question = user.split("QUESTION:", 1)[-1].split("\n", 1)[0].strip().lower().split()
        verdicts: list[tuple[str, int]] = []
        for block in user.split("chunk_id: ")[1:]:
            chunk_id, _, rest = block.partition("\n")
            passage = rest.replace("passage:", " ").lower()
            overlap = sum(1 for term in question if term in passage)
            verdicts.append((chunk_id.strip(), overlap))
        verdicts.sort(key=lambda item: (-item[1], item[0]))
        return json.dumps(
            {
                "verdicts": [
                    {
                        "chunk_id": chunk_id,
                        "relevance": min(10.0, float(overlap) * 2.0),
                        "rank": rank,
                        "rationale": "lexical overlap with the question",
                    }
                    for rank, (chunk_id, overlap) in enumerate(verdicts, start=1)
                ]
            }
        )

    # -- per-passage interface, matching the legacy loop ---------------------
    def score(self, prompt: str) -> float:
        """Score one passage; one call per candidate, as the legacy path did."""
        self.calls += 1
        self.prompt_chars += len(prompt)
        self._sleep()
        question = prompt.split("Question:", 1)[-1].split("\n", 1)[0].strip().lower().split()
        passage = prompt.split("Passage:", 1)[-1].lower()
        return min(10.0, float(sum(1 for term in question if term in passage)) * 2.0)


@dataclass
class ArmResult:
    """Aggregated measurements for one arm of the comparison."""

    name: str
    corpus_size: int
    queries: int
    candidate_counts: list[int] = field(default_factory=list)
    retrieval_latency_ms: list[float] = field(default_factory=list)
    rerank_latency_ms: list[float] = field(default_factory=list)
    model_calls: list[int] = field(default_factory=list)
    prompt_tokens_estimated: list[int] = field(default_factory=list)
    corpus_fraction_examined: list[float] = field(default_factory=list)
    recall: dict[int, list[float]] = field(default_factory=dict)
    ndcg: dict[int, list[float]] = field(default_factory=dict)
    final_rankings: dict[str, list[str]] = field(default_factory=dict)
    retrieval_rankings: dict[str, list[str]] = field(default_factory=dict)
    retrieval_recall: dict[int, list[float]] = field(default_factory=dict)
    retrieval_ndcg: dict[int, list[float]] = field(default_factory=dict)
    degraded_queries: int = 0
    config: dict[str, object] = field(default_factory=dict)

    def record_quality(
        self,
        query: BenchmarkQuery,
        retrieval_ranking: Sequence[str],
        final_ranking: Sequence[str],
    ) -> None:
        """Score both stages with the P1 retrieval metrics.

        Both, because they answer different questions. The **retrieval** stage
        isolates the effect of bounding the candidate set — the change this
        milestone is actually making. The **final** stage includes reranking,
        which reorders by design, so a difference there may be the reranker
        working rather than retrieval regressing. Reporting only the second
        would make the two indistinguishable.
        """
        relevance = query.relevance()
        for k in MEASURED_AT_K:
            for ranking, recall_store, ndcg_store in (
                (final_ranking, self.recall, self.ndcg),
                (retrieval_ranking, self.retrieval_recall, self.retrieval_ndcg),
            ):
                value = recall_at_k(list(ranking), relevance, k)
                if value is not None:
                    recall_store.setdefault(k, []).append(value)
                value = ndcg_at_k(list(ranking), relevance, k)
                if value is not None:
                    ndcg_store.setdefault(k, []).append(value)
        self.final_rankings[query.query_id] = list(final_ranking)
        self.retrieval_rankings[query.query_id] = list(retrieval_ranking)

    def as_metadata(self) -> dict[str, object]:
        """Machine-readable summary."""

        def summarise(values: Sequence[float]) -> dict[str, float]:
            if not values:
                return {"mean": 0.0, "median": 0.0, "p95": 0.0, "max": 0.0}
            ordered = sorted(values)
            index = min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))
            return {
                "mean": round(mean(values), 4),
                "median": round(median(values), 4),
                "p95": round(ordered[index], 4),
                "max": round(max(values), 4),
            }

        return {
            "arm": self.name,
            "corpus_size": self.corpus_size,
            "queries": self.queries,
            "config": self.config,
            "candidates_per_query": summarise([float(c) for c in self.candidate_counts]),
            "corpus_fraction_examined": summarise(self.corpus_fraction_examined),
            "retrieval_latency_ms": summarise(self.retrieval_latency_ms),
            "rerank_latency_ms": summarise(self.rerank_latency_ms),
            "model_calls_per_query": summarise([float(c) for c in self.model_calls]),
            "model_calls_total": sum(self.model_calls),
            "prompt_tokens_estimated_total": sum(self.prompt_tokens_estimated),
            "degraded_queries": self.degraded_queries,
            "quality_after_rerank": {
                f"recall_at_{k}": round(mean(values), 4)
                for k, values in sorted(self.recall.items())
                if values
            }
            | {
                f"ndcg_at_{k}": round(mean(values), 4)
                for k, values in sorted(self.ndcg.items())
                if values
            },
            "quality_retrieval_only": {
                f"recall_at_{k}": round(mean(values), 4)
                for k, values in sorted(self.retrieval_recall.items())
                if values
            }
            | {
                f"ndcg_at_{k}": round(mean(values), 4)
                for k, values in sorted(self.retrieval_ndcg.items())
                if values
            },
        }


def _backends(corpus: BenchmarkCorpus) -> tuple[InMemoryDense, InMemorySparse]:
    """Build both backends over the benchmark corpus, once."""
    embed = hashed_embedder()
    vectors = [embed(chunk.text) for chunk in corpus.chunks]  # type: ignore[operator]
    return InMemoryDense(corpus.chunks, vectors, embed), InMemorySparse(corpus.chunks)


def run_baseline_arm(
    corpus: BenchmarkCorpus,
    *,
    alpha: float = 0.7,
    retrieval_top_k: int = 10,
    rerank_top_k: int = 3,
    per_call_latency_ms: float = 40.0,
) -> ArmResult:
    """Measure the legacy algorithm: full-corpus fusion, one call per candidate."""
    dense, sparse = _backends(corpus)
    provider = FakeRerankProvider(per_call_latency_ms)
    result = ArmResult(
        name="baseline_full_corpus_sequential_rerank",
        corpus_size=corpus.size,
        queries=len(corpus.queries),
        config={
            "fusion": "linear_full_corpus",
            "alpha": alpha,
            "retrieval_top_k": retrieval_top_k,
            "rerank_top_k": rerank_top_k,
            "rerank_calls_per_query": "one per candidate",
            "provider_per_call_latency_ms": per_call_latency_ms,
        },
    )
    for query in corpus.queries:
        retrieval = legacy_search(
            query.text,
            dense=dense,
            sparse=sparse,
            chunks=corpus.chunks,
            alpha=alpha,
            top_k=retrieval_top_k,
        )
        before_calls = provider.calls
        rerank = legacy_rerank(query.text, retrieval.candidates, provider, top_k=rerank_top_k)
        result.candidate_counts.append(len(retrieval.candidates))
        result.corpus_fraction_examined.append(retrieval.corpus_fraction_examined)
        result.retrieval_latency_ms.append(retrieval.latency_ms)
        result.rerank_latency_ms.append(rerank.latency_ms)
        result.model_calls.append(provider.calls - before_calls)
        result.prompt_tokens_estimated.append(rerank.prompt_tokens_estimated)
        result.record_quality(
            query,
            [c.chunk_id for c in retrieval.candidates],
            [c.chunk_id for c in rerank.candidates],
        )
    return result


def run_candidate_arm(
    corpus: BenchmarkCorpus,
    *,
    config: CandidateConfig | None = None,
    rerank_config: RerankConfig | None = None,
    per_call_latency_ms: float = 40.0,
) -> ArmResult:
    """Measure the bounded candidate-union pipeline with batched reranking."""
    config = config or CandidateConfig()
    rerank_config = rerank_config or RerankConfig()
    dense, sparse = _backends(corpus)
    provider = FakeRerankProvider(per_call_latency_ms)
    result = ArmResult(
        name="candidate_union_batched_rerank",
        corpus_size=corpus.size,
        queries=len(corpus.queries),
        config=dict(config.as_metadata())
        | {"rerank": rerank_config.as_metadata()}
        | {"provider_per_call_latency_ms": per_call_latency_ms},
    )
    for query in corpus.queries:
        retrieval = search_candidates(query.text, dense=dense, sparse=sparse, config=config)
        before_calls = provider.calls
        outcome = rerank_candidates(
            query.text, retrieval.candidates, provider, rerank_config, sleep=lambda _d: None
        )
        result.candidate_counts.append(retrieval.trace.union_size)
        result.corpus_fraction_examined.append(retrieval.trace.corpus_fraction_examined)
        result.retrieval_latency_ms.append(retrieval.trace.total_latency_ms)
        result.rerank_latency_ms.append(outcome.latency_ms)
        result.model_calls.append(provider.calls - before_calls)
        result.prompt_tokens_estimated.append(outcome.prompt_tokens_estimated)
        result.degraded_queries += 1 if outcome.degraded else 0
        result.record_quality(
            query,
            [c.chunk_id for c in retrieval.candidates],
            [c.chunk_id for c in outcome.candidates],
        )
    return result


def agreement(
    baseline: ArmResult, candidate: ArmResult, *, stage: str = "final"
) -> dict[str, object]:
    """How far the two arms' final rankings agree, per query.

    Label-free and therefore the strongest evidence available that bounding the
    candidate set did not change what the patient sees: for each query, the
    overlap between the two arms' final chunk sets and whether the top-ranked
    chunk is the same.
    """
    left = baseline.final_rankings if stage == "final" else baseline.retrieval_rankings
    right = candidate.final_rankings if stage == "final" else candidate.retrieval_rankings
    overlaps: list[float] = []
    same_top: list[bool] = []
    for query_id, baseline_ranking in sorted(left.items()):
        candidate_ranking = right.get(query_id, [])
        if not baseline_ranking:
            continue
        shared = set(baseline_ranking) & set(candidate_ranking)
        overlaps.append(len(shared) / len(set(baseline_ranking)))
        same_top.append(bool(candidate_ranking) and candidate_ranking[0] == baseline_ranking[0])
    return {
        "stage": stage,
        "queries_compared": len(overlaps),
        "mean_set_overlap": round(mean(overlaps), 4) if overlaps else 0.0,
        "queries_with_identical_top_result": sum(same_top),
        "queries_with_full_overlap": sum(1 for value in overlaps if value >= 1.0),
    }


__all__ = [
    "MEASURED_AT_K",
    "ArmResult",
    "FakeRerankProvider",
    "agreement",
    "run_baseline_arm",
    "run_candidate_arm",
]

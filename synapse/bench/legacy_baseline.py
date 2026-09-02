"""
synapse.bench.legacy_baseline
=============================
The pre-optimisation algorithm, re-implemented for measurement.

**This is a measurement instrument, not a code path.** Nothing in the
application imports it. It exists so the "before" half of the benchmark runs the
legacy algorithm against the same corpus, the same backends and the same fake
provider as the "after" half — which is the only way the comparison isolates the
algorithm rather than the environment.

Why re-implement rather than import ``Retrieval/hybrid_retriever.py``:

* that module imports ``faiss`` and ``openai`` at module scope and reaches the
  network through a client it constructs internally, so it cannot be driven
  offline;
* importing it here would make the typed package depend on the untyped legacy
  tree, which is the coupling the package exists to remove.

Fidelity is not asserted, it is **tested**:
``tests/test_retrieval_benchmark.py::TestLegacyBaselineFidelity`` runs this
implementation and the real ``Retrieval.hybrid_retriever.linear_fusion`` over
the same inputs and requires identical output. If the legacy function changes,
that test fails and this file is wrong until it is updated.

What is reproduced, exactly:

1. dense search with ``top_k=len(corpus)`` — the full-corpus FAISS query;
2. a score for **every** chunk from the sparse index;
3. min-max normalisation over the **whole corpus**, then a weighted sum;
4. reconstruction of the dense score array through a ``{chunk_id: score}`` map,
   including the ``2.0`` default for chunks the map does not contain;
5. one reranking request **per candidate**, sequentially.
"""

from __future__ import annotations  # Postponed annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass

from synapse.retrieval.backends import CorpusChunk, DenseBackend, SparseBackend
from synapse.retrieval.candidates import Candidate

MISSING_DISTANCE_DEFAULT = 2.0  # The legacy default for a chunk absent from the score map


def min_max_normalize(scores: Sequence[float]) -> list[float]:
    """Scale to [0, 1]; a constant array becomes all 0.5.

    Byte-for-byte the legacy behaviour, including the 1e-10 equality threshold.
    """
    if not scores:
        return []
    lowest = min(scores)
    highest = max(scores)
    if highest - lowest < 1e-10:
        return [0.5] * len(scores)
    return [(score - lowest) / (highest - lowest) for score in scores]


def invert_distances(distances: Sequence[float]) -> list[float]:
    """``1 / (1 + d)`` — the legacy distance-to-similarity conversion."""
    return [1.0 / (1.0 + distance) for distance in distances]


def linear_fusion_full_corpus(
    vector_scores_all: Sequence[float],
    bm25_scores_all: Sequence[float],
    chunks: Sequence[CorpusChunk],
    alpha: float,
    top_k: int,
) -> list[Candidate]:
    """The legacy weighted linear fusion, over every chunk in the corpus."""
    vector_similarity = invert_distances(vector_scores_all)
    normalised_vector = min_max_normalize(vector_similarity)
    normalised_bm25 = min_max_normalize(bm25_scores_all)
    combined = [
        alpha * vector + (1.0 - alpha) * bm25
        for vector, bm25 in zip(normalised_vector, normalised_bm25, strict=True)
    ]
    top_indices = sorted(range(len(combined)), key=lambda i: combined[i], reverse=True)[:top_k]
    return [
        Candidate(
            chunk_id=chunks[index].chunk_id,
            document_id=chunks[index].document_id,
            text=chunks[index].text,
            title=chunks[index].title,
            url=chunks[index].url,
            dense_score=normalised_vector[index],
            sparse_score=normalised_bm25[index],
            fused_score=combined[index],
            fused_rank=rank,
        )
        for rank, index in enumerate(top_indices, start=1)
    ]


@dataclass(frozen=True)
class LegacyRetrievalMeasurement:
    """What the legacy retrieval stage cost."""

    candidates: list[Candidate]
    corpus_size: int
    dense_requested: int  # len(corpus), by construction
    materialised_scores: int  # Score values built per query
    latency_ms: float

    @property
    def corpus_fraction_examined(self) -> float:
        """Always 1.0 for this algorithm; recorded so the comparison is explicit."""
        return 1.0


def legacy_search(
    query: str,
    *,
    dense: DenseBackend,
    sparse: SparseBackend,
    chunks: Sequence[CorpusChunk],
    alpha: float = 0.7,
    top_k: int = 10,
) -> LegacyRetrievalMeasurement:
    """Run the legacy full-corpus fusion and measure it."""
    started = time.perf_counter()
    corpus_size = len(chunks)

    # 1. The full-corpus dense query.
    dense_hits = dense.search(query, corpus_size)

    # 2. Rebuild an array aligned to the corpus through a chunk_id map, with the
    #    legacy default for anything missing.
    score_map = {hit.chunk_id: hit.score for hit in dense_hits}
    vector_scores_all = [
        score_map.get(chunk.chunk_id, MISSING_DISTANCE_DEFAULT) for chunk in chunks
    ]

    # 3. A sparse score for every chunk, whether or not it shares a term.
    sparse_hits = sparse.search(query, corpus_size)
    sparse_map = {hit.chunk_id: hit.score for hit in sparse_hits}
    bm25_scores_all = [sparse_map.get(chunk.chunk_id, 0.0) for chunk in chunks]

    candidates = linear_fusion_full_corpus(vector_scores_all, bm25_scores_all, chunks, alpha, top_k)
    elapsed = (time.perf_counter() - started) * 1000
    return LegacyRetrievalMeasurement(
        candidates=candidates,
        corpus_size=corpus_size,
        dense_requested=corpus_size,
        materialised_scores=corpus_size * 2,  # One dense and one sparse score per chunk
        latency_ms=elapsed,
    )


@dataclass(frozen=True)
class LegacyRerankMeasurement:
    """What the legacy per-passage reranking cost."""

    candidates: list[Candidate]
    model_calls: int
    prompt_tokens_estimated: int
    latency_ms: float
    failures_swallowed: int


LEGACY_RERANK_PROMPT = """\
You are evaluating how useful a medical text passage is for answering a patient's question.
Question: {query}

Passage: {passage}

Respond with ONLY a JSON object in this exact format:
{{"score": <number 0-10>, "reason": "<one sentence>"}}
"""


def legacy_rerank(
    query: str,
    candidates: Sequence[Candidate],
    client: object,
    *,
    top_k: int = 3,
) -> LegacyRerankMeasurement:
    """One request per candidate, sequentially, swallowing every failure.

    The ``except Exception`` is reproduced deliberately: it is a measured
    property of the baseline that a provider failure produced a plausible-looking
    result rather than a signal.
    """
    started = time.perf_counter()
    scored: list[tuple[float, Candidate]] = []
    calls = 0
    tokens = 0
    swallowed = 0

    for candidate in candidates:
        prompt = LEGACY_RERANK_PROMPT.format(query=query, passage=candidate.text)
        tokens += max(1, len(prompt) // 4)
        calls += 1
        try:
            raw = client.score(prompt)  # type: ignore[attr-defined]
            score = float(raw)
        except Exception:
            score = candidate.fused_score * 10.0
            swallowed += 1
        scored.append((score, candidate))

    scored.sort(key=lambda item: item[0], reverse=True)
    elapsed = (time.perf_counter() - started) * 1000
    from dataclasses import replace

    return LegacyRerankMeasurement(
        candidates=[
            replace(candidate, fused_rank=rank)
            for rank, (_score, candidate) in enumerate(scored[:top_k], start=1)
        ],
        model_calls=calls,
        prompt_tokens_estimated=tokens,
        latency_ms=elapsed,
        failures_swallowed=swallowed,
    )


__all__ = [
    "MISSING_DISTANCE_DEFAULT",
    "LegacyRerankMeasurement",
    "LegacyRetrievalMeasurement",
    "invert_distances",
    "legacy_rerank",
    "legacy_search",
    "linear_fusion_full_corpus",
    "min_max_normalize",
]

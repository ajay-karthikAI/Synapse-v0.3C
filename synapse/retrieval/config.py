"""
synapse.retrieval.config
========================
Candidate sizes, fusion parameters and reranker limits — in one typed place.

Every value here ends up in the run metadata of whatever consumed it
(:class:`~synapse.retrieval.search.RetrievalTrace`), because a retrieval result
is not interpretable without the sizes that produced it. "Recall@5 was 0.82" is
meaningless if nobody recorded whether the union was built from 20 candidates or
200.

Why the defaults are what they are is measured, not asserted: see
docs/retrieval-runtime.md §4 and the committed sweep in
``artifacts/benchmarks/``. In short, ``dense_top_n = sparse_top_n = 50`` on a
2,220-chunk corpus reproduces the full-corpus fusion top-10 exactly on the
benchmark query set, while touching ~4.5% of the corpus.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass
from enum import StrEnum


class FusionStrategy(StrEnum):
    """How the dense and sparse candidate lists are combined."""

    LINEAR = "linear"  # Weighted sum of min-max normalised scores; tunable, score-sensitive
    RRF = "rrf"  # Reciprocal rank fusion; rank-only, robust across score distributions


@dataclass(frozen=True)
class CandidateConfig:
    """Bounded candidate retrieval and fusion.

    ``dense_top_n`` and ``sparse_top_n`` are the whole point of this milestone:
    the previous online path asked FAISS for ``top_k=len(corpus)`` and BM25 for
    a score per chunk, then fused across the entire corpus on every query.
    """

    dense_top_n: int = 50  # Candidates requested from the dense index
    sparse_top_n: int = 50  # Candidates requested from the sparse index
    fusion: FusionStrategy = FusionStrategy.RRF
    alpha: float = 0.7  # LINEAR only: weight on the dense component
    rrf_k: int = 60  # RRF only: the standard Cormack et al. (2009) constant
    final_top_k: int = 10  # Candidates handed to the reranker
    missing_score_floor: float = (
        0.0  # Score assigned to a candidate absent from one component's list, after normalisation
    )

    def __post_init__(self) -> None:
        """Reject configurations that cannot produce a usable candidate set."""
        if self.dense_top_n < 0 or self.sparse_top_n < 0:
            raise ValueError("candidate counts must be non-negative")
        if self.dense_top_n == 0 and self.sparse_top_n == 0:
            raise ValueError("at least one retrieval component must be queried")
        if self.final_top_k < 1:
            raise ValueError("final_top_k must be at least 1")
        if not 0.0 <= self.alpha <= 1.0:
            raise ValueError("alpha must be within [0, 1]")
        if self.rrf_k < 1:
            raise ValueError("rrf_k must be at least 1")

    @property
    def max_candidates(self) -> int:
        """Upper bound on the union size, before deduplication.

        The real union is usually smaller, because the two components overlap —
        which is the property that makes bounded retrieval work.
        """
        return self.dense_top_n + self.sparse_top_n

    def as_metadata(self) -> dict[str, object]:
        """Machine-readable form, recorded with every run."""
        return {
            "dense_top_n": self.dense_top_n,
            "sparse_top_n": self.sparse_top_n,
            "fusion": self.fusion.value,
            "alpha": self.alpha if self.fusion is FusionStrategy.LINEAR else None,
            "rrf_k": self.rrf_k if self.fusion is FusionStrategy.RRF else None,
            "final_top_k": self.final_top_k,
        }


@dataclass(frozen=True)
class RerankConfig:
    """One bounded structured reranking request, and what may happen when it fails.

    The previous implementation issued **one LLM request per candidate**, in a
    loop, with no timeout, no retry policy and a bare ``except Exception`` that
    silently substituted the retrieval score. Ten candidates meant ten
    sequential round trips on the patient's critical path, and a provider
    outage looked exactly like a successful rerank.
    """

    enabled: bool = True
    model: str = "gpt-4o-mini"
    prompt_id: str = "rerank-batched-v1"  # Versioned; changing the prompt changes this
    top_k: int = 5  # Candidates kept after reranking.
    # Raised from 3 after measuring: with three passages the model produced a
    # single claim per question, and every one of them verified. Verification
    # was not the constraint on answer length; the evidence supply was.

    connect_timeout: float = 5.0  # Time allowed to establish a connection
    read_timeout: float = 20.0  # Time allowed for the response body
    deadline_seconds: float = 30.0  # Total wall-clock budget across all attempts
    max_attempts: int = 3  # One primary call plus at most two retries
    max_schema_attempts: int = 2  # A malformed response is retried ONCE; a model that cannot satisfy the schema will not learn to on attempt five
    backoff_base_seconds: float = 0.5  # Exponential: base * 2**(attempt - 1)
    backoff_max_seconds: float = 4.0  # Ceiling, so backoff cannot eat the deadline
    jitter: bool = True  # Full jitter, so retries from concurrent sessions do not synchronise

    min_score: float = 0.0  # Inclusive lower bound on a returned relevance score
    max_score: float = 10.0  # Inclusive upper bound
    max_rationale_chars: int = 240  # A rationale longer than this is prose, not a reason

    def __post_init__(self) -> None:
        """Reject limits that would make failure unbounded."""
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.max_schema_attempts < 1:
            raise ValueError("max_schema_attempts must be at least 1")
        if self.max_schema_attempts > self.max_attempts:
            # Clamped rather than rejected: `max_attempts=1` unambiguously means
            # one attempt, and forcing a caller to restate that in a second
            # field is a trap, not a safeguard. The invariant that matters —
            # schema retries are bounded, and bounded below the total — holds
            # either way.
            object.__setattr__(self, "max_schema_attempts", self.max_attempts)
        if self.deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be positive")
        if self.min_score >= self.max_score:
            raise ValueError("min_score must be below max_score")
        if self.top_k < 1:
            raise ValueError("top_k must be at least 1")

    def as_metadata(self) -> dict[str, object]:
        """Machine-readable form, recorded with every run."""
        return {
            "enabled": self.enabled,
            "model": self.model,
            "prompt_id": self.prompt_id,
            "top_k": self.top_k,
            "connect_timeout": self.connect_timeout,
            "read_timeout": self.read_timeout,
            "deadline_seconds": self.deadline_seconds,
            "max_attempts": self.max_attempts,
            "max_schema_attempts": self.max_schema_attempts,
        }


DEFAULT_CANDIDATE_CONFIG = CandidateConfig()
DEFAULT_RERANK_CONFIG = RerankConfig()

__all__ = [
    "DEFAULT_CANDIDATE_CONFIG",
    "DEFAULT_RERANK_CONFIG",
    "CandidateConfig",
    "FusionStrategy",
    "RerankConfig",
]

"""
synapse.retrieval.search
========================
Bounded candidate retrieval: query both components, union, fuse, filter.

The ordering is the design:

1. **Query each component for a bounded ``top_n``.** Never for the corpus.
2. **Union on stable chunk identifiers**, deduplicating deterministically.
3. **Fuse** with the configured strategy, breaking ties on the identifier.
4. **Filter by source-pack eligibility**, before anything downstream sees the
   candidate — so an ungoverned, retracted or expired document cannot reach
   generation even if retrieval ranked it first (requirement 8).
5. **Record everything** in a :class:`RetrievalTrace`, including the candidate
   sizes, so a result can be interpreted and a regression can be attributed.

The guard in :func:`_bounded_request` is the part worth reading twice. The
legacy path asked FAISS for ``top_k=len(self.chunks)`` on every online query;
this raises rather than allowing it, because a limit that is silently clamped is
a limit nobody notices has been exceeded.
"""

from __future__ import annotations  # Postponed annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from synapse.errors import SynapseArtifactError
from synapse.logging import get_logger
from synapse.retrieval.backends import DenseBackend, SparseBackend
from synapse.retrieval.candidates import Candidate, Hit, fuse, union_candidates
from synapse.retrieval.config import DEFAULT_CANDIDATE_CONFIG, CandidateConfig

logger = get_logger(__name__)

# Below this corpus size, a request for the whole corpus is not a scaling
# problem — a four-chunk test fixture is legitimately exhausted by any sensible
# candidate count. Above it, asking for everything is the defect this module
# exists to prevent.
FULL_CORPUS_GUARD_FLOOR = 64


class UnboundedRetrievalError(SynapseArtifactError):
    """A retrieval component was asked for the whole corpus during an online query."""

    reason = "unbounded retrieval request refused"


@dataclass(frozen=True)
class RetrievalTrace:
    """What the retrieval stage did, in machine-readable form.

    Recorded with every run (requirement 9). A candidate set is not
    interpretable without the sizes that produced it, and a benchmark that does
    not record them cannot be compared against another.
    """

    corpus_size: int
    dense_requested: int
    dense_returned: int
    sparse_requested: int
    sparse_returned: int
    union_size: int  # After deduplication
    duplicates_removed: int  # Chunks found by both components
    filtered_ineligible: int  # Removed by the source-pack eligibility filter
    candidates_returned: int  # After truncation to final_top_k
    config: dict[str, object] = field(default_factory=dict)
    eligibility_enforced: bool = False
    dense_latency_ms: float = 0.0
    sparse_latency_ms: float = 0.0
    fusion_latency_ms: float = 0.0

    @property
    def total_latency_ms(self) -> float:
        """Wall-clock across the three retrieval stages."""
        return self.dense_latency_ms + self.sparse_latency_ms + self.fusion_latency_ms

    @property
    def corpus_fraction_examined(self) -> float:
        """Share of the corpus materialised as candidates.

        The headline number for this milestone: the legacy path was 1.0 on every
        query by construction.
        """
        return self.union_size / self.corpus_size if self.corpus_size else 0.0

    def as_metadata(self) -> dict[str, object]:
        """Machine-readable form for run metadata and benchmark records."""
        return {
            "corpus_size": self.corpus_size,
            "dense_requested": self.dense_requested,
            "dense_returned": self.dense_returned,
            "sparse_requested": self.sparse_requested,
            "sparse_returned": self.sparse_returned,
            "union_size": self.union_size,
            "duplicates_removed": self.duplicates_removed,
            "filtered_ineligible": self.filtered_ineligible,
            "candidates_returned": self.candidates_returned,
            "corpus_fraction_examined": round(self.corpus_fraction_examined, 6),
            "eligibility_enforced": self.eligibility_enforced,
            "latency_ms": {
                "dense": round(self.dense_latency_ms, 3),
                "sparse": round(self.sparse_latency_ms, 3),
                "fusion": round(self.fusion_latency_ms, 3),
                "total": round(self.total_latency_ms, 3),
            },
            "config": self.config,
        }


@dataclass(frozen=True)
class RetrievalResult:
    """The candidate set and the record of how it was produced."""

    candidates: list[Candidate]
    trace: RetrievalTrace

    @property
    def is_empty(self) -> bool:
        """True when nothing survived retrieval and filtering."""
        return not self.candidates


def _bounded_request(requested: int, corpus_size: int, component: str) -> int:
    """Validate and clamp one component's candidate request.

    Raises:
        UnboundedRetrievalError: the configured count would require the whole
            corpus on a corpus large enough for that to matter. Refused rather
            than clamped: the legacy path's cost came precisely from a request
            derived from ``len(corpus)``, and silently reducing it here would
            hide a misconfiguration that belongs in front of an operator.
    """
    if corpus_size > FULL_CORPUS_GUARD_FLOOR and requested >= corpus_size:
        raise UnboundedRetrievalError(
            component=component, requested=requested, corpus_size=corpus_size
        )
    return min(requested, corpus_size)


def search_candidates(
    query: str,
    *,
    dense: DenseBackend | None = None,
    sparse: SparseBackend | None = None,
    config: CandidateConfig | None = None,
    eligible_documents: frozenset[str] | None = None,
    dense_distance: bool = True,
) -> RetrievalResult:
    """Retrieve a bounded, fused, eligibility-filtered candidate set.

    Args:
        query: the patient's question.
        dense: dense backend, or ``None`` to run sparse-only.
        sparse: sparse backend, or ``None`` to run dense-only.
        config: candidate sizes and fusion parameters.
        eligible_documents: document identifiers permitted to reach generation,
            normally from :func:`synapse.governance.eligibility.select_eligible`.
            ``None`` disables filtering and is recorded as such — an ungoverned
            run must be visible in the trace, never inferred from its absence.
        dense_distance: True when dense scores are distances (FAISS L2).

    Returns:
        The fused candidates truncated to ``final_top_k``, and the trace.
    """
    config = config or DEFAULT_CANDIDATE_CONFIG
    if dense is None and sparse is None:
        raise ValueError("at least one retrieval backend is required")

    corpus_size = dense.corpus_size if dense is not None else sparse.corpus_size  # type: ignore[union-attr]

    dense_hits: Sequence[Hit] = []
    dense_requested = 0
    dense_elapsed = 0.0
    if dense is not None and config.dense_top_n:
        dense_requested = _bounded_request(config.dense_top_n, dense.corpus_size, "dense")
        started = time.perf_counter()
        dense_hits = dense.search(query, dense_requested)
        dense_elapsed = (time.perf_counter() - started) * 1000

    sparse_hits: Sequence[Hit] = []
    sparse_requested = 0
    sparse_elapsed = 0.0
    if sparse is not None and config.sparse_top_n:
        sparse_requested = _bounded_request(config.sparse_top_n, sparse.corpus_size, "sparse")
        started = time.perf_counter()
        sparse_hits = sparse.search(query, sparse_requested)
        sparse_elapsed = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    union = union_candidates(dense_hits, sparse_hits)
    duplicates = len(dense_hits) + len(sparse_hits) - len(union)

    filtered_out = 0
    if eligible_documents is not None:
        # Requirement 8. Applied to the union, before fusion truncation, so an
        # ineligible document cannot displace an eligible one from the final
        # candidate set merely by ranking above it.
        before = len(union)
        union = [candidate for candidate in union if candidate.document_id in eligible_documents]
        filtered_out = before - len(union)

    fused = fuse(union, config, dense_distance=dense_distance)
    candidates = fused[: config.final_top_k]
    fusion_elapsed = (time.perf_counter() - started) * 1000

    trace = RetrievalTrace(
        corpus_size=corpus_size,
        dense_requested=dense_requested,
        dense_returned=len(dense_hits),
        sparse_requested=sparse_requested,
        sparse_returned=len(sparse_hits),
        union_size=len(fused),
        duplicates_removed=duplicates,
        filtered_ineligible=filtered_out,
        candidates_returned=len(candidates),
        config=config.as_metadata(),
        eligibility_enforced=eligible_documents is not None,
        dense_latency_ms=dense_elapsed,
        sparse_latency_ms=sparse_elapsed,
        fusion_latency_ms=fusion_elapsed,
    )
    logger.info("candidates retrieved", extra=trace.as_metadata())
    return RetrievalResult(candidates=candidates, trace=trace)


__all__ = [
    "FULL_CORPUS_GUARD_FLOOR",
    "RetrievalResult",
    "RetrievalTrace",
    "UnboundedRetrievalError",
    "search_candidates",
]

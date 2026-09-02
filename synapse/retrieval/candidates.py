"""
synapse.retrieval.candidates
============================
The candidate union: build it, deduplicate it, fuse it, and keep the evidence.

What this replaces
------------------
``Retrieval/hybrid_retriever.linear_fusion`` fused across the **entire corpus**
on every online query: FAISS was asked for ``top_k=len(chunks)``, BM25 produced
a score for every chunk, and the two arrays were combined position-by-position.
On the committed 2,220-chunk corpus that is ~2,220 dict entries, two 2,220-element
score arrays and a 2,220-iteration fusion loop per query, to return ten results.

It was also **unsound**, independently of cost. The alignment was rebuilt with
``{chunk.chunk_id(): score}`` keyed on the legacy identifier
``f"{pmid}_chunk{index}"``, which collides for any document ingested more than
once — 162 collisions in the committed corpus (docs/quality-architecture.md
§1.2, C6/C7). Colliding chunks overwrote one another in the score map, and every
chunk missing from the map was silently assigned a default distance of 2.0.

Here, a candidate is keyed by its **stable chunk identifier**, carries the rank
and score from each component that produced it, and survives fusion as one
object. Nothing is reconstructed by position, so nothing can be misaligned.

Determinism
-----------
Every ordering in this module is total. Fused scores are floats and ties are
real — two candidates found at the same rank by both components genuinely tie —
so ties break on ``chunk_id``, which is unique by construction. The same inputs
always produce the same output, in the same order, on any machine: a property
the benchmark and the snapshot tests both depend on.
"""

from __future__ import annotations  # Postponed annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from synapse.retrieval.config import CandidateConfig, FusionStrategy

DENSE = "dense"
SPARSE = "sparse"


@dataclass(frozen=True)
class Hit:
    """One result from a single retrieval component.

    Deliberately minimal: a backend reports what it found and at what rank, and
    knows nothing about fusion, eligibility or reranking.
    """

    chunk_id: str
    document_id: str
    text: str
    rank: int  # One-based, within this component's result list
    score: float  # Raw component score, whatever its scale
    title: str = ""
    url: str = ""

    def __post_init__(self) -> None:
        """A hit with a non-positive rank cannot be fused."""
        if self.rank < 1:
            raise ValueError("hit rank is one-based and must be at least 1")


@dataclass(frozen=True)
class Candidate:
    """One deduplicated candidate, with the evidence from every component.

    Component ranks and scores are retained rather than collapsed into the fused
    score (requirement 7). Without them, "why did this rank third?" is
    unanswerable after the fact, and the two components cannot be evaluated
    separately.
    """

    chunk_id: str
    document_id: str
    text: str
    title: str = ""
    url: str = ""

    dense_rank: int | None = None
    dense_score: float | None = None
    sparse_rank: int | None = None
    sparse_score: float | None = None

    fused_score: float = 0.0
    fused_rank: int = 0  # Assigned by :func:`fuse`, one-based

    @property
    def components(self) -> tuple[str, ...]:
        """Which components retrieved this candidate, in a stable order."""
        found = []
        if self.dense_rank is not None:
            found.append(DENSE)
        if self.sparse_rank is not None:
            found.append(SPARSE)
        return tuple(found)

    @property
    def retrieved_by_both(self) -> bool:
        """True when dense and sparse retrieval agree this is a candidate."""
        return self.dense_rank is not None and self.sparse_rank is not None

    def as_metadata(self) -> dict[str, object]:
        """Machine-readable form for traces, benchmarks and debugging."""
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "fused_rank": self.fused_rank,
            "fused_score": round(self.fused_score, 6),
            "dense_rank": self.dense_rank,
            "dense_score": None if self.dense_score is None else round(self.dense_score, 6),
            "sparse_rank": self.sparse_rank,
            "sparse_score": None if self.sparse_score is None else round(self.sparse_score, 6),
            "components": list(self.components),
        }


def union_candidates(dense: Sequence[Hit], sparse: Sequence[Hit]) -> list[Candidate]:
    """Merge two component result lists into one deduplicated candidate list.

    Deduplication is by ``chunk_id`` — the stable identifier, not the text and
    not a position — so a chunk found by both components becomes **one**
    candidate carrying both ranks (requirements 3 and 4).

    Within a component, a repeated ``chunk_id`` keeps the better (lower) rank:
    a backend returning the same chunk twice is malformed, and taking the first
    occurrence silently would make the result depend on iteration order.

    The returned order is the insertion order (dense first, then sparse-only
    candidates), which is deterministic but *not* meaningful — :func:`fuse`
    assigns the ordering that matters.
    """
    merged: dict[str, Candidate] = {}

    for hit in dense:
        existing = merged.get(hit.chunk_id)
        if existing is None:
            merged[hit.chunk_id] = Candidate(
                chunk_id=hit.chunk_id,
                document_id=hit.document_id,
                text=hit.text,
                title=hit.title,
                url=hit.url,
                dense_rank=hit.rank,
                dense_score=hit.score,
            )
        elif existing.dense_rank is None or hit.rank < existing.dense_rank:
            merged[hit.chunk_id] = replace(existing, dense_rank=hit.rank, dense_score=hit.score)

    for hit in sparse:
        existing = merged.get(hit.chunk_id)
        if existing is None:
            merged[hit.chunk_id] = Candidate(
                chunk_id=hit.chunk_id,
                document_id=hit.document_id,
                text=hit.text,
                title=hit.title,
                url=hit.url,
                sparse_rank=hit.rank,
                sparse_score=hit.score,
            )
        elif existing.sparse_rank is None or hit.rank < existing.sparse_rank:
            merged[hit.chunk_id] = replace(existing, sparse_rank=hit.rank, sparse_score=hit.score)

    return list(merged.values())


def _min_max_normalise(values: Mapping[str, float], *, invert: bool) -> dict[str, float]:
    """Scale scores to [0, 1] over the candidate set.

    ``invert`` is for distance-like scores, where lower is better: FAISS returns
    L2 distances. Normalising over the **candidate set** rather than the corpus
    is a deliberate difference from the legacy implementation, and the only one
    that changes fused values: the range is now the range actually under
    consideration, which is what makes bounded fusion possible at all.

    An empty or constant set maps to 0.5 — neutral, so a component that cannot
    discriminate does not decide the ordering by accident.
    """
    if not values:
        return {}
    lowest = min(values.values())
    highest = max(values.values())
    if highest - lowest < 1e-12:
        return dict.fromkeys(values, 0.5)
    span = highest - lowest
    if invert:
        return {key: (highest - value) / span for key, value in values.items()}
    return {key: (value - lowest) / span for key, value in values.items()}


def _sorted_deterministically(candidates: Iterable[Candidate]) -> list[Candidate]:
    """Order by fused score descending, breaking ties on ``chunk_id``.

    Ties are common and are not noise: two candidates at identical ranks in both
    components have identical RRF scores by construction. Breaking on the
    identifier makes the order total and reproducible; without it, the result
    depends on dict iteration order, which is stable within a process and
    therefore hides the problem until a benchmark is re-run elsewhere.
    """
    return sorted(candidates, key=lambda candidate: (-candidate.fused_score, candidate.chunk_id))


def fuse(
    candidates: Sequence[Candidate],
    config: CandidateConfig,
    *,
    dense_distance: bool = True,
) -> list[Candidate]:
    """Score and order the candidate union.

    Args:
        candidates: the deduplicated union.
        config: fusion strategy and its parameters.
        dense_distance: True when the dense score is a distance (lower is
            better), as FAISS L2 is. False for a similarity.

    Returns:
        The candidates, ordered, each carrying ``fused_score`` and a one-based
        ``fused_rank``. The full union is returned, not a truncated list —
        truncation is the caller's decision and the trace records both sizes.
    """
    if not candidates:
        return []

    if config.fusion is FusionStrategy.RRF:
        scored = {
            candidate.chunk_id: _rrf_score(candidate, config.rrf_k) for candidate in candidates
        }
    else:
        dense_scores = {c.chunk_id: c.dense_score for c in candidates if c.dense_score is not None}
        sparse_scores = {
            c.chunk_id: c.sparse_score for c in candidates if c.sparse_score is not None
        }
        normalised_dense = _min_max_normalise(dense_scores, invert=dense_distance)
        normalised_sparse = _min_max_normalise(sparse_scores, invert=False)
        floor = config.missing_score_floor
        scored = {
            candidate.chunk_id: (
                config.alpha * normalised_dense.get(candidate.chunk_id, floor)
                + (1.0 - config.alpha) * normalised_sparse.get(candidate.chunk_id, floor)
            )
            for candidate in candidates
        }

    ordered = _sorted_deterministically(
        replace(candidate, fused_score=scored[candidate.chunk_id]) for candidate in candidates
    )
    return [
        replace(candidate, fused_rank=index) for index, candidate in enumerate(ordered, start=1)
    ]


def _rrf_score(candidate: Candidate, rrf_k: int) -> float:
    """Reciprocal rank fusion over whichever components found this candidate.

    ``sum(1 / (k + rank))``. Rank-only, so it is unaffected by the scale of
    either component's scores — the property that makes it safe to apply to a
    bounded candidate set, where score ranges shift with the cut-off.

    A candidate found by only one component is not penalised beyond receiving
    one term instead of two; that asymmetry is the mechanism by which agreement
    between components is rewarded.
    """
    score = 0.0
    if candidate.dense_rank is not None:
        score += 1.0 / (rrf_k + candidate.dense_rank)
    if candidate.sparse_rank is not None:
        score += 1.0 / (rrf_k + candidate.sparse_rank)
    return score


def top_k(candidates: Sequence[Candidate], k: int) -> list[Candidate]:
    """The first ``k`` candidates of an already-fused list."""
    return list(candidates[:k])


__all__ = [
    "DENSE",
    "SPARSE",
    "Candidate",
    "Hit",
    "fuse",
    "top_k",
    "union_candidates",
]

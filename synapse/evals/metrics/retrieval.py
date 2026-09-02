"""
synapse.evals.metrics.retrieval
===============================
Retrieval metrics: Recall@k, Precision@k, MRR, nDCG@k, hit rate.

Every function is pure arithmetic over recorded identifiers — no model, no
network, no clock. That is what makes the deterministic half of the harness
reproducible offline.

Three denominator decisions are made here deliberately, because the legacy
implementation got each of them wrong and the errors were invisible:

* **Recall@k returns ``None``, not ``0.0``, when a case has no relevant
  documents.** The old code returned 0.0, which is indistinguishable from
  "retrieval found nothing" — and since the app called it with an empty
  ground-truth list on every live query, its reported recall was structurally
  zero regardless of retrieval quality.
* **Precision@k divides by ``min(k, len(retrieved))``, not by ``k``.** Dividing
  by k under-reports precision whenever fewer than k results come back, which
  is common on a small corpus.
* **Retrieved identifiers are deduplicated before scoring.** The shipped corpus
  contains 162 colliding chunk identifiers across 2,220 chunks; without
  deduplication a duplicate document counts twice and inflates precision.
"""

from __future__ import annotations  # Postponed annotations

import math  # Logarithmic discount for nDCG
from collections.abc import Mapping, Sequence

# Grade at or above which a document counts as "relevant" for the binary
# metrics. Grade 1 is "marginally related; would not be missed", so counting it
# would make recall trivially easy to satisfy.
RELEVANCE_THRESHOLD = 1


def _dedupe_preserving_order(items: Sequence[str]) -> list[str]:
    """Remove repeats while keeping rank order.

    Rank order is what MRR and nDCG measure, so a set would destroy the signal;
    but a repeated document must not be counted twice.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _relevant_ids(relevance: Mapping[str, int]) -> set[str]:
    """Identifiers graded at or above the relevance threshold."""
    return {target for target, grade in relevance.items() if grade >= RELEVANCE_THRESHOLD}


def recall_at_k(retrieved: Sequence[str], relevance: Mapping[str, int], k: int) -> float | None:
    """Fraction of relevant documents that appear in the top ``k``.

    Denominator: the number of documents graded relevant for this case.

    Returns ``None`` when the case has no relevant documents — the metric is
    genuinely undefined there, and reporting 0.0 would make an unlabelled case
    look like a retrieval failure.
    """
    relevant = _relevant_ids(relevance)
    if not relevant:
        return None
    top_k = set(_dedupe_preserving_order(retrieved)[:k])
    return len(top_k & relevant) / len(relevant)


def precision_at_k(retrieved: Sequence[str], relevance: Mapping[str, int], k: int) -> float | None:
    """Fraction of the top ``k`` retrieved documents that are relevant.

    Denominator: ``min(k, len(retrieved))``. Dividing by ``k`` when fewer than
    ``k`` results exist penalises a system for a short result list rather than
    for a wrong one.

    Returns ``None`` when nothing was retrieved, or when the case has no
    relevant documents to be precise about.
    """
    deduped = _dedupe_preserving_order(retrieved)
    if not deduped:
        return None
    relevant = _relevant_ids(relevance)
    if not relevant:
        return None
    top_k = deduped[:k]
    hits = sum(1 for document_id in top_k if document_id in relevant)
    return hits / len(top_k)


def reciprocal_rank(retrieved: Sequence[str], relevance: Mapping[str, int]) -> float | None:
    """Reciprocal of the rank of the first relevant document.

    1.0 when the first result is relevant, 0.5 when the second is, and so on.
    ``0.0`` when nothing relevant was retrieved at all — which is a genuine
    measurement, distinct from the ``None`` returned when the case has no
    ground truth to find.
    """
    relevant = _relevant_ids(relevance)
    if not relevant:
        return None
    for rank, document_id in enumerate(_dedupe_preserving_order(retrieved), start=1):
        if document_id in relevant:
            return 1.0 / rank
    return 0.0


def dcg_at_k(retrieved: Sequence[str], relevance: Mapping[str, int], k: int) -> float:
    """Discounted cumulative gain over the top ``k``.

    Uses the exponential gain ``2^grade - 1``, which is what makes graded
    relevance worth collecting: it rewards a grade-3 document substantially
    more than a grade-1, where a linear gain would treat three marginal hits as
    equal to one perfect one.
    """
    total = 0.0
    for index, document_id in enumerate(_dedupe_preserving_order(retrieved)[:k], start=1):
        grade = relevance.get(document_id, 0)
        if grade > 0:
            total += (2**grade - 1) / math.log2(
                index + 1
            )  # index+1 so the first position discounts by log2(2)=1
    return total


def ndcg_at_k(retrieved: Sequence[str], relevance: Mapping[str, int], k: int) -> float | None:
    """Normalised discounted cumulative gain over the top ``k``.

    DCG divided by the DCG of the ideal ranking — every graded document sorted
    by grade descending. Returns ``None`` when the case has no relevant
    documents, since the ideal DCG would be zero and the ratio undefined.
    """
    graded = {target: grade for target, grade in relevance.items() if grade > 0}
    if not graded:
        return None
    ideal_order = sorted(
        graded, key=lambda target: graded[target], reverse=True
    )  # The best ranking achievable for this case
    ideal = dcg_at_k(ideal_order, relevance, k)
    if (
        ideal == 0
    ):  # Defensive: unreachable while graded is non-empty, but a zero divisor must never escape
        return None
    return dcg_at_k(retrieved, relevance, k) / ideal


def hit_at_k(retrieved: Sequence[str], relevance: Mapping[str, int], k: int) -> float | None:
    """1.0 when at least one relevant document appears in the top ``k``.

    Coarser than recall, and useful precisely because of that: for a case with
    one relevant document, recall@5 and hit@5 agree, but across a mixed dataset
    hit rate answers "did we find *anything* useful" without being dragged down
    by cases that have many relevant documents.
    """
    relevant = _relevant_ids(relevance)
    if not relevant:
        return None
    top_k = set(_dedupe_preserving_order(retrieved)[:k])
    return 1.0 if top_k & relevant else 0.0


__all__ = [
    "RELEVANCE_THRESHOLD",
    "dcg_at_k",
    "hit_at_k",
    "ndcg_at_k",
    "precision_at_k",
    "recall_at_k",
    "reciprocal_rank",
]

"""
synapse.evals.metrics
=====================
Deterministic metric families. Pure arithmetic over recorded outputs — no
model, no network, no clock — which is what lets the whole family run offline
and reproduce exactly.

    retrieval   Recall@k, Precision@k, MRR, nDCG@k, hit rate
    answer      citation correctness/completeness, groundedness, readability
    safety      emergency sensitivity/specificity, negation, medication, injection
    cost        tokens, latency percentiles, cost from a versioned price table
"""

from __future__ import annotations  # Postponed annotations

__all__: list[
    str
] = []  # Submodules are imported explicitly, so a metric's family is visible at every call site

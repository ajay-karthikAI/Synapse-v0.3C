"""
synapse.retrieval
=================
Bounded candidate retrieval and batched reranking.

Replaces two online behaviours that scaled with the corpus rather than with the
query:

* ``Retrieval/hybrid_retriever.py`` asked FAISS for ``top_k=len(chunks)`` and
  BM25 for a score per chunk on **every** query, then fused across the whole
  corpus — and rebuilt the alignment through a collision-prone legacy
  identifier, so the fusion was unsound as well as expensive.
* ``Retrieval/reranker.py`` issued **one LLM request per candidate**, in a loop,
  with no timeout and a bare ``except`` that made an outage look like a result.

    synapse.retrieval.config      candidate sizes, fusion and rerank limits
    synapse.retrieval.backends    the two component protocols, plus offline fakes
    synapse.retrieval.candidates  union, deduplication, fusion, tie-breaking
    synapse.retrieval.search      bounded retrieval, eligibility filter, trace
    synapse.retrieval.rerank      one batched structured request, validated
    synapse.retrieval.evidence    candidates to the answer layer's evidence

The invariants: no online query asks a component for the corpus; candidates are
keyed by stable chunk identifiers throughout; every ordering is total and
reproducible; ineligible sources are filtered before generation sees them; and
degradation never relaxes a P1 guarantee — a degraded rerank still produces
evidence that the answer layer verifies claim by claim.
"""

from __future__ import annotations  # Postponed annotations

from synapse.retrieval.candidates import Candidate, Hit, fuse, top_k, union_candidates
from synapse.retrieval.config import (
    DEFAULT_CANDIDATE_CONFIG,
    DEFAULT_RERANK_CONFIG,
    CandidateConfig,
    FusionStrategy,
    RerankConfig,
)
from synapse.retrieval.evidence import RetrievalBundle, bundle_from_candidates
from synapse.retrieval.rerank import (
    RerankOutcome,
    RerankOutcomeKind,
    RerankValidationError,
    rerank_candidates,
)
from synapse.retrieval.search import (
    RetrievalResult,
    RetrievalTrace,
    UnboundedRetrievalError,
    search_candidates,
)

__all__ = [
    "DEFAULT_CANDIDATE_CONFIG",
    "DEFAULT_RERANK_CONFIG",
    "Candidate",
    "CandidateConfig",
    "FusionStrategy",
    "Hit",
    "RerankConfig",
    "RerankOutcome",
    "RerankOutcomeKind",
    "RerankValidationError",
    "RetrievalBundle",
    "RetrievalResult",
    "RetrievalTrace",
    "UnboundedRetrievalError",
    "bundle_from_candidates",
    "fuse",
    "rerank_candidates",
    "search_candidates",
    "top_k",
    "union_candidates",
]

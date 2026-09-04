"""
synapse.retrieval.evidence
==========================
Typed candidates to the evidence the answer layer verifies against.

This is the join between retrieval and generation, and it is deliberately dull:
a direct field-for-field construction with no identifier rewriting. Candidates
already carry stable ``chunk_id`` and ``document_id`` values, so nothing here
has to derive, parse or repair one — which is the whole difference from
:mod:`synapse.ui.legacy_evidence`, the deprecated adapter that must reconstruct
identifiers from a legacy chunk object and drop what it cannot.

``source_order`` is the fused (or reranked) candidate order, and it is what
:class:`~synapse.answer.render.SourceNumbering` numbers from. So the ``[1]`` a
patient sees is the top-ranked source for their question, and the ordering the
retrieval trace records is the ordering on the page.

``relevance`` is the one number here that reaches a patient as a judgement, so
it comes from the reranker's verdict or it is absent — never from a fusion
score, which measures rank agreement rather than usefulness. See
:func:`bundle_from_candidates`.
"""

from __future__ import annotations  # Postponed annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from synapse.answer.verify import RetrievedEvidence
from synapse.retrieval.candidates import Candidate
from synapse.retrieval.config import DEFAULT_RERANK_CONFIG

if TYPE_CHECKING:  # Annotation only: this module does not depend on the reranker at runtime
    from synapse.retrieval.rerank import RerankVerdict


@dataclass(frozen=True)
class RetrievalBundle:
    """Everything the answer pipeline needs from retrieval, already typed."""

    evidence: RetrievedEvidence
    source_order: list[str]  # Document identifiers, in display order
    relevance: dict[str, float] = field(default_factory=dict)  # document_id -> 0..1
    metadata: dict[str, object] = field(default_factory=dict)  # Retrieval and rerank traces

    @property
    def is_empty(self) -> bool:
        """True when there is nothing to ground an answer in."""
        return not self.evidence.chunk_texts


def bundle_from_candidates(
    candidates: Sequence[Candidate],
    *,
    verdicts: Mapping[str, RerankVerdict] | None = None,
    relevance_scale: float = DEFAULT_RERANK_CONFIG.max_score,
    metadata: dict[str, object] | None = None,
) -> RetrievalBundle:
    """Build the evidence bundle from a ranked candidate list.

    ``relevance`` is normalised to 0..1 for display and carries the *first*
    (best-ranked) candidate's score for each document, because the source panel
    shows one row per document while candidates are per chunk.

    It is derived from the reranker's ``verdicts`` — a judgement of *usefulness
    for this question* — and **never** from ``fused_score``. Fusion scores
    cannot carry that meaning: under RRF, the default, the score is
    ``sum(1 / (k + rank))`` over the components that found a candidate, so it is
    a function of rank alone and is bounded in roughly ``[0.009, 0.033]``
    whatever the corpus actually contained. Rescaling that onto 0..1 would put
    the top source near 100% on *every* query, including one this corpus cannot
    answer, which is precisely the reading
    :class:`~synapse.api.models.SourceModel` forbids.

    So a document with no verdict gets **no relevance entry at all**, and the
    interface renders no band for it rather than a fabricated one. That is the
    honest state whenever reranking was disabled, skipped, or degraded to the
    fused order (:class:`~synapse.retrieval.rerank.RerankOutcomeKind`) — the
    ordering is still usable, but nothing judged these passages against the
    question, so there is nothing to report.
    """
    chunk_texts: dict[str, str] = {}
    titles: dict[str, str] = {}
    urls: dict[str, str] = {}
    relevance: dict[str, float] = {}
    source_order: list[str] = []

    for candidate in candidates:
        chunk_texts[candidate.chunk_id] = candidate.text
        if candidate.document_id not in source_order:
            source_order.append(candidate.document_id)
            titles[candidate.document_id] = candidate.title or candidate.document_id
            urls[candidate.document_id] = candidate.url
            verdict = None if verdicts is None else verdicts.get(candidate.chunk_id)
            if verdict is not None and relevance_scale > 0:
                # Clamped: a reranker score at the top of its range must not
                # render as more than 100%. `_validate_response` already rejects
                # an out-of-range score, so this is defence in depth for a
                # caller that assembled the mapping itself.
                relevance[candidate.document_id] = min(
                    1.0, max(0.0, verdict.relevance / relevance_scale)
                )

    return RetrievalBundle(
        evidence=RetrievedEvidence(
            chunk_texts=chunk_texts,
            source_ids=frozenset(source_order),
            source_titles=titles,
            source_urls=urls,
        ),
        source_order=source_order,
        relevance=relevance,
        metadata=metadata or {},
    )


__all__ = ["RetrievalBundle", "bundle_from_candidates"]

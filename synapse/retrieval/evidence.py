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
"""

from __future__ import annotations  # Postponed annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from synapse.answer.verify import RetrievedEvidence
from synapse.retrieval.candidates import Candidate


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
    relevance_scale: float = 10.0,
    metadata: dict[str, object] | None = None,
) -> RetrievalBundle:
    """Build the evidence bundle from a ranked candidate list.

    ``relevance`` is normalised to 0..1 for display and carries the *first*
    (best-ranked) candidate's score for each document, because the source panel
    shows one row per document while candidates are per chunk.
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
            if relevance_scale > 0:
                # Clamped: a reranker score at the top of its range must not
                # render as more than 100%.
                relevance[candidate.document_id] = min(
                    1.0, max(0.0, candidate.fused_score / relevance_scale)
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

"""
synapse.ui.legacy_evidence
==========================
**DEPRECATED.** Adapter from the legacy retrieval result shape to typed evidence.

Scheduled for removal on **2026-11-30** (see ``REMOVAL_DEADLINE`` and
docs/answer-rendering.md §7). Do not add callers.

Why it exists at all
--------------------
The production retrieval stack (``Retrieval/hybrid_retriever.py``,
``Retrieval/reranker.py``) still returns lists of
``{"chunk": Chunk, "rank": int, "relevance_score": float}`` where ``Chunk`` is
the legacy dataclass from ``Data/fetch_and_chunk.py``. That object identifies a
chunk as ``f"{pmid}_chunk{chunk_index}"``, which collides across documents
ingested more than once (162 collisions in the committed corpus —
docs/quality-architecture.md §1.2, C6/C7) and does not satisfy the identifier
grammar the answer layer verifies against.

Rather than let that shape reach :mod:`synapse.answer`, it is converted **here,
once**, into :class:`synapse.answer.verify.RetrievedEvidence` keyed by real
identifiers. Everything downstream — the prompt, verification, numbering, the
rendered page and the exported brief — then sees one shape.

Fail-closed conversion
----------------------
Anything that cannot be given a well-formed identifier is **dropped**, not
guessed at:

* a chunk with no numeric PMID and no text to hash;
* a chunk whose derived identifier does not match the grammar;
* a duplicate identifier, where only the first occurrence is kept.

Dropping is safe in the direction that matters: evidence that is not in the
map is never shown to the model *and* never verifies a citation, so a claim
that depends on it is withheld. The alternative — inventing an identifier so
the chunk survives — would produce a citation pointing at a document that does
not exist under that name.

The counts are returned rather than logged and forgotten, so the caller can
surface them and so a corpus that starts failing conversion is visible.
"""

from __future__ import annotations  # Postponed annotations

import warnings
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from synapse.answer.verify import RetrievedEvidence
from synapse.errors import ArtifactSchemaError
from synapse.identifiers import (
    document_id_for_content,
    document_id_for_pubmed,
    is_valid_chunk_id,
    make_chunk_id,
)
from synapse.logging import get_logger

logger = get_logger(__name__)

REMOVAL_DEADLINE = date(
    2026, 11, 30
)  # Hard date. After it, this module is deleted and the retrieval layer emits typed records directly (docs/answer-rendering.md §7).

DEPRECATION_MESSAGE = (
    "synapse.ui.legacy_evidence is a temporary migration adapter for the legacy "
    f"retrieval result shape and is scheduled for removal on {REMOVAL_DEADLINE.isoformat()}. "
    "New code must build synapse.answer.verify.RetrievedEvidence directly."
)


@dataclass(frozen=True)
class ConversionResult:
    """Converted evidence plus what could not be converted."""

    evidence: RetrievedEvidence
    source_order: list[str]  # Retrieval order; drives display numbering
    relevance: dict[str, float] = field(default_factory=dict)  # source_id -> reranker relevance
    dropped_unidentifiable: int = 0  # Chunks with no derivable identifier
    dropped_duplicate_ids: int = 0  # Legacy identifier collisions (C6/C7)

    @property
    def is_empty(self) -> bool:
        """True when nothing usable survived conversion."""
        return not self.evidence.chunk_texts


def _document_id_for(chunk: Any) -> str | None:
    """Derive a well-formed document identifier, or ``None`` if impossible."""
    pmid = str(getattr(chunk, "pmid", "") or "").strip()
    text = str(getattr(chunk, "text", "") or "")
    try:
        if pmid.isdigit():
            return document_id_for_pubmed(pmid)
        if text:
            # No natural key: content-address it, exactly as the ingestion
            # pipeline does for a pasted text file.
            return document_id_for_content("txt", text)
    except ArtifactSchemaError:
        # Malformed after all. Dropped rather than repaired.
        return None
    return None


def evidence_from_reranked(results: list[Any]) -> ConversionResult:
    """Convert legacy reranked retrieval results into typed evidence.

    Emits :class:`DeprecationWarning` on every call, so a caller added after the
    removal deadline is visible in test output rather than silent.
    """
    warnings.warn(DEPRECATION_MESSAGE, DeprecationWarning, stacklevel=2)

    chunk_texts: dict[str, str] = {}
    titles: dict[str, str] = {}
    urls: dict[str, str] = {}
    relevance: dict[str, float] = {}
    source_order: list[str] = []
    unidentifiable = 0
    duplicates = 0

    for item in results:
        chunk = item.get("chunk") if isinstance(item, dict) else None
        if chunk is None:
            unidentifiable += 1
            continue
        text = str(getattr(chunk, "text", "") or "")
        document_id = _document_id_for(chunk) if text else None
        if document_id is None:
            unidentifiable += 1
            continue

        ordinal = getattr(chunk, "chunk_index", 0)
        chunk_id = make_chunk_id(document_id, int(ordinal) if isinstance(ordinal, int) else 0)
        if not is_valid_chunk_id(chunk_id):  # Belt and braces: make_chunk_id already validates
            unidentifiable += 1
            continue
        if chunk_id in chunk_texts:
            # A legacy collision: two different chunks claiming one identifier.
            # The first is kept and the second dropped, so no quote can verify
            # against text the model was not shown under that identifier.
            duplicates += 1
            continue

        chunk_texts[chunk_id] = text
        if document_id not in source_order:
            source_order.append(document_id)  # Retrieval order, preserved
            titles[document_id] = str(getattr(chunk, "title", "") or document_id)
            urls[document_id] = str(getattr(chunk, "source_url", "") or "")
        score = item.get("relevance_score") if isinstance(item, dict) else None
        if isinstance(score, (int, float)) and document_id not in relevance:
            relevance[document_id] = float(score)

    logger.info(
        "legacy retrieval results converted",
        extra={
            "chunks": len(chunk_texts),
            "sources": len(source_order),
            "dropped_unidentifiable": unidentifiable,
            "dropped_duplicate_ids": duplicates,
        },
    )
    return ConversionResult(
        evidence=RetrievedEvidence(
            chunk_texts=chunk_texts,
            source_ids=frozenset(source_order),
            source_titles=titles,
            source_urls=urls,
        ),
        source_order=source_order,
        relevance=relevance,
        dropped_unidentifiable=unidentifiable,
        dropped_duplicate_ids=duplicates,
    )


__all__ = [
    "DEPRECATION_MESSAGE",
    "REMOVAL_DEADLINE",
    "ConversionResult",
    "evidence_from_reranked",
]

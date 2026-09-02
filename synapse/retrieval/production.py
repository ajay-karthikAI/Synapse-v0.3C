"""
synapse.retrieval.production
============================
Adapters that put the real index behind the retrieval protocols.

The production stack still stores its index as the legacy ``VectorStore`` and
``BM25Index`` objects, so something has to bridge them to
:mod:`synapse.retrieval.backends`. That bridging is here, in one file, so that
``app.py`` contains no retrieval logic and the rest of :mod:`synapse.retrieval`
never imports the legacy tree.

Two properties this file is responsible for:

* **Bounded queries.** Both adapters pass ``top_n`` straight through to the
  underlying index. Nothing here derives a limit from ``len(corpus)``, which is
  what the code being replaced did on every query.
* **Stable identifiers.** The legacy chunk object identifies itself as
  ``f"{pmid}_chunk{index}"``, which collides across documents ingested more than
  once. The adapters rebuild the identifier to the grammar in
  :mod:`synapse.identifiers` and **drop** any chunk that cannot be given one,
  so a candidate always carries an identifier the answer layer can verify a
  citation against.

Marked for removal with the rest of the legacy retrieval tree (2026-11-30, see
docs/retrieval-runtime.md §8): when ``Retrieval/`` emits typed records, these
adapters become unnecessary rather than merely thin.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass
from typing import Any

from synapse.errors import ArtifactSchemaError
from synapse.identifiers import (
    document_id_for_content,
    document_id_for_pubmed,
    make_chunk_id,
)
from synapse.logging import get_logger
from synapse.retrieval.candidates import Hit

logger = get_logger(__name__)


def stable_identifiers(chunk: Any) -> tuple[str, str] | None:
    """Derive ``(document_id, chunk_id)`` for a legacy chunk, or ``None``.

    ``None`` means the chunk cannot be cited safely, and the caller drops it.
    That is the fail-closed direction: evidence that is absent cannot be shown
    to the model and cannot verify a citation, whereas an invented identifier
    would produce a citation pointing at a document that does not exist.
    """
    text = str(getattr(chunk, "text", "") or "")
    if not text:
        return None
    pmid = str(getattr(chunk, "pmid", "") or "").strip()
    ordinal = getattr(chunk, "chunk_index", 0)
    try:
        document_id = (
            document_id_for_pubmed(pmid) if pmid.isdigit() else document_id_for_content("txt", text)
        )
        return document_id, make_chunk_id(
            document_id, int(ordinal) if isinstance(ordinal, int) else 0
        )
    except (ArtifactSchemaError, ValueError):
        return None


def _hit_from_chunk(chunk: Any, rank: int, score: float) -> Hit | None:
    """Build a :class:`Hit` from a legacy chunk, or ``None`` if it cannot be identified."""
    identifiers = stable_identifiers(chunk)
    if identifiers is None:
        return None
    document_id, chunk_id = identifiers
    return Hit(
        chunk_id=chunk_id,
        document_id=document_id,
        text=str(getattr(chunk, "text", "")),
        rank=rank,
        score=float(score),
        title=str(getattr(chunk, "title", "") or ""),
        url=str(getattr(chunk, "source_url", "") or ""),
    )


@dataclass
class LegacyDenseBackend:
    """The legacy ``VectorStore`` behind :class:`~synapse.retrieval.backends.DenseBackend`."""

    store: Any  # Retrieval.vector_store.VectorStore
    api_key: str

    @property
    def corpus_size(self) -> int:
        """Number of vectors in the index."""
        return len(getattr(self.store, "chunks", []) or [])

    def search(self, query: str, top_n: int) -> list[Hit]:
        """Bounded nearest-neighbour query. Scores are L2 distances."""
        raw = self.store.search(query, self.api_key, top_k=top_n)
        hits: list[Hit] = []
        dropped = 0
        for position, item in enumerate(raw, start=1):
            hit = _hit_from_chunk(item["chunk"], position, item["score"])
            if hit is None:
                dropped += 1
                continue
            hits.append(hit)
        if dropped:
            logger.warning(
                "dense hits dropped for unusable identifiers", extra={"dropped": dropped}
            )
        return hits


@dataclass
class LegacySparseBackend:
    """The legacy ``BM25Index`` behind :class:`~synapse.retrieval.backends.SparseBackend`.

    Calls ``search(top_k=top_n)``, never ``get_all_scores``: the latter produces
    a score for every chunk in the corpus and was half of the cost this
    milestone removes.
    """

    index: Any  # Retrieval.bm25_index.BM25Index

    @property
    def corpus_size(self) -> int:
        """Number of chunks indexed."""
        return len(getattr(self.index, "chunks", []) or [])

    def search(self, query: str, top_n: int) -> list[Hit]:
        """Bounded lexical query."""
        raw = self.index.search(query, top_k=top_n)
        hits: list[Hit] = []
        dropped = 0
        for position, item in enumerate(raw, start=1):
            hit = _hit_from_chunk(item["chunk"], position, item["score"])
            if hit is None:
                dropped += 1
                continue
            hits.append(hit)
        if dropped:
            logger.warning(
                "sparse hits dropped for unusable identifiers", extra={"dropped": dropped}
            )
        return hits


__all__ = ["LegacyDenseBackend", "LegacySparseBackend", "stable_identifiers"]

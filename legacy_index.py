"""
legacy_index
============
The binding between the application service and the prototype's index.

This file exists because of one rule: **no module inside ``synapse`` may import
``Data``, ``Retrieval``, ``Generation`` or ``Evaluation``**. The package has to
stay installable and testable without a vector-search stack, and
``tests/test_no_pickle_and_imports.py`` fails the build if that ever stops being
true. :mod:`synapse.service.index` therefore describes the four loading steps it
needs as callables; this module is the one place that supplies the legacy ones.

It sits at the repository root, beside ``app.py`` and ``build_corpus.py``, for
the same reason ``app.py`` does: it is part of the prototype, not part of the
package. Both interfaces -- the Streamlit fallback today, the FastAPI process
next -- import it from here, so neither grows its own copy of this wiring and
the two cannot drift apart.

Dated for removal with the rest of the legacy retrieval tree (2026-11-30, see
docs/retrieval-runtime.md §8). When ``Retrieval/`` emits typed records, the
service can be pointed at them directly and this file becomes unnecessary rather
than merely thin.

Every import below is function-local, exactly as it was in ``app.py``. Importing
this module must not cost a FAISS load, because the FastAPI process imports it
at startup and the failure mode of a missing prototype dependency should be a
typed failure on a turn, not a process that will not boot.
"""

from __future__ import annotations  # Postponed annotations

from pathlib import Path
from typing import Any

from synapse.service.index import (
    DEFAULT_CHUNKS_PATH,
    DEFAULT_INDEX_DIR,
    CachingIndexProvider,
)

# Fusion settings for the legacy hybrid retriever. Carried over unchanged from
# app.py: this extraction moves the call, not the configuration.
FUSION_STRATEGY = "linear"
FUSION_ALPHA = 0.7


def legacy_index_provider(
    *,
    api_key: str,
    chunks_path: Path = DEFAULT_CHUNKS_PATH,
    index_dir: Path = DEFAULT_INDEX_DIR,
) -> CachingIndexProvider:
    """A provider wired to the prototype's pickle corpus and hybrid index.

    ``api_key`` is needed only to *build* an index that is not already on disk,
    because building embeds the whole corpus. Opening a prebuilt one does not
    use it.
    """

    def load_corpus() -> Any:
        from Data.fetch_and_chunk import load_chunks

        return load_chunks(str(chunks_path))

    def verify_index() -> Any:
        # Raises on a hash or manifest mismatch, which is the whole point: the
        # service catches nothing here, so an unverifiable index becomes a typed
        # index_unverified failure with nothing generated.
        from synapse.retrieval.index_gate import check_index

        return check_index(index_dir)

    def open_index() -> Any:
        from Retrieval.hybrid_retriever import HybridRetriever

        return HybridRetriever.load(str(index_dir), fusion=FUSION_STRATEGY, alpha=FUSION_ALPHA)

    def build_index(chunks: Any) -> Any:
        from Retrieval.hybrid_retriever import HybridRetriever

        hybrid = HybridRetriever(fusion=FUSION_STRATEGY, alpha=FUSION_ALPHA)
        hybrid.build(chunks, api_key=api_key)
        return hybrid

    return CachingIndexProvider(
        load_corpus=load_corpus,
        verify_index=verify_index,
        open_index=open_index,
        build_index=build_index,
    )


__all__ = ["FUSION_ALPHA", "FUSION_STRATEGY", "legacy_index_provider"]

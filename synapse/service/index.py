"""
synapse.service.index
=====================
Loading the corpus and the retrieval index, once, in the right order.

This is the extraction of the first half of ``app.py``'s ``retrieve`` closure:
the part that read cached chunks and a cached retriever out of
``st.session_state``, built either if absent, and wrote the results back.

**Nothing here imports the legacy tree.** ``synapse`` must stay importable
without a vector-search stack, and ``tests/test_no_pickle_and_imports.py``
fails the build if any module in the package imports ``Data``, ``Retrieval``,
``Generation`` or ``Evaluation``. So this module takes the four loading steps as
injected callables and owns only the part worth testing: the *ordering*, the
caching, and the narrowness of the fallback. The legacy binding that supplies
those callables lives in ``legacy_index.py`` at the repository root, outside the
package, dated for removal with the rest of the prototype
(docs/retrieval-runtime.md §8).

That split is the same one :mod:`synapse.retrieval.production` already makes:
the package describes the shape it needs, and the un-migrated tree is passed in
rather than imported.

Two properties are load-bearing and are preserved exactly:

1. **The index gate runs before any retrieval.** An index whose contents do not
   match its manifest cannot be served from, because a citation would then point
   at a different document than the one that was read. ``verify_index`` raises,
   the exception propagates to :func:`~synapse.ui.pipeline.answer_turn`, and the
   turn becomes a typed ``index_unverified`` failure with nothing generated at
   all. It runs **before** ``open_index``, so an unverifiable index is never
   even opened.
2. **Falling back to a rebuild is narrow.** Only ``OSError``, ``ValueError`` and
   ``RuntimeError`` mean "no usable prebuilt index" -- ``RuntimeError`` because
   that is what FAISS raises for a missing index file. Anything else propagates
   and becomes a typed ``retrieval_failed`` outcome rather than being papered
   over by an expensive rebuild that would fail the same way.
"""

from __future__ import annotations  # Postponed annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from synapse.service.progress import ProgressEvent, ProgressReporter, ProgressStage, null_reporter

# Where the prototype's artifacts live by default. Both are relative to the
# working directory, as they were in app.py.
DEFAULT_CHUNKS_PATH = Path("processed_chunks.pkl")
DEFAULT_INDEX_DIR = Path("hybrid_index")

# The exceptions that mean "there is no usable prebuilt index", and nothing
# else. Kept as a named constant so widening it is a visible, reviewable act.
NO_PREBUILT_INDEX = (OSError, ValueError, RuntimeError)

# The four steps, as the package sees them. Typed as returning Any because the
# objects they produce are the untyped prototype's; pretending otherwise would
# be a fiction mypy could not check.
CorpusLoader = Callable[[], Any]  # -> the chunk corpus
IndexVerifier = Callable[[], Any]  # -> gate result; RAISES if contents != manifest
IndexOpener = Callable[[], Any]  # -> a retriever; raises if none is on disk
IndexBuilder = Callable[[Any], Any]  # corpus -> a freshly built retriever


@dataclass(frozen=True)
class LoadedIndex:
    """The corpus and the retriever, ready to search.

    ``gate`` is the index-gate result. ``app.py`` recorded it and never read it;
    it is kept because it is the evidence that verification actually ran, and
    the transparency page has a use for it.
    """

    chunks: Any  # The prototype's chunk objects; untyped by construction
    hybrid: Any  # The prototype's HybridRetriever
    gate: Any = None


class IndexProvider(Protocol):
    """Anything that can produce a :class:`LoadedIndex`.

    A protocol rather than a base class, so a test supplies a fake without
    importing FAISS and without inheriting from anything.
    """

    def load(self, report: ProgressReporter = ...) -> LoadedIndex:
        """Return the loaded corpus and retriever, building them if needed."""
        ...


@dataclass
class CachingIndexProvider:
    """Loads the corpus and retriever once, then returns the cached pair.

    The cache is instance state rather than session state: whatever holds the
    service holds the index. Locally that is Streamlit's session; in a server it
    is the process. Neither needs to know the difference.

    Not thread-safe by construction, and it does not need to be: a concurrent
    second load is wasteful but not incorrect, because both callers would
    produce an equivalent retriever over the same verified artifacts.
    """

    load_corpus: CorpusLoader
    verify_index: IndexVerifier
    open_index: IndexOpener
    build_index: IndexBuilder

    _chunks: Any = None
    _hybrid: Any = None
    _gate: Any = None

    @property
    def loaded(self) -> bool:
        """True once an index is cached, so a caller can report cheaply."""
        return self._hybrid is not None

    def load(self, report: ProgressReporter = null_reporter) -> LoadedIndex:
        """Load or return the cached corpus and retriever.

        Raises whatever the injected steps raise. Every exception here is
        classified by :func:`synapse.ui.errors.classify` into a typed failure
        code -- an integrity failure becomes ``index_unverified``, anything else
        ``retrieval_failed`` -- and neither produces medical content.
        """
        if not self._chunks:
            report(ProgressEvent(ProgressStage.LOADING_CORPUS))
            self._chunks = self.load_corpus()

        if self._hybrid is None:
            # Fail closed BEFORE the index is opened. See the module docstring.
            report(ProgressEvent(ProgressStage.VERIFYING_INDEX))
            self._gate = self.verify_index()

            report(ProgressEvent(ProgressStage.OPENING_INDEX))
            try:
                self._hybrid = self.open_index()
            except NO_PREBUILT_INDEX:
                # No prebuilt index on disk, or an unreadable one. Narrow on
                # purpose -- see the module docstring.
                report(ProgressEvent(ProgressStage.BUILDING_INDEX))
                self._hybrid = self.build_index(self._chunks)

        return LoadedIndex(chunks=self._chunks, hybrid=self._hybrid, gate=self._gate)


__all__ = [
    "DEFAULT_CHUNKS_PATH",
    "DEFAULT_INDEX_DIR",
    "NO_PREBUILT_INDEX",
    "CachingIndexProvider",
    "CorpusLoader",
    "IndexBuilder",
    "IndexOpener",
    "IndexProvider",
    "IndexVerifier",
    "LoadedIndex",
]

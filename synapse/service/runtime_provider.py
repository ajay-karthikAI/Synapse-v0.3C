"""
synapse.service.runtime_provider
================================
The seam between a **verified runtime artifact** and the application service.

``synapse.runtime.RuntimeIndex`` downloads an archive, checks its digest,
extracts it without ever calling ``extractall``, rebuilds the sparse index from
the verified corpus, and refuses to construct a dense backend whose vector count
or dimensionality disagrees with that corpus. What it produces is a
:class:`~synapse.runtime.readiness.RuntimeBackends` -- a dense and a sparse
backend that already satisfy the retrieval protocols.

``synapse.service`` asks for an :class:`~synapse.service.index.IndexProvider`.
Until this module existed, nothing joined the two: ``RuntimeIndex`` was
referenced only in tests and in docstrings, and ``serve_api.py`` could serve
only from the legacy prototype index. A container built from the locked
architecture -- one process, one pinned read-only artifact -- had no way to
start.

This is that join, and it is deliberately thin. It performs no retrieval, wraps
no backend, and derives no identifier. Both index shapes converge on the same
pair of protocols inside
:meth:`~synapse.service.retrieval.RetrievalService._backends`, so the deployed
artifact and the local prototype cannot answer the same question differently.

Readiness is not re-litigated here
----------------------------------
``RuntimeIndex`` owns the verdict and never raises from :meth:`start`; a failed
gate becomes a typed :class:`~synapse.runtime.readiness.Readiness`. This
provider only refuses to hand over backends that were never loaded, and it does
so with the runtime's own ``NotReadyError`` rather than inventing a second
vocabulary for the same condition.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass

from synapse.logging import get_logger
from synapse.runtime.readiness import RuntimeIndex
from synapse.service.index import LoadedIndex, RetrievalBackends
from synapse.service.progress import ProgressEvent, ProgressReporter, ProgressStage, null_reporter

logger = get_logger(__name__)


@dataclass(frozen=True)
class RuntimeIndexProvider:
    """An :class:`~synapse.service.index.IndexProvider` over a started runtime index.

    Holds the :class:`~synapse.runtime.readiness.RuntimeIndex` rather than the
    backends, so that a process can construct the provider during startup -- and
    register a health check that reports ``STARTING`` -- before the artifact has
    finished loading.

    There is no caching here and none is needed: ``RuntimeIndex`` loads once in
    :meth:`~synapse.runtime.readiness.RuntimeIndex.start` and holds the result,
    so :meth:`load` is a field read. That is the difference from
    :class:`~synapse.service.index.CachingIndexProvider`, which exists because
    the prototype's loader is expensive and re-entrant.
    """

    index: RuntimeIndex

    def load(self, report: ProgressReporter = null_reporter) -> LoadedIndex:
        """Return the artifact's backends, already typed.

        Raises:
            NotReadyError: the artifact has not finished loading, or a startup
                gate failed. A caller reaching retrieval without checking
                readiness has a bug, and it surfaces as one rather than as a
                quietly empty result set.
        """
        # OPENING_INDEX -- "loading a prebuilt index" -- which is precisely what a
        # pinned artifact is. Emitted for parity with the legacy provider, whose
        # own load genuinely reads from disk at this point. The stage enum is
        # closed and its message table frozen, so no query, artifact path or
        # bucket name can reach a progress indicator by construction.
        report(ProgressEvent(ProgressStage.OPENING_INDEX))

        backends = self.index.backends  # Raises NotReadyError unless ready
        return LoadedIndex(
            # The verified corpus, in place of the prototype's chunk objects.
            # Nothing in the service reads this; it is carried because the
            # transparency surface has a use for the counts.
            chunks=backends.corpus,
            # No prototype retriever exists on this path, and nothing may fall
            # back to one: `RetrievalService` reads `backends` first and only
            # touches `hybrid` when it is absent.
            hybrid=None,
            # The evidence that verification actually ran.
            gate=backends.artifact,
            backends=RetrievalBackends(dense=backends.dense, sparse=backends.sparse),
        )


__all__ = ["RuntimeIndexProvider"]

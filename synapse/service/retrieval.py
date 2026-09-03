"""
synapse.service.retrieval
=========================
One query in, one typed :class:`~synapse.retrieval.RetrievalBundle` out.

The second half of ``app.py``'s ``retrieve`` closure, extracted. The ordering
and the limits live in :mod:`synapse.retrieval`, not here; this module loads the
index, resolves a follow-up into a standalone search query, wraps the prototype
backends in their adapters, and hands over.

The returned bundle is typed, so no identifier is derived, repaired or dropped
anywhere downstream.

What is deliberately fragile-looking and is not
-----------------------------------------------
Both model calls on this path **fail open**, and neither can cost a patient
their turn:

* the query rewrite degrades to the raw query, which is always a valid thing to
  search for and is exactly the pre-rewrite behaviour;
* the rerank returns the fused retrieval order unchanged, because a ranking that
  could not be improved is still a usable ranking -- and every claim built on it
  is still verified by the answer layer regardless of what order it arrived in.

What must not move
------------------
The rewritten query is used for **retrieval only**. It never leaves this module:
:func:`~synapse.ui.pipeline.answer_turn` passes its own ``query`` to generation,
so the patient's raw words are what reach the prompt and what every claim is
verified against. Nothing about the generation contract changes, which is why
``synapse.answer.generate.PROMPT_ID`` does not move.
"""

from __future__ import annotations  # Postponed annotations

from collections.abc import Sequence

from synapse.memory.query_rewrite import TurnSummary
from synapse.retrieval import (
    DEFAULT_CANDIDATE_CONFIG,
    DEFAULT_RERANK_CONFIG,
    RetrievalBundle,
    bundle_from_candidates,
    rerank_candidates,
    search_candidates,
)
from synapse.service.clients import ClientFactory
from synapse.service.governance import EligibilityDecision
from synapse.service.index import IndexProvider
from synapse.service.progress import ProgressEvent, ProgressReporter, ProgressStage, null_reporter


class RetrievalService:
    """Builds the retrieval bundle for one turn.

    Callable, so it can be handed straight to
    :func:`~synapse.ui.pipeline.answer_turn` as its ``retrieve`` argument once
    the per-turn context is bound (see
    :meth:`~synapse.service.service.SynapseService.ask`).
    """

    def __init__(
        self,
        *,
        index: IndexProvider,
        clients: ClientFactory,
        api_key: str,
    ) -> None:
        self._index = index
        self._clients = clients
        # Held only to hand to the dense backend, which embeds the query itself.
        self._api_key = api_key

    def retrieve(
        self,
        query: str,
        *,
        history: Sequence[TurnSummary] = (),
        eligibility: EligibilityDecision,
        report: ProgressReporter = null_reporter,
    ) -> RetrievalBundle:
        """Search, rerank, and return a typed bundle.

        ``history`` is used **only** to resolve a follow-up into a standalone
        search query. It does not reach the emergency detector and it does not
        reach generation.
        """
        from synapse.retrieval.production import LegacyDenseBackend, LegacySparseBackend

        loaded = self._index.load(report)

        # Resolve a context-dependent follow-up ("what about the side effects?")
        # into a standalone query, so BM25 and FAISS see the whole question
        # rather than six context-free words.
        report(ProgressEvent(ProgressStage.UNDERSTANDING_QUESTION))
        retrieval_query, was_rewritten = self._rewrite(query, history)

        report(ProgressEvent(ProgressStage.SEARCHING))
        # Bounded on both sides. Neither count is derived from the corpus size:
        # the prototype asked FAISS for top_k=len(chunks) and BM25 for a score
        # per chunk, on every query.
        retrieval = search_candidates(
            retrieval_query,
            dense=LegacyDenseBackend(loaded.hybrid.vector_store, self._api_key),
            sparse=LegacySparseBackend(loaded.hybrid.bm25_index),
            config=DEFAULT_CANDIDATE_CONFIG,
            eligible_documents=eligibility.document_ids,
        )

        report(ProgressEvent(ProgressStage.RANKING))
        # ONE request for the whole candidate set, with its own timeouts,
        # deadline and bounded retries. On failure this returns the fused
        # retrieval order unchanged rather than raising.
        outcome = rerank_candidates(
            retrieval_query,
            retrieval.candidates,
            self._clients.for_rerank(),
            DEFAULT_RERANK_CONFIG,
        )

        report(ProgressEvent(ProgressStage.PREPARING_ANSWER))
        return bundle_from_candidates(
            outcome.candidates,
            metadata={
                "retrieval": retrieval.trace.as_metadata(),
                "rerank": outcome.as_metadata(),
                # A BOOLEAN, and nothing else. Neither the original nor the
                # rewritten query, nor any substring of either, is recorded: a
                # rewritten query is still the patient's question
                # (docs/privacy-logging-policy.md).
                "query_rewritten": was_rewritten,
                # Flat so it reaches the "turn rendered" log line; the nested
                # retrieval trace carries the same fact as eligibility_enforced.
                "governed": eligibility.enforced,
                # Nested ON PURPOSE. answer_turn's "turn rendered" log line
                # spreads every NON-dict metadata value into its extra={}, so a
                # bare string here would put the patient's question into the
                # logs -- the one thing the telemetry policy forbids. Nested, it
                # is skipped by that comprehension and ignored by
                # _record_retrieval_shape, while still reaching the renderer
                # through AnswerPresentation.conversion.
                "rewrite": {"resolved_query": retrieval_query if was_rewritten else ""},
            },
        )

    def _rewrite(self, query: str, history: Sequence[TurnSummary]) -> tuple[str, bool]:
        """Resolve a follow-up for retrieval. Degrades to the raw query.

        ``rewrite_query`` catches everything internally and is tested for it, so
        the guard below should be unreachable -- but this runs inside the
        closure ``answer_turn`` wraps in its own retrieval try/except, and there
        an escaping exception becomes a ``retrieval_failed`` card. That would
        cost the patient the whole turn to save them a query rewrite.
        """
        from synapse.memory.query_rewrite import rewrite_query

        try:
            return rewrite_query(query, history, self._clients.for_rerank())
        # Broad on purpose, and belt-and-braces: see the docstring.
        except Exception:
            return query, False


__all__ = ["RetrievalService"]

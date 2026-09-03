"""
synapse.service.service
=======================
The application service: one question in, one recorded turn out.

This is what ``app.py``'s submit handler was, minus Streamlit. It owns nothing
that decides *what* a patient sees -- that is still
:mod:`synapse.ui.pipeline` and the answer layer beneath it -- and everything
about *assembling a turn*: resolving governance, snapshotting conversation
context, binding the retrieval closure, supplying the two injection points, and
recording the result.

Why this exists
---------------
There is about to be a second caller. A FastAPI process and the Streamlit
fallback must run the *same* turn, or the local interface stops being evidence
about the deployed one. Every property below was previously a closure inside a
1,900-line script, reachable only by running Streamlit:

* the emergency latch window,
* which turns are eligible as retrieval context,
* the provenance stamps telemetry records,
* the order the two model calls happen in.

None of them changed in the move. What changed is that they are now typed,
type-checked, and exercised offline by ``tests/test_service.py`` against fakes.

What this module must never acquire
-----------------------------------
A branch that renders model prose on a failure path. There is none here, and
there is none below: :func:`~synapse.ui.pipeline.answer_turn` returns a
:class:`~synapse.ui.pipeline.TurnOutcome` that is exclusively a validated
presentation or a typed failure, and this module passes it through untouched.
"""

from __future__ import annotations  # Postponed annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from synapse.answer.generate import PROMPT_ID as ANSWER_PROMPT_ID
from synapse.answer.policy import DisplayPolicy
from synapse.answer.providers import DEFAULT_MODEL
from synapse.retrieval import RetrievalBundle
from synapse.retrieval.rerank import PROMPT_ID as RERANK_PROMPT_ID
from synapse.service.clients import ClientFactory, OpenAIClientFactory
from synapse.service.conversation import (
    DEFAULT_ESCALATION_WINDOW,
    DEFAULT_HISTORY_LIMIT,
    Conversation,
    ConversationTurn,
)
from synapse.service.governance import (
    DEFAULT_SOURCE_PACK,
    log_eligibility,
    resolve_eligible_documents,
)
from synapse.service.index import (
    DEFAULT_CHUNKS_PATH,
    DEFAULT_INDEX_DIR,
    IndexProvider,
)
from synapse.service.progress import ProgressEvent, ProgressReporter, ProgressStage, null_reporter
from synapse.service.retrieval import RetrievalService
from synapse.telemetry.recorder import TelemetryRecorder
from synapse.ui.pipeline import Retriever, TurnOutcome, answer_turn

# The red-flag check: query in, "is this an emergency" out. Named here so the
# injection point is visible in the signature rather than buried in a default.
EmergencyCheck = Callable[[str], bool]


class MissingCredentialError(RuntimeError):
    """No API key was configured.

    A deployment fault, not a patient-facing one. The caller renders the fixed
    failure copy with the ``configuration_error`` code; this exception's message
    is for an operator and is never shown.
    """


@dataclass(frozen=True)
class ServiceConfig:
    """Everything the service needs that is not injected.

    Frozen: a turn must not be able to change where the index is read from or
    which model answers, and a shared server instance must not be mutable by a
    request.
    """

    api_key: str
    source_pack: Path = DEFAULT_SOURCE_PACK
    chunks_path: Path = DEFAULT_CHUNKS_PATH
    index_dir: Path = DEFAULT_INDEX_DIR
    answer_model: str = DEFAULT_MODEL
    history_limit: int = DEFAULT_HISTORY_LIMIT
    escalation_window: int = DEFAULT_ESCALATION_WINDOW
    # Provenance stamps. Recorded by telemetry, never shown to a patient. Empty
    # is the honest default: the prototype's artifacts carry no version yet.
    provider: str = "openai"
    corpus_version: str = ""
    index_version: str = ""

    @classmethod
    def from_environment(cls) -> ServiceConfig:
        """Read the configuration from the process environment.

        The key is **server-side only**. It is read here, from the environment,
        and never accepted from a request, a form field or a header -- which is
        why the Streamlit sidebar no longer has a place to type one.
        """
        return cls(
            api_key=os.getenv("OPENAI_API_KEY", ""),
            source_pack=Path(os.getenv("SYNAPSE_SOURCE_PACK", str(DEFAULT_SOURCE_PACK))),
            chunks_path=Path(os.getenv("SYNAPSE_CHUNKS_PATH", str(DEFAULT_CHUNKS_PATH))),
            index_dir=Path(os.getenv("SYNAPSE_INDEX_DIR", str(DEFAULT_INDEX_DIR))),
        )

    def require_credentials(self) -> None:
        """Raise if the deployment cannot generate an answer at all."""
        if not self.api_key:
            raise MissingCredentialError("OPENAI_API_KEY is not set; the answer path cannot run")


@dataclass
class SynapseService:
    """Runs one turn, framework-neutral.

    Construct with :meth:`from_config` for production, or directly with fakes
    for a test. Every collaborator is injected, so the whole path -- including
    every failure branch -- runs offline without an API key.
    """

    config: ServiceConfig
    clients: ClientFactory
    index: IndexProvider
    is_emergency: EmergencyCheck
    telemetry: TelemetryRecorder = field(default_factory=TelemetryRecorder.disabled)
    policy: DisplayPolicy | None = None
    # Replaces the whole retrieval stage. Production leaves this None and gets
    # RetrievalService over the injected index; a test supplies a bundle
    # directly, which is how the golden states are produced without FAISS, an
    # embedding call, or a corpus on disk.
    #
    # It takes the same shape answer_turn's own ``retrieve`` argument does, so
    # substituting it cannot change the contract -- only where the bundle comes
    # from.
    retriever: Retriever | None = None

    @classmethod
    def from_config(
        cls,
        config: ServiceConfig,
        *,
        index: IndexProvider,
        telemetry: TelemetryRecorder | None = None,
    ) -> SynapseService:
        """The production wiring: real clients, real detector, injected index.

        ``index`` is a parameter rather than something this method builds
        because the only index that exists today is the prototype's, and
        ``synapse`` may not import the prototype
        (tests/test_no_pickle_and_imports.py). The caller passes
        ``legacy_index.legacy_index_provider(...)``; when the retrieval tree
        emits typed records, it will pass something else and nothing in this
        package will need to change.
        """
        config.require_credentials()
        from synapse.safety import load_detector

        return cls(
            config=config,
            clients=OpenAIClientFactory(api_key=config.api_key, answer_model=config.answer_model),
            index=index,
            is_emergency=load_detector().is_emergency,
            telemetry=telemetry or TelemetryRecorder.disabled(),
        )

    def ask(
        self,
        query: str,
        conversation: Conversation,
        *,
        report: ProgressReporter = null_reporter,
    ) -> ConversationTurn:
        """Run one turn and append it to ``conversation``.

        Appending here rather than at the call site is deliberate: the emergency
        latch reads the conversation, so a caller that forgot to record a turn
        would silently release a red flag. One method owns both halves.

        Never raises for an expected failure. A provider outage, a malformed
        generation, an unverifiable index and an empty retrieval all come back
        as typed failures inside the outcome.
        """
        report(ProgressEvent(ProgressStage.CHECKING_QUESTION))

        # Resolved on the calling thread, because it reads files and the
        # retrieval closure may run on a worker.
        eligibility = resolve_eligible_documents(self.config.source_pack)
        log_eligibility(eligibility)

        # Snapshotted before the closure is built, for the same reason: the
        # closure must not reach back into caller-owned state it may not be able
        # to read from another thread.
        history = conversation.retrieval_history(self.config.history_limit)
        questions = conversation.question_history(self.config.history_limit)
        recent_escalation = conversation.recent_escalation(self.config.escalation_window)

        retrieve: Retriever
        if self.retriever is not None:
            retrieve = self.retriever
        else:
            retrieval = RetrievalService(
                index=self.index, clients=self.clients, api_key=self.config.api_key
            )

            def retrieve(user_query: str) -> RetrievalBundle:
                """Bound to this turn's context. Handed to answer_turn as-is."""
                return retrieval.retrieve(
                    user_query,
                    history=history,
                    eligibility=eligibility,
                    report=report,
                )

        # One call. The ordering that carries the safety properties -- emergency
        # check, then retrieval, then schema-constrained generation, then
        # verification, then the display decision -- lives in
        # synapse.ui.pipeline, not here.
        outcome: TurnOutcome = answer_turn(
            query,
            retrieve=retrieve,
            client=self.clients.for_answer(),
            is_emergency=self.is_emergency,
            policy=self.policy,
            # Prior patient QUESTIONS only. Not the prior answers: an answer
            # summary is not in the passages, so a model quoting one would cite
            # evidence that was never retrieved and lose the claim.
            #
            # This does not reach is_emergency, which still sees the raw query
            # alone, and it does not reach retrieve.
            history=questions,
            recent_escalation=recent_escalation,
            telemetry=self.telemetry,
            provenance=self.provenance(),
        )

        report(ProgressEvent(ProgressStage.ANSWER_READY if outcome.ok else ProgressStage.NO_ANSWER))

        turn = ConversationTurn(query=query, outcome=outcome)
        conversation.append(turn)
        return turn

    def provenance(self) -> dict[str, str]:
        """Version stamps for telemetry. Counts and identifiers only, no content.

        The prompt ids travel with the turn so a wording change is visible in
        telemetry even when the schema is unchanged.
        """
        return {
            "provider": self.config.provider,
            "model": self.config.answer_model,
            "prompt_version": ANSWER_PROMPT_ID,
            "rerank_prompt_version": RERANK_PROMPT_ID,
            "corpus_version": self.config.corpus_version,
            "index_version": self.config.index_version,
        }


__all__ = [
    "EmergencyCheck",
    "MissingCredentialError",
    "ServiceConfig",
    "SynapseService",
]

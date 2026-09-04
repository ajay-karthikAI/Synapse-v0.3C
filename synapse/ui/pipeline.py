"""
synapse.ui.pipeline
===================
One query in, one renderable outcome out.

This is the seam between the Streamlit application and the answer layer. It
exists so that ``app.py`` contains presentation only: the ordering that carries
the safety properties lives here, in a module that is typed, linted and tested,
rather than inside a script that is none of those.

The ordering, which is the whole point:

    emergency check -> retrieve -> convert to typed evidence
        -> generate (schema-constrained) -> verify -> decide -> render

Three invariants hold at every step:

1. **Emergency routing runs first and short-circuits.** It is decided before
   retrieval, exactly as in P1, and no generation happens on that path.
2. **Every failure is typed, and no failure produces medical content.** There is
   no branch anywhere below that renders model prose when validation fails. A
   failed turn yields an :class:`~synapse.ui.errors.AnswerFailure`, which the
   interface renders as a fixed message plus a code.
3. **The outcome is either an answer or a failure, never both and never
   neither.** :class:`TurnOutcome` makes the two states exclusive, so a caller
   cannot forget to check.

Retrieval and generation are injected rather than imported. That keeps the
network out of the tests and lets the whole path — including the failure
branches — run offline against fakes.

Telemetry is recorded here because this is the only place that sees the whole
request: the stage boundaries, the version stamps, the outcome and the failure
code. It records **counts, durations, versions and typed codes — never the
query, the answer, or any excerpt** (docs/privacy-logging-policy.md). It is
optional and defaults to a disabled recorder, and every telemetry call is
already fail-safe, so an observability fault cannot cost a patient an answer.
"""

from __future__ import annotations  # Postponed annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from synapse.answer.generate import AnswerResult, GenerationClient, answer_query
from synapse.answer.policy import DisplayPolicy
from synapse.answer.render import SourceNumbering
from synapse.answer.schema import AnswerAction, GroundedAnswer
from synapse.answer.verify import RetrievedEvidence
from synapse.logging import get_logger
from synapse.retrieval.evidence import RetrievalBundle
from synapse.telemetry.recorder import RequestTelemetry, TelemetryRecorder
from synapse.telemetry.schema import ActionResult, StatusClass
from synapse.ui.errors import AnswerFailure, AnswerFailureCode, classify
from synapse.ui.legacy_evidence import ConversionResult, evidence_from_reranked

logger = get_logger(__name__)

# A retriever returns either a typed bundle (synapse.retrieval) or the legacy
# list of reranked dicts, which the deprecated adapter converts. The typed form
# is the one new code uses; the list form is dated for removal.
Retriever = Callable[[str], "list[Any] | RetrievalBundle"]
EmergencyCheck = Callable[[str], bool]  # query -> is this a red flag


@dataclass(frozen=True)
class AnswerPresentation:
    """A successful turn, in the form the interface renders.

    Carries the numbering alongside the answer deliberately: every surface —
    inline citations, the source panel, the evidence cards and the exported
    brief — must use the *same* map, and passing them together is what makes
    that impossible to get wrong.
    """

    result: AnswerResult
    numbering: SourceNumbering
    conversion: ConversionResult | RetrievalBundle | None = (
        None  # Retrieval diagnostics, typed or legacy
    )

    @property
    def answer(self) -> GroundedAnswer:
        """The answer as it will be displayed, after withholding."""
        return self.result.answer

    @property
    def action(self) -> AnswerAction:
        """The action state to render."""
        return self.result.answer.action


@dataclass(frozen=True)
class TurnOutcome:
    """Either a presentation or a failure. Never both."""

    presentation: AnswerPresentation | None = None
    failure: AnswerFailure | None = None

    def __post_init__(self) -> None:
        """Reject the two states that would let a caller render nothing, or guess."""
        if (self.presentation is None) == (self.failure is None):
            raise ValueError("a turn outcome must carry exactly one of presentation or failure")

    @property
    def ok(self) -> bool:
        """True when there is an answer to render."""
        return self.presentation is not None


def emergency_turn() -> TurnOutcome:
    """The outcome for a query the red-flag detector escalated.

    Produced without retrieval or generation, so nothing can go wrong between
    detection and display. The numbering is empty because no evidence was
    consulted — an emergency card cites nothing.
    """
    result = answer_query(
        query="",  # Not used on this path; generation is short-circuited
        evidence=_EMPTY_EVIDENCE,
        source_order=[],
        client=_UNREACHABLE_CLIENT,
        is_emergency=True,
    )
    return TurnOutcome(presentation=AnswerPresentation(result=result, numbering=SourceNumbering()))


def answer_turn(
    query: str,
    *,
    retrieve: Retriever,
    client: GenerationClient,
    is_emergency: EmergencyCheck,
    policy: DisplayPolicy | None = None,
    telemetry: TelemetryRecorder | None = None,
    provenance: dict[str, str] | None = None,
    history: Sequence[str] = (),
    recent_escalation: bool = False,
) -> TurnOutcome:
    """Run one query end to end and return something renderable.

    Never raises for an expected failure: a provider outage, a malformed
    generation and an empty retrieval all come back as typed failures, because
    the interface has to render *something* and that something must never be
    unvalidated medical text.

    ``telemetry`` and ``provenance`` are optional. Omitted, the turn behaves
    exactly as before and records nothing.

    ``history`` is earlier patient questions, passed to generation as context for
    resolving a follow-up. It is NOT given to ``is_emergency``: the red-flag
    check runs on the raw query alone, exactly as before, so the detector's
    negation scoping cannot be confused by text from another turn. It is also not
    given to ``retrieve``; the retrieval-side rewrite is the caller's business
    and is independent of this.

    ``recent_escalation`` latches a red flag across turns. A patient who
    described crushing chest pain and then asked "is that serious?" was
    escalated on the first turn and answered normally on the second: the
    follow-up carries no emergency vocabulary of its own, so the detector --
    which sees one query at a time -- had nothing to fire on.

    A latch rather than a concatenation, deliberately. Joining the turns into one
    string and re-running the detector was measured against this repository's own
    corpus: negation survived it (0 flips in 24), but it invented emergencies in
    3 of 5 benign pairs, because the proximity window pairs stems across the
    join -- "what does a chest x-ray show" followed by "i have pain in my knee"
    escalates on chest+pain, and neither turn is an emergency. A latch
    re-tokenizes nothing, so it cannot manufacture a match or move a negation's
    scope; it only remembers a verdict the detector already reached on the
    patient's own words.

    It is an OR, never a replacement: the raw-query check runs exactly as before
    and a denial that did not escalate leaves the latch unset.
    """
    recorder = telemetry or TelemetryRecorder.disabled()
    request = recorder.request(**(provenance or {}))

    try:
        with request.stage("safety_check"):
            # OR, so a latched red flag cannot be cleared by a benign follow-up,
            # and the detector still runs on the patient's own words every turn.
            escalate = is_emergency(query) or bool(recent_escalation)
    # Broad on purpose: a detector fault must not become a silent non-escalation.
    except Exception as exc:
        # Fail closed, loudly. The failure card tells the patient to speak to
        # staff, which is the safe direction when the red-flag check is broken.
        logger.error("emergency detection failed", extra={"error": type(exc).__name__})
        failure = classify(exc)
        _record_failure(request, failure, exc)
        return TurnOutcome(failure=failure)
    if escalate:
        logger.info("emergency routing fired", extra={"action": AnswerAction.EMERGENCY.value})
        request.set_outcome(ActionResult.EMERGENCY)
        request.emit()
        return emergency_turn()

    try:
        with request.stage("retrieval"):
            retrieved = retrieve(query)
    # Broad on purpose: retrieval spans FAISS, BM25 and an embedding API.
    except Exception as exc:
        # An index that failed VERIFICATION is not an ordinary retrieval error
        # and must not be reported as one. "BM25 raised" and "the index did not
        # match its manifest" have different causes, different operator
        # responses, and different consequences: the second means a citation
        # could point at a different document than the one that was read, which
        # is the failure the index gate exists to prevent. Collapsing both onto
        # `retrieval_failed` made an integrity failure indistinguishable from a
        # transient one, in the logs and on the screen.
        #
        # Only that one code is preserved from `classify`. Everything else stays
        # `retrieval_failed`, because a provider error raised during retrieval
        # should not surface as, say, `generation_unavailable`.
        classified = classify(exc)
        failure = (
            classified
            if classified.code is AnswerFailureCode.INDEX_UNVERIFIED
            else AnswerFailure(AnswerFailureCode.RETRIEVAL_FAILED, type(exc).__name__)
        )
        logger.warning("retrieval failed", extra=failure.as_dict())
        _record_failure(request, failure, exc)
        return TurnOutcome(failure=failure)

    conversion: RetrievalBundle | ConversionResult
    if isinstance(retrieved, RetrievalBundle):
        # The typed path: identifiers are already stable, so nothing is derived,
        # repaired or dropped on the way in.
        conversion = retrieved
        retrieval_metadata = retrieved.metadata
    else:
        # The deprecated path, for the un-migrated legacy retrieval stack.
        conversion = evidence_from_reranked(retrieved)
        retrieval_metadata = {"adapter": "legacy_evidence"}

    if conversion.is_empty:
        # Nothing usable was retrieved, so there is nothing to ground an answer
        # in. This is a failure rather than an abstention: an abstention is a
        # statement about the evidence, and here the evidence never arrived.
        dropped = getattr(conversion, "dropped_unidentifiable", 0) + getattr(
            conversion, "dropped_duplicate_ids", 0
        )
        logger.info("no usable evidence after conversion", extra={"dropped": dropped})
        failure = AnswerFailure(AnswerFailureCode.EVIDENCE_UNAVAILABLE, f"dropped={dropped}")
        _record_failure(request, failure)
        return TurnOutcome(failure=failure)

    _record_retrieval_shape(request, conversion, retrieval_metadata)

    try:
        with request.stage("generation"):
            result = answer_query(
                query,
                conversion.evidence,
                conversion.source_order,
                client,
                policy=policy,
                history=history,
            )
    # Broad on purpose: everything below is classified into a typed code.
    except Exception as exc:
        failure = classify(exc)
        # Logged with the code, never with the provider's message or the query.
        logger.warning("answer generation failed", extra=failure.as_dict())
        _record_failure(request, failure, exc)
        return TurnOutcome(failure=failure)

    numbering = SourceNumbering.from_evidence(
        conversion.evidence, conversion.source_order, conversion.relevance
    )
    logger.info(
        "turn rendered",
        extra={
            "action": result.answer.action.value,
            "claims_shown": len(result.answer.claims),
            "claims_withheld": len(result.decision.withheld_claim_ids),
            "sources": len(numbering.refs),
            **{
                f"retrieval_{k}": v
                for k, v in retrieval_metadata.items()
                if not isinstance(v, dict)
            },
        },
    )
    request.claims_shown = len(result.answer.claims)
    request.claims_withheld = len(result.decision.withheld_claim_ids)
    request.set_outcome(ActionResult(result.answer.action.value))
    request.emit()

    return TurnOutcome(
        presentation=AnswerPresentation(result=result, numbering=numbering, conversion=conversion)
    )


class _UnreachableClient:
    """A generation client that must never be called.

    Used on the emergency path, where :func:`synapse.answer.generate.answer_query`
    short-circuits before generation. If the short-circuit is ever removed, this
    raises instead of quietly calling a provider on an escalated query.
    """

    def generate_structured(self, *, system: str, user: str, schema: dict) -> str:
        raise AssertionError("generation must not run on the emergency path")


_UNREACHABLE_CLIENT = _UnreachableClient()

# An emergency answer cites nothing, so the evidence map is empty.
_EMPTY_EVIDENCE = RetrievedEvidence(chunk_texts={}, source_ids=frozenset())


def _record_failure(
    request: RequestTelemetry, failure: AnswerFailure, exc: BaseException | None = None
) -> None:
    """Record a failed turn: typed code, exception type, status class.

    Never the exception's message and never a traceback — the message can quote
    the prompt, which contains the patient's question
    (docs/privacy-logging-policy.md §2.9).
    """
    status = None
    if exc is not None:
        raw_status = getattr(exc, "details", {}).get("status") if hasattr(exc, "details") else None
        status = _status_class(raw_status, exc)
    request.set_outcome(
        ActionResult.FAILURE,
        failure_code=failure.code.value,
        exception_type=type(exc).__name__ if exc is not None else "",
        status_class=status,
    )
    request.emit()


def _status_class(raw_status: object, exc: BaseException) -> StatusClass | None:
    """Map a provider status to its class, or a transport failure to its kind."""
    if isinstance(raw_status, int):
        if raw_status == 429:
            return StatusClass.RATE_LIMITED
        if 200 <= raw_status < 300:
            return StatusClass.OK
        if 400 <= raw_status < 500:
            return StatusClass.CLIENT_ERROR
        if raw_status >= 500:
            return StatusClass.SERVER_ERROR
    if isinstance(exc, TimeoutError):
        return StatusClass.TIMEOUT
    return None


def _record_retrieval_shape(
    request: RequestTelemetry,
    conversion: RetrievalBundle | ConversionResult,
    metadata: dict[str, object],
) -> None:
    """Record the *shape* of retrieval: counts and strategy, never identifiers.

    ``evidence_count`` is a number. The chunk identifiers behind it are not
    recorded, because an identifier naming a rare-disease paper narrows the
    query as effectively as the query would (docs/privacy-logging-policy.md
    §2.4).
    """
    request.evidence_count = len(conversion.evidence.chunk_texts)
    retrieval = metadata.get("retrieval")
    if isinstance(retrieval, dict):
        union = retrieval.get("union_size")
        if isinstance(union, int):
            request.candidate_count = union
        config = retrieval.get("config")
        if isinstance(config, dict):
            request.retrieval_strategy = str(config.get("fusion", ""))
    rerank = metadata.get("rerank")
    if isinstance(rerank, dict):
        request.degraded = bool(rerank.get("degraded", False))
        attempts = rerank.get("attempts")
        if isinstance(attempts, int):
            request.retry_count = max(0, attempts - 1)
        calls = rerank.get("model_calls")
        if isinstance(calls, int):
            request.model_calls += calls
        tokens = rerank.get("prompt_tokens_estimated")
        if isinstance(tokens, int):
            request.input_tokens += tokens


__all__ = [
    "AnswerPresentation",
    "EmergencyCheck",
    "Retriever",
    "TurnOutcome",
    "answer_turn",
    "emergency_turn",
]

"""
synapse.api.envelopes
=====================
One :class:`~synapse.ui.pipeline.TurnOutcome` in, one envelope out.

The single place a Python outcome becomes a wire response. It is deliberately
the *only* such place, because the mapping encodes decisions that must not be
made twice and differently:

* which failure codes are a **designed state** (no verified evidence) rather
  than a fault, and therefore get the insufficient card instead of the error
  card. That set is two codes, and it mirrors ``app.py``'s
  ``EVIDENCE_FAILURE_CODES`` exactly — the Streamlit fallback and the API must
  render the same failure the same way or the local interface stops being
  evidence about the deployed one;
* that an **abstention is the insufficient state**, shown as an answer envelope
  carrying an ``insufficient`` block, because the questions survive even when
  the claims do not;
* that an **escalation cites nothing** and offers no brief;
* that a **failure carries only a code and fixed copy**.

Nothing here reads model output. Every string it emits is either a validated
field of a ``GroundedAnswer`` that already passed verification, or an
application constant. There is no branch that stringifies an exception.
"""

from __future__ import annotations  # Postponed annotations

from typing import Literal

from synapse.answer.render import (
    EMERGENCY_MESSAGE,
    PERMANENT_DISCLAIMER,
    STAFF_MESSAGE,
    SourceNumbering,
)
from synapse.answer.schema import AnswerAction, GroundedAnswer, SupportStatus
from synapse.api.models import (
    AnswerEnvelope,
    ClaimModel,
    EmergencyEnvelope,
    ExcerptModel,
    FailureEnvelope,
    InsufficientEnvelope,
    InsufficientModel,
    SourceModel,
)
from synapse.evidence import InsufficientEvidence, InsufficientReason, from_failure_code
from synapse.service.conversation import ConversationTurn
from synapse.ui.errors import PATIENT_ERROR_MESSAGE

# Failure codes that mean "no verified evidence" rather than "the tool broke".
# Anything else is a fault and gets the failure envelope. Mirrors app.py.
EVIDENCE_FAILURE_CODES = frozenset({"evidence_unavailable", "index_unverified"})


def envelope_for(
    turn: ConversationTurn,
    *,
    turn_index: int,
    pack_id: str = "",
    pack_version: str = "",
) -> AnswerEnvelope | EmergencyEnvelope | InsufficientEnvelope | FailureEnvelope:
    """Convert a completed turn into exactly one envelope."""
    outcome = turn.outcome

    if outcome.failure is not None:
        code = outcome.failure.code.value
        if code in EVIDENCE_FAILURE_CODES:
            return InsufficientEnvelope(
                detail=_insufficient(from_failure_code(code), pack_id, pack_version),
                turn_index=turn_index,
                disclaimer=PERMANENT_DISCLAIMER,
            )
        return FailureEnvelope(
            code=code,
            # The same message for every code, from a constant. Never composed.
            message=PATIENT_ERROR_MESSAGE,
            turn_index=turn_index,
        )

    presentation = outcome.presentation
    if presentation is None:  # pragma: no cover - TurnOutcome forbids this state
        raise AssertionError("a successful outcome must carry a presentation")

    answer = presentation.answer
    numbering = presentation.numbering

    if answer.action is AnswerAction.EMERGENCY:
        return EmergencyEnvelope(
            message=EMERGENCY_MESSAGE,
            turn_index=turn_index,
            disclaimer=PERMANENT_DISCLAIMER,
        )

    return AnswerEnvelope(
        action=_action_of(answer),
        summary=answer.summary,
        claims=_claims(answer, numbering),
        doctor_evaluation=answer.doctor_evaluation,
        questions_for_doctor=list(answer.questions_for_doctor),
        limitations=list(answer.limitations),
        sources=_sources(numbering),
        excerpts=_excerpts(answer, numbering),
        staff_message=STAFF_MESSAGE if answer.action is AnswerAction.MEDICAL_STAFF else "",
        # An abstention IS the insufficient state: the display policy withheld
        # every claim, so the page says so in those words rather than showing an
        # answer card with nothing in it.
        insufficient=(
            _insufficient(InsufficientReason.BELOW_THRESHOLD, pack_id, pack_version)
            if answer.action is AnswerAction.ABSTAIN
            else None
        ),
        resolved_query=resolved_query(turn),
        # An escalation is the only answered state with no brief; it never
        # reaches here, so anything that does is eligible.
        brief_available=True,
        turn_index=turn_index,
        disclaimer=PERMANENT_DISCLAIMER,
    )


def resolved_query(turn: ConversationTurn) -> str:
    """The rewritten retrieval query for this turn, or ``""``.

    Defensive at every step, exactly as ``app.py`` is: the turn may be a
    failure, may predate this field, or may have come through the legacy
    conversion path, and a missing disclosure must never cost a patient their
    answer.
    """
    try:
        presentation = turn.outcome.presentation
        if presentation is None:
            return ""
        metadata = getattr(presentation.conversion, "metadata", None)
        if not isinstance(metadata, dict):
            return ""
        rewrite = metadata.get("rewrite")
        if not isinstance(rewrite, dict):
            return ""
        resolved = rewrite.get("resolved_query")
        return resolved if isinstance(resolved, str) else ""
    # Broad on purpose: a disclosure is a nicety, an answer is not.
    except Exception:
        return ""


def _action_of(answer: GroundedAnswer) -> Literal["answer", "abstain", "medical_staff"]:
    """Map to the three actions an answer envelope can carry.

    Exhaustive and fail-closed rather than ``answer.action.value``: emergency is
    handled before this is reached, and if a fifth action is ever added, this
    raises instead of widening the envelope's contract by accident.
    """
    match answer.action:
        case AnswerAction.ANSWER:
            return "answer"
        case AnswerAction.ABSTAIN:
            return "abstain"
        case AnswerAction.MEDICAL_STAFF:
            return "medical_staff"
        case _:  # pragma: no cover - emergency is routed above
            raise AssertionError(f"unmapped answer action: {answer.action}")


def _claims(answer: GroundedAnswer, numbering: SourceNumbering) -> list[ClaimModel]:
    """Displayed claims only. Withheld claims were removed by the display policy."""
    return [
        ClaimModel(
            claim_id=claim.claim_id,
            text=claim.text,
            source_numbers=[
                numbering.refs[source_id].number
                for source_id in claim.source_ids
                if source_id in numbering.refs
            ],
            support=(
                "partially_supported"
                if claim.support_status is SupportStatus.PARTIALLY_SUPPORTED
                else "supported"
            ),
        )
        for claim in answer.claims
    ]


def _sources(numbering: SourceNumbering) -> list[SourceModel]:
    """The numbered source panel, in display order."""
    return [
        SourceModel(
            number=ref.number,
            source_id=ref.source_id,
            title=ref.title,
            # Dropped unless http(s): escaping a javascript: URL still yields a
            # working link, so the scheme is filtered rather than escaped.
            url=ref.url if ref.url.startswith(("http://", "https://")) else "",
            relevance=ref.relevance_score,
        )
        for ref in numbering.ordered()
    ]


def _excerpts(answer: GroundedAnswer, numbering: SourceNumbering) -> list[ExcerptModel]:
    """The verbatim spans the verifier matched, under the same source numbers."""
    excerpts: list[ExcerptModel] = []
    for claim in answer.claims:
        for excerpt in claim.supporting_excerpts:
            ref = numbering.refs.get(excerpt.source_id)
            if ref is None:
                continue  # Never number a source that was not retrieved
            excerpts.append(
                ExcerptModel(
                    source_number=ref.number, chunk_id=excerpt.chunk_id, quote=excerpt.quote
                )
            )
    return excerpts


def _insufficient(reason: InsufficientReason, pack_id: str, pack_version: str) -> InsufficientModel:
    """The full patient-facing content, from application constants only."""
    state = InsufficientEvidence(reason, pack_id=pack_id, pack_version=pack_version)
    return InsufficientModel(
        reason=state.reason.value,
        heading=state.heading,
        message=state.message,
        not_a_judgement=state.not_a_judgement,
        next_steps=list(state.next_steps),
    )


__all__ = ["EVIDENCE_FAILURE_CODES", "envelope_for", "resolved_query"]

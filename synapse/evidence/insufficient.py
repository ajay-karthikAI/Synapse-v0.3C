"""
synapse.evidence.insufficient
=============================
"We do not have enough verified information" — as a designed state, not a gap.

Six different things can leave the system without usable evidence, and they are
genuinely different **operationally**: nothing retrieved, retrieval that failed
validation, evidence below the sufficiency threshold, sources that conflict, a
question outside the pack's scope, and an index that would not verify. An
operator needs to tell them apart.

A patient does not. To a patient they are one situation — *this tool cannot
answer your question* — and the wording differs only in the sentence that says
why, which is written to be true without being alarming.

The hard rule this module exists to enforce
-------------------------------------------
**The gap is never filled with general model knowledge.** There is no field on
any object here that can carry generated medical text, and the copy is composed
from module constants. A model cannot contribute a sentence to this state, which
is the only way to guarantee that "we could not verify anything" is not followed
by an unverified paragraph.

Two wording rules, both non-obvious and both load-bearing:

* **Never imply that absence of evidence is absence of disease.** "We found
  nothing about that" can be read as reassurance. Every message here is about
  *this tool's sources*, never about the patient's body, and the constant
  :data:`NOT_A_JUDGEMENT` states that explicitly in every rendering.
* **Do not alarm.** Nothing here says "warning", "failed", "error" or "unsafe" to
  a patient. The system not having a citation is an ordinary limitation of a
  small research index, and presenting it as a malfunction invites a reader to
  conclude their question was itself alarming.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass
from enum import StrEnum

HEADING = "Not enough verified information"

# Present in every rendering. This is the sentence that stops "we found nothing"
# from being read as "there is nothing wrong with you".
NOT_A_JUDGEMENT = (
    "This is about what is in Synapse's sources, not about you or your health. "
    "It does not mean there is nothing to discuss, and it does not rule anything in or out."
)

# Neutral next steps. Deliberately about how to raise the topic, not about what
# to do medically — the latter would be advice, which this system does not give.
NEXT_STEPS: tuple[str, ...] = (
    "Write your question down as you would say it, and bring it to your appointment.",
    "Tell your clinician what you have noticed and when it started.",
    "Ask what they think is worth checking, and what would change their answer.",
    "If something feels urgent before then, contact your clinic or local urgent care service.",
)


class InsufficientReason(StrEnum):
    """Why no verified answer could be produced.

    Operator-facing. The patient sees the wording in :data:`REASON_MESSAGES`,
    which is close to identical across several of these on purpose.
    """

    NO_ELIGIBLE_EVIDENCE = "no_eligible_evidence"  # Retrieval returned nothing usable
    FAILED_VALIDATION = "failed_validation"  # Retrieved, but no citation verified
    BELOW_THRESHOLD = "below_threshold"  # Some support, below the display policy
    CONFLICTING_SOURCES = "conflicting_sources"  # Sources disagree; no safe summary
    OUT_OF_SCOPE = "out_of_scope"  # The pack does not cover this topic
    INDEX_UNVERIFIED = "index_unverified"  # The index could not be validated


# One sentence per reason, patient-facing. Each says what is true about the
# sources without characterising the question or the person asking it.
REASON_MESSAGES: dict[InsufficientReason, str] = {
    InsufficientReason.NO_ELIGIBLE_EVIDENCE: (
        "Synapse could not find research in its sources that answers this question."
    ),
    InsufficientReason.FAILED_VALIDATION: (
        "Synapse found some research, but could not confirm that it supports a clear answer, "
        "so it has not shown one."
    ),
    InsufficientReason.BELOW_THRESHOLD: (
        "Synapse found only partial support for an answer. Rather than give you something "
        "incomplete, it has not answered."
    ),
    InsufficientReason.CONFLICTING_SOURCES: (
        "The sources Synapse found do not agree with each other, so it cannot summarise them "
        "safely. This is common where research is still developing."
    ),
    InsufficientReason.OUT_OF_SCOPE: (
        "This question is outside the topics Synapse's reviewed sources cover."
    ),
    InsufficientReason.INDEX_UNVERIFIED: (
        "Synapse could not confirm its research library was intact, so it has not used it. "
        "This is a problem with the tool, not with your question."
    ),
}

# What a clinician or operator sees in a review view. Plain, technical, and
# never shown to a patient.
REASON_OPERATOR_NOTES: dict[InsufficientReason, str] = {
    InsufficientReason.NO_ELIGIBLE_EVIDENCE: "Retrieval returned no candidates that passed source-pack eligibility.",
    InsufficientReason.FAILED_VALIDATION: "Candidates were retrieved; no claim's excerpt verified against its cited chunk.",
    InsufficientReason.BELOW_THRESHOLD: "Supported claims fell below the display policy's ratio or floor.",
    InsufficientReason.CONFLICTING_SOURCES: "Verified claims contradict one another; no safe summary is available.",
    InsufficientReason.OUT_OF_SCOPE: "The query falls outside the source pack's declared scope.",
    InsufficientReason.INDEX_UNVERIFIED: "Index or manifest verification failed; retrieval was not attempted.",
}


@dataclass(frozen=True)
class InsufficientEvidence:
    """The complete patient-facing content for this state.

    Every string is drawn from the constants above. There is deliberately no
    constructor parameter through which generated text could be supplied.
    """

    reason: InsufficientReason
    pack_id: str = ""
    pack_version: str = ""

    @property
    def heading(self) -> str:
        """The heading shown to a patient."""
        return HEADING

    @property
    def message(self) -> str:
        """The one sentence explaining what happened."""
        return REASON_MESSAGES[self.reason]

    @property
    def not_a_judgement(self) -> str:
        """The sentence that prevents a reassurance reading."""
        return NOT_A_JUDGEMENT

    @property
    def next_steps(self) -> tuple[str, ...]:
        """Neutral suggestions for raising the topic with a clinician."""
        return NEXT_STEPS

    @property
    def operator_note(self) -> str:
        """The technical reason, for a review view only."""
        return REASON_OPERATOR_NOTES[self.reason]

    def as_metadata(self) -> dict[str, str]:
        """Machine-readable form for telemetry and logs.

        Carries the reason code and the pack version — never the query.
        """
        return {
            "insufficient_reason": self.reason.value,
            "pack_id": self.pack_id,
            "pack_version": self.pack_version,
        }


def from_failure_code(code: str) -> InsufficientReason:
    """Map a typed pipeline failure code onto a patient-facing reason.

    Anything unrecognised maps to :attr:`InsufficientReason.NO_ELIGIBLE_EVIDENCE`,
    which is the most conservative claim available: it says only that no usable
    evidence was found, which is true of every failure on this path.
    """
    mapping = {
        "evidence_unavailable": InsufficientReason.NO_ELIGIBLE_EVIDENCE,
        "retrieval_failed": InsufficientReason.NO_ELIGIBLE_EVIDENCE,
        "index_unverified": InsufficientReason.INDEX_UNVERIFIED,
        "generation_schema_invalid": InsufficientReason.FAILED_VALIDATION,
        "generation_invalid_json": InsufficientReason.FAILED_VALIDATION,
    }
    return mapping.get(code, InsufficientReason.NO_ELIGIBLE_EVIDENCE)


__all__ = [
    "HEADING",
    "NEXT_STEPS",
    "NOT_A_JUDGEMENT",
    "REASON_MESSAGES",
    "REASON_OPERATOR_NOTES",
    "InsufficientEvidence",
    "InsufficientReason",
    "from_failure_code",
]

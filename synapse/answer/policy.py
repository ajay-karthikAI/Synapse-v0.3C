"""
synapse.answer.policy
=====================
The decision: display, degrade, or abstain.

Requirements 8 and 9 in one place. Two rules, applied in order:

1. **An unsupported medical claim is never displayed.** Not greyed out, not
   footnoted — withheld. A patient reading an answer has no way to weigh a
   caveat about groundedness, so the only safe treatment is absence.

2. **If what remains cannot answer the question, abstain and say why.** An
   answer stripped of its substantive claims is not a shorter answer, it is a
   misleading one: the surviving sentences read as complete while the load-
   bearing ones have been silently removed.

Deciding whether "enough survived" needs a definition of *essential*, and this
module uses a deliberately blunt one: a claim is essential when it is the only
thing standing between the answer and vacuity. The threshold is expressed as a
ratio and a floor, both configurable, and both erring toward abstention —
over-abstaining is a usability cost, under-abstaining is a safety one.

Emergency routing is untouched. It fires before retrieval, and this module
passes it straight through (requirement 17).
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass

from synapse.answer.schema import AnswerAction, GroundedAnswer, SupportStatus
from synapse.answer.verify import VerificationReport
from synapse.logging import get_logger

logger = get_logger(__name__)

# Text shown when the system abstains. Fixed here rather than generated, so a
# patient always receives the same honest explanation and the wording cannot
# drift with the model's mood.
ABSTENTION_SUMMARY = (
    "I could not find enough reliable information in my sources to answer this safely."
)
ABSTENTION_DETAIL = (
    "The sources available to me do not contain enough supporting evidence for a clear answer "
    "to your question. Rather than give you something that might be wrong, I have not answered it. "
    "The questions listed for your appointment are still worth asking."
)


@dataclass(frozen=True)
class DisplayPolicy:
    """When an answer may be displayed rather than abstained from."""

    min_supported_ratio: float = 0.5  # At least half the claims must survive verification
    min_supported_claims: int = 1  # And at least this many in absolute terms
    treat_partial_as_supported: bool = (
        True  # A partially-supported claim may be shown, marked; it has real evidence behind it
    )
    abstain_on_any_fabrication: bool = (
        False  # When True, a single invented citation abstains the whole answer
    )

    def describe(self) -> str:
        """One-line description, recorded alongside every decision."""
        parts = [f"min_ratio={self.min_supported_ratio}", f"min_claims={self.min_supported_claims}"]
        if self.treat_partial_as_supported:
            parts.append("partial-displayed")
        if self.abstain_on_any_fabrication:
            parts.append("fabrication-abstains")
        return "; ".join(parts)


@dataclass
class PolicyDecision:
    """The outcome, and why."""

    action: AnswerAction
    displayed_claim_ids: list[str]
    withheld_claim_ids: list[str]
    reason: str
    policy: str

    @property
    def abstained(self) -> bool:
        """True when no medical content will be shown."""
        return self.action is AnswerAction.ABSTAIN

    def as_dict(self) -> dict[str, object]:
        """Machine-readable form for logging and evaluation."""
        return {
            "action": self.action.value,
            "displayed": self.displayed_claim_ids,
            "withheld": self.withheld_claim_ids,
            "reason": self.reason,
            "policy": self.policy,
        }


def decide(
    answer: GroundedAnswer,
    report: VerificationReport,
    policy: DisplayPolicy | None = None,
) -> PolicyDecision:
    """Decide whether a verified answer may be displayed.

    Expects an answer whose ``support_status`` fields have already been set by
    :func:`synapse.answer.verify.verify_answer`; the statuses on an unverified
    answer default to ``UNSUPPORTED``, so passing one in fails closed.
    """
    policy = policy or DisplayPolicy()

    # Emergency and staff routing are decided before retrieval and pass through
    # untouched. This change does not revisit the red-flag policy.
    if answer.action in (AnswerAction.EMERGENCY, AnswerAction.MEDICAL_STAFF):
        return PolicyDecision(
            action=answer.action,
            displayed_claim_ids=[],
            withheld_claim_ids=[c.claim_id for c in answer.claims],
            reason="routing decided before retrieval; claim display not applicable",
            policy=policy.describe(),
        )

    displayable_statuses = {SupportStatus.SUPPORTED}
    if policy.treat_partial_as_supported:
        displayable_statuses.add(SupportStatus.PARTIALLY_SUPPORTED)

    displayed = [c.claim_id for c in answer.claims if c.support_status in displayable_statuses]
    withheld = [c.claim_id for c in answer.claims if c.support_status not in displayable_statuses]

    if policy.abstain_on_any_fabrication and report.fabrication_count:
        # A model that invented one citation has demonstrated it will invent
        # citations; the remaining ones deserve no more trust.
        return PolicyDecision(
            AnswerAction.ABSTAIN,
            [],
            [c.claim_id for c in answer.claims],
            f"{report.fabrication_count} claim(s) cite evidence that was not retrieved",
            policy.describe(),
        )

    if not answer.claims:
        return PolicyDecision(
            AnswerAction.ABSTAIN, [], [], "the answer contains no claims", policy.describe()
        )

    ratio = len(displayed) / len(answer.claims)
    if len(displayed) < policy.min_supported_claims or ratio < policy.min_supported_ratio:
        # Too little survived. Showing the remainder would read as a complete
        # answer while the load-bearing claims had been silently removed.
        return PolicyDecision(
            AnswerAction.ABSTAIN,
            [],
            [c.claim_id for c in answer.claims],
            f"only {len(displayed)} of {len(answer.claims)} claim(s) are supported "
            f"({ratio:.0%}, policy requires {policy.min_supported_ratio:.0%} and at least {policy.min_supported_claims})",
            policy.describe(),
        )

    logger.info(
        "answer display decided",
        extra={"action": "answer", "displayed": len(displayed), "withheld": len(withheld)},
    )
    return PolicyDecision(
        AnswerAction.ANSWER,
        displayed,
        withheld,
        f"{len(displayed)} of {len(answer.claims)} claim(s) supported",
        policy.describe(),
    )


def apply(answer: GroundedAnswer, decision: PolicyDecision) -> GroundedAnswer:
    """Produce the answer as it will actually be shown.

    Withheld claims are **removed**, not marked. Nothing downstream can then
    accidentally render one, which is a stronger guarantee than asking every
    renderer to remember to filter.

    On abstention the summary and doctor-evaluation text are replaced with the
    fixed abstention wording, so no fragment of unsupported prose survives.
    """
    if decision.abstained:
        return answer.model_copy(
            update={
                "action": AnswerAction.ABSTAIN,
                "summary": ABSTENTION_SUMMARY,
                "claims": [],  # Every claim withheld
                "doctor_evaluation": "",
                # Questions are retained: they assert nothing, cite nothing, and
                # remain the most useful thing a patient can take to their
                # appointment when the system cannot answer.
                "limitations": [*answer.limitations, ABSTENTION_DETAIL],
            }
        )

    displayed = set(decision.displayed_claim_ids)
    return answer.model_copy(
        update={
            "action": decision.action,
            "claims": [claim for claim in answer.claims if claim.claim_id in displayed],
        }
    )


__all__ = [
    "ABSTENTION_DETAIL",
    "ABSTENTION_SUMMARY",
    "DisplayPolicy",
    "PolicyDecision",
    "apply",
    "decide",
]

"""
synapse.evalset.gating
======================
Which cases may influence a release decision.

The requirement, stated as code:

    **A case cannot become release-gating until its review requirements are
    satisfied.**

The default policy is restrictive, and every relaxation is recorded in the
report that uses it. A case is gating-eligible only when *all* of the following
hold — the list is deliberately conjunctive, and a single failure disqualifies:

* not synthetic (terminally excluded by construction);
* not excluded for any other reason;
* annotation status is ``reviewed`` or ``adjudicated``;
* at least ``minimum_reviews`` independent reviews;
* no open disagreement;
* an adjudication record exists if reviewers disagreed;
* redaction discharged if the privacy class requires it;
* labelled against the corpus version being evaluated.

The last one is easy to overlook and is the reason the shipped evaluation set
was worthless: relevance labels naming documents that are not in the corpus
produce a score of zero regardless of retrieval quality.

Every decision carries a *reason*, so a report can explain why a dataset of 300
cases gates on 12 rather than merely stating the number.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass, field

from synapse.logging import get_logger
from synapse.schemas.enums import AnnotationStatus, DisagreementStatus, RedactionStatus
from synapse.schemas.evalset import GATING_ELIGIBLE_STATUSES, REDACTION_REQUIRED_CLASSES, EvalCase

logger = get_logger(__name__)


@dataclass(frozen=True)
class GatingPolicy:
    """What a case must satisfy to influence a release decision.

    Defaults are the restrictive ones. ``allow_single_review`` and
    ``allow_unreviewed`` exist for bootstrapping a brand-new dataset, and both
    are surfaced in :meth:`describe` so a relaxed run cannot be mistaken for a
    governed one.
    """

    minimum_reviews: int = 2  # Two independent reviews, so agreement is computable at all
    require_adjudication_on_disagreement: bool = True
    require_redaction: bool = True
    require_matching_corpus_version: bool = True
    allow_single_review: bool = False  # Bootstrapping only
    allow_unreviewed: bool = False  # Bootstrapping only; never for a release gate

    def describe(self) -> str:
        """One-line description, recorded wherever the policy is applied."""
        if self.allow_unreviewed:  # Deliberately alarming: this must stand out in a report
            return "UNGOVERNED: unreviewed cases permitted (bootstrapping only, never for release gating)"
        parts = [f"min_reviews={1 if self.allow_single_review else self.minimum_reviews}"]
        if self.require_adjudication_on_disagreement:
            parts.append("adjudication-required")
        if self.require_redaction:
            parts.append("redaction-required")
        if self.require_matching_corpus_version:
            parts.append("corpus-version-pinned")
        return "; ".join(parts)

    @property
    def effective_minimum_reviews(self) -> int:
        """Review count actually enforced, after the single-review relaxation."""
        return 1 if self.allow_single_review else self.minimum_reviews


@dataclass(frozen=True)
class GatingDecision:
    """Whether one case may gate a release, and why."""

    case_id: str
    eligible: bool
    reason: str  # Machine-readable reason code
    detail: str = ""  # Human-readable context

    def as_dict(self) -> dict[str, object]:
        """Machine-readable form for reports."""
        return {
            "case_id": self.case_id,
            "eligible": self.eligible,
            "reason": self.reason,
            "detail": self.detail,
        }


def evaluate_case(
    case: EvalCase, policy: GatingPolicy, *, corpus_version: str | None = None
) -> GatingDecision:
    """Decide whether one case may gate a release.

    Checks run most-decisive first, and the first failure wins, so the reason
    code names the *primary* disqualifier rather than an incidental one.
    """
    if case.is_synthetic:
        # Checked first and unconditionally: no policy relaxation reaches this,
        # because a synthetic case is an illustration, not evidence.
        return GatingDecision(
            case.case_id,
            False,
            "synthetic",
            "engineering-authored illustration; never release-gating",
        )

    if case.excluded:
        return GatingDecision(case.case_id, False, "excluded", case.exclusion_reason or "")

    # Checked before the review rules, deliberately: an unredacted
    # patient-derived case must not gate a release even with two clinician reviews.
    owes_redaction = policy.require_redaction and case.privacy_class in REDACTION_REQUIRED_CLASSES
    if owes_redaction and case.redaction_status is not RedactionStatus.REDACTED:
        return GatingDecision(
            case.case_id,
            False,
            "redaction_incomplete",
            f"privacy_class={case.privacy_class.value}, redaction_status={case.redaction_status.value}",
        )

    if (
        policy.require_matching_corpus_version
        and corpus_version is not None
        and case.corpus_version != corpus_version
    ):
        # Labels are not portable across corpus versions. This is the check the
        # shipped evaluation set would have failed.
        return GatingDecision(
            case.case_id,
            False,
            "corpus_version_mismatch",
            f"labelled against {case.corpus_version}, evaluating {corpus_version}",
        )

    if policy.allow_unreviewed:  # Escape hatch, evaluated only after the guards above
        return GatingDecision(
            case.case_id, True, "ungoverned_override", "gating policy permits unreviewed cases"
        )

    if case.annotation_status not in GATING_ELIGIBLE_STATUSES:
        return GatingDecision(
            case.case_id,
            False,
            "not_reviewed",
            f"annotation_status={case.annotation_status.value}; needs one of {sorted(s.value for s in GATING_ELIGIBLE_STATUSES)}",
        )

    if case.reviewer_count < policy.effective_minimum_reviews:
        return GatingDecision(
            case.case_id,
            False,
            "insufficient_reviews",
            f"{case.reviewer_count} review(s), policy requires {policy.effective_minimum_reviews}",
        )

    if (
        case.disagreement_status is DisagreementStatus.DISAGREED_OPEN
        and policy.require_adjudication_on_disagreement
    ):
        # Reviewers disagreed and nobody resolved it, so the case has no
        # settled label to measure against.
        return GatingDecision(
            case.case_id,
            False,
            "unresolved_disagreement",
            "reviewers disagree and no adjudication is recorded",
        )

    if case.annotation_status is AnnotationStatus.ADJUDICATED and case.adjudication is None:
        # Defence in depth: the schema forbids this, and so does the gate.
        return GatingDecision(
            case.case_id,
            False,
            "missing_adjudication_record",
            "status is adjudicated but no record exists",
        )

    return GatingDecision(
        case.case_id,
        True,
        "eligible",
        f"{case.reviewer_count} review(s), {case.annotation_status.value}",
    )


@dataclass
class GatingReport:
    """Which cases in a dataset may gate a release."""

    dataset_version: str
    policy_description: str
    decisions: list[GatingDecision] = field(default_factory=list)

    @property
    def eligible(self) -> list[GatingDecision]:
        """Decisions admitting a case."""
        return [decision for decision in self.decisions if decision.eligible]

    @property
    def eligible_case_ids(self) -> list[str]:
        """Identifiers of gating-eligible cases, sorted."""
        return sorted(decision.case_id for decision in self.eligible)

    def reason_counts(self) -> dict[str, int]:
        """How many cases were excluded for each reason, sorted for stable output."""
        counts: dict[str, int] = {}
        for decision in self.decisions:
            if not decision.eligible:
                counts[decision.reason] = counts.get(decision.reason, 0) + 1
        return dict(sorted(counts.items()))

    def as_dict(self) -> dict[str, object]:
        """Machine-readable form for CI."""
        return {
            "dataset_version": self.dataset_version,
            "policy": self.policy_description,
            "total_cases": len(self.decisions),
            "eligible_count": len(self.eligible),
            "excluded_count": len(self.decisions) - len(self.eligible),
            "exclusion_reasons": self.reason_counts(),
            "eligible_case_ids": self.eligible_case_ids,
            "decisions": [decision.as_dict() for decision in self.decisions],
        }

    def render(self) -> str:
        """Console rendering, leading with the number that matters."""
        lines = [
            f"Release gating — dataset {self.dataset_version}",
            f"  policy          : {self.policy_description}",
            f"  total cases     : {len(self.decisions)}",
            f"  GATING-ELIGIBLE : {len(self.eligible)}",
        ]
        reasons = self.reason_counts()
        if reasons:
            lines.append("  excluded, by reason:")
            width = max(len(reason) for reason in reasons)
            lines.extend(
                f"    {reason.ljust(width)} : {count}" for reason, count in reasons.items()
            )
        if not self.eligible:
            lines.append("\n  No case may gate a release. Metrics computed from this dataset are")
            lines.append("  informational only and must not block or approve a release.")
        return "\n".join(lines)


def evaluate_dataset(
    cases: list[EvalCase],
    *,
    dataset_version: str,
    policy: GatingPolicy | None = None,
    corpus_version: str | None = None,
) -> GatingReport:
    """Apply the gating policy to every case in a dataset."""
    policy = policy or GatingPolicy()
    report = GatingReport(dataset_version=dataset_version, policy_description=policy.describe())
    report.decisions = [
        evaluate_case(case, policy, corpus_version=corpus_version) for case in cases
    ]
    logger.info(
        "gating evaluated",
        extra={
            "dataset_version": dataset_version,
            "total": len(report.decisions),
            "eligible": len(report.eligible),
        },
    )
    return report


__all__ = ["GatingDecision", "GatingPolicy", "GatingReport", "evaluate_case", "evaluate_dataset"]

"""
synapse.evalset.agreement
=========================
Inter-annotator agreement.

Why raw percentage agreement is not enough: if 90% of cases are
``ordinary_education``, two reviewers who both label everything
``ordinary_education`` agree 90% of the time while demonstrating nothing. The
kappa statistics correct for agreement expected by chance, which is the number
worth reporting.

Which statistic applies depends on the data, so the implementation picks and
*says which it picked*:

* **Cohen's kappa** — exactly two reviewers, both rating the same items.
* **Fleiss' kappa** — three or more, with a fixed number of ratings per item.
* **Raw agreement only** — ragged reviewer counts, where neither kappa is
  defined. Reported as such rather than approximated.

Everything here is deterministic arithmetic over recorded judgements. No model,
no sampling, no LLM.
"""

from __future__ import annotations  # Postponed annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from synapse.logging import get_logger
from synapse.schemas.evalset import EvalCase

logger = get_logger(__name__)

# Fields compared for agreement. Both are gating-relevant: they decide what the
# system is expected to do, so a disagreement on either blocks the case.
AGREEMENT_FIELDS = ("category", "expected_behavior")

KAPPA_INTERPRETATION = (  # Landis & Koch (1977) bands, quoted for orientation only
    (0.81, "almost perfect"),
    (0.61, "substantial"),
    (0.41, "moderate"),
    (0.21, "fair"),
    (0.00, "slight"),
)


def interpret_kappa(value: float) -> str:
    """Conventional descriptive band for a kappa value.

    Included for readability, not authority. These bands are a widely-quoted
    convention, not a standard, and what counts as adequate agreement for
    patient-facing medical labelling is a decision for whoever owns the
    labelling protocol.
    """
    if value < 0:  # Worse than chance
        return "poor (worse than chance)"
    for floor, label in KAPPA_INTERPRETATION:
        if value >= floor:
            return label
    return "slight"


def cohens_kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Cohen's kappa for two reviewers over paired categorical judgements.

    Returns ``None`` when there are no pairs. Returns ``1.0`` when both
    reviewers used only one category and agreed on every item — the formula is
    0/0 there, and treating perfect agreement as undefined would be misleading.
    """
    if not pairs:
        return None
    total = len(pairs)
    observed = sum(1 for left, right in pairs if left == right) / total

    left_counts = Counter(left for left, _ in pairs)
    right_counts = Counter(right for _, right in pairs)
    # Chance agreement: probability both reviewers pick the same label
    # independently, given each reviewer's own marginal distribution.
    expected = sum(
        (left_counts[label] / total) * (right_counts[label] / total)
        for label in set(left_counts) | set(right_counts)
    )

    if expected >= 1.0:  # Degenerate: one label used throughout, so chance agreement is certainty
        return 1.0 if observed >= 1.0 else 0.0
    return (observed - expected) / (1 - expected)


def fleiss_kappa(ratings: list[list[str]]) -> float | None:
    """Fleiss' kappa for three or more reviewers.

    Args:
        ratings: one list of labels per item. Every item must carry the same
            number of ratings — Fleiss' kappa is undefined otherwise, and
            ``None`` is returned rather than an approximation.
    """
    if not ratings:
        return None
    rater_counts = {len(item) for item in ratings}
    if len(rater_counts) != 1:  # Ragged data: the statistic does not apply
        return None
    raters = rater_counts.pop()
    if raters < 2:
        return None

    categories = sorted({label for item in ratings for label in item})
    items = len(ratings)

    # Per-item agreement: the proportion of rater pairs within an item that agree.
    item_agreements = []
    for item in ratings:
        counts = Counter(item)
        agreeing_pairs = sum(count * (count - 1) for count in counts.values())
        item_agreements.append(agreeing_pairs / (raters * (raters - 1)))
    observed = sum(item_agreements) / items

    # Chance agreement from the overall marginal distribution across all ratings.
    total_ratings = items * raters
    proportions = [
        sum(item.count(category) for item in ratings) / total_ratings for category in categories
    ]
    expected = sum(proportion**2 for proportion in proportions)

    if expected >= 1.0:  # Degenerate: a single category throughout
        return 1.0 if observed >= 1.0 else 0.0
    return (observed - expected) / (1 - expected)


@dataclass
class FieldAgreement:
    """Agreement statistics for one labelled field."""

    field_name: str
    comparable_cases: int  # Cases with two or more reviews
    raw_agreement: float | None  # Proportion of cases where every reviewer matched
    kappa: float | None  # Cohen's or Fleiss', depending on reviewer count
    statistic: str  # Which statistic was used, or why none was
    disagreeing_case_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        """Machine-readable form for reports."""
        return {
            "field": self.field_name,
            "comparable_cases": self.comparable_cases,
            "raw_agreement": round(self.raw_agreement, 4)
            if self.raw_agreement is not None
            else None,
            "kappa": round(self.kappa, 4) if self.kappa is not None else None,
            "kappa_interpretation": interpret_kappa(self.kappa) if self.kappa is not None else None,
            "statistic": self.statistic,
            "disagreeing_case_ids": self.disagreeing_case_ids,
        }


@dataclass
class AgreementReport:
    """Inter-annotator agreement across a dataset."""

    dataset_version: str
    total_cases: int
    multi_reviewed_cases: int
    reviewer_ids: list[str] = field(default_factory=list)
    fields: list[FieldAgreement] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_computable(self) -> bool:
        """True when at least one case carries two or more reviews."""
        return self.multi_reviewed_cases > 0

    def as_dict(self) -> dict[str, object]:
        """Machine-readable form for CI."""
        return {
            "dataset_version": self.dataset_version,
            "total_cases": self.total_cases,
            "multi_reviewed_cases": self.multi_reviewed_cases,
            "computable": self.is_computable,
            "reviewer_count": len(self.reviewer_ids),
            "reviewer_ids": self.reviewer_ids,
            "fields": [entry.as_dict() for entry in self.fields],
            "notes": self.notes,
        }

    def render(self) -> str:
        """Console rendering."""
        lines = [f"Inter-annotator agreement — dataset {self.dataset_version}"]
        lines.append(f"  cases with 2+ reviews : {self.multi_reviewed_cases} of {self.total_cases}")
        lines.append(f"  distinct reviewers    : {len(self.reviewer_ids)}")
        if not self.is_computable:
            lines.append("\n  Not computable: no case carries two or more independent reviews.")
        for entry in self.fields:
            kappa = (
                f"{entry.kappa:.3f} ({interpret_kappa(entry.kappa)})"
                if entry.kappa is not None
                else "n/a"
            )
            raw = f"{entry.raw_agreement:.1%}" if entry.raw_agreement is not None else "n/a"
            lines.append(f"\n  {entry.field_name}")
            lines.append(f"    raw agreement : {raw}")
            lines.append(f"    {entry.statistic:<14}: {kappa}")
            if entry.disagreeing_case_ids:
                lines.append(
                    f"    disagreements : {len(entry.disagreeing_case_ids)} — {', '.join(entry.disagreeing_case_ids[:5])}"
                )
        for note in self.notes:
            lines.append(f"\n  NOTE: {note}")
        return "\n".join(lines)


def compute_agreement(cases: list[EvalCase], *, dataset_version: str) -> AgreementReport:
    """Compute inter-annotator agreement over every multi-reviewed case.

    Synthetic cases are excluded: they carry no reviews by construction, and
    counting them would understate the reviewed proportion.
    """
    reviewable = [case for case in cases if not case.is_synthetic and not case.excluded]
    multi = [case for case in reviewable if case.reviewer_count >= 2]

    reviewers = sorted({review.reviewer_id for case in reviewable for review in case.reviews})
    report = AgreementReport(
        dataset_version=dataset_version,
        total_cases=len(reviewable),
        multi_reviewed_cases=len(multi),
        reviewer_ids=reviewers,
    )

    if not multi:
        report.notes.append(
            "No case carries two or more independent reviews, so agreement cannot be computed. "
            "Single-reviewed cases cannot gate a release under the default policy."
        )
        return report

    rater_counts = {case.reviewer_count for case in multi}
    for field_name in AGREEMENT_FIELDS:
        labels_by_case = [
            [getattr(review, field_name).value for review in case.reviews] for case in multi
        ]
        disagreeing = [
            case.case_id
            for case, labels in zip(multi, labels_by_case, strict=True)
            if len(set(labels)) > 1
        ]
        raw = 1.0 - (len(disagreeing) / len(multi))

        if rater_counts == {2}:  # Exactly two reviewers on every comparable case
            kappa = cohens_kappa([(labels[0], labels[1]) for labels in labels_by_case])
            statistic = "cohen's kappa"
        elif len(rater_counts) == 1:  # Uniform reviewer count of three or more
            kappa = fleiss_kappa(labels_by_case)
            statistic = "fleiss' kappa"
        else:
            # Ragged reviewer counts: neither kappa is defined. Reported as
            # such rather than silently approximated with one of them.
            kappa, statistic = None, "not applicable (ragged reviewer counts)"

        report.fields.append(
            FieldAgreement(
                field_name=field_name,
                comparable_cases=len(multi),
                raw_agreement=raw,
                kappa=kappa,
                statistic=statistic,
                disagreeing_case_ids=sorted(disagreeing),
            )
        )

    if len(rater_counts) > 1:
        report.notes.append(
            f"Reviewer counts vary across cases ({sorted(rater_counts)}), so no kappa statistic applies. "
            "Raw agreement is reported alone and overstates agreement on skewed label distributions."
        )
    if len(multi) < 20:
        # Not a threshold anyone should treat as authoritative, but a small-n
        # kappa is unstable and a reader should be told before quoting it.
        report.notes.append(
            f"Only {len(multi)} multi-reviewed case(s): kappa is unstable at this sample size."
        )

    logger.info(
        "agreement computed", extra={"multi_reviewed": len(multi), "reviewers": len(reviewers)}
    )
    return report


def disagreement_matrix(cases: list[EvalCase], field_name: str) -> dict[tuple[str, str], int]:
    """Confusion counts between reviewer label pairs, for diagnosing disagreement.

    Shows *which* labels get confused rather than only how often, which is what
    tells a protocol owner whether two categories need clearer definitions.
    """
    matrix: dict[tuple[str, str], int] = defaultdict(int)
    for case in cases:
        if case.reviewer_count < 2:
            continue
        labels = sorted(getattr(review, field_name).value for review in case.reviews)
        for index, left in enumerate(labels):
            for right in labels[index + 1 :]:
                matrix[(left, right)] += 1  # Sorted pairs, so (a,b) and (b,a) accumulate together
    return dict(sorted(matrix.items()))


__all__ = [
    "AGREEMENT_FIELDS",
    "AgreementReport",
    "FieldAgreement",
    "cohens_kappa",
    "compute_agreement",
    "disagreement_matrix",
    "fleiss_kappa",
    "interpret_kappa",
]

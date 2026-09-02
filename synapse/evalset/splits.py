"""
synapse.evalset.splits
======================
Train / dev / test assignment, and leakage detection.

Two independent leakage channels are guarded here, because closing one does
nothing about the other:

1. **Query leakage.** A query and its near-duplicate in different splits means
   a system tuned on train has effectively seen test. Prevented by assigning
   whole near-duplicate *clusters* to a split rather than individual cases.

2. **Document leakage.** The same relevant document graded in both train and
   test means retrieval was tuned against evidence the test set depends on.
   This one cannot always be prevented — a small corpus forces overlap — so it
   is *measured and reported* rather than silently accepted.

Assignment is deterministic: clusters are ordered by a salted digest of their
member identifiers, not by iteration order or by a random seed. The same
dataset always produces the same splits on every machine, so a split boundary
never moves underneath a comparison between two evaluation runs.
"""

from __future__ import annotations  # Postponed annotations

import hashlib  # Deterministic cluster ordering
from collections import defaultdict
from dataclasses import dataclass, field

from synapse.evalset.similarity import DEFAULT_NEAR_DUPLICATE_THRESHOLD, cluster_by_similarity
from synapse.logging import get_logger
from synapse.schemas.enums import DatasetSplit
from synapse.schemas.evalset import EvalCase

logger = get_logger(__name__)

DEFAULT_RATIOS: dict[
    DatasetSplit, float
] = {  # Conventional split; dev is small because it is iterated on, test larger because it must resolve small differences
    DatasetSplit.TRAIN: 0.6,
    DatasetSplit.DEV: 0.2,
    DatasetSplit.TEST: 0.2,
}

SPLIT_SALT = "synapse-evalset-split-v1"  # Fixed salt. Changing it reshuffles every split, which is a deliberate, breaking act — hence the version suffix.


@dataclass
class SplitPlan:
    """A proposed assignment of cases to splits."""

    assignments: dict[str, DatasetSplit] = field(default_factory=dict)  # case_id -> split
    clusters: list[list[str]] = field(default_factory=list)  # Near-duplicate clusters, as assigned
    ratios: dict[str, float] = field(default_factory=dict)  # Achieved proportions

    def counts(self) -> dict[str, int]:
        """Cases per split, with every split present even at zero."""
        counts = {split.value: 0 for split in DatasetSplit}
        for split in self.assignments.values():
            counts[split.value] += 1
        return counts

    def multi_case_clusters(self) -> list[list[str]]:
        """Clusters holding more than one case — the near-duplicate groups."""
        return [cluster for cluster in self.clusters if len(cluster) > 1]


def _cluster_sort_key(cluster: list[str]) -> str:
    """Deterministic ordering key for a cluster.

    A salted digest of the members rather than the raw identifier, so ordering
    does not correlate with naming — otherwise every ``syn-emergency-*`` case
    would land in the same split purely because of its prefix.
    """
    payload = SPLIT_SALT + "|" + "|".join(sorted(cluster))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def plan_splits(
    cases: list[EvalCase],
    *,
    ratios: dict[DatasetSplit, float] | None = None,
    threshold: float = DEFAULT_NEAR_DUPLICATE_THRESHOLD,
    stratify_by_category: bool = True,
) -> SplitPlan:
    """Assign cases to splits without separating near-duplicate queries.

    Stratification by category is on by default: without it, a category with
    few cases can land entirely in one split, and a metric computed per
    category then has an empty cell in the others.

    Excluded cases are left ``UNASSIGNED`` — they take part in no split, since
    they take part in no evaluation.
    """
    ratios = ratios or DEFAULT_RATIOS
    total = sum(ratios.values())
    if abs(total - 1.0) > 1e-9:  # Silently renormalising would hide a config typo
        raise ValueError(f"split ratios must sum to 1.0, got {total}")

    usable = [case for case in cases if not case.excluded]
    clusters = cluster_by_similarity(
        [(case.case_id, case.query) for case in usable], threshold=threshold
    )

    # Cluster category = the category of its lexicographically first member.
    # A cluster of near-duplicates that disagree on category is a labelling
    # problem, reported separately by the validator rather than papered over here.
    category_by_case = {case.case_id: case.category.value for case in usable}
    grouped: dict[str, list[list[str]]] = defaultdict(list)
    for cluster in clusters:
        key = category_by_case[cluster[0]] if stratify_by_category else "_all"
        grouped[key].append(cluster)

    assignments: dict[str, DatasetSplit] = {case.case_id: DatasetSplit.UNASSIGNED for case in cases}
    ordered_splits = [
        split
        for split in (DatasetSplit.TRAIN, DatasetSplit.DEV, DatasetSplit.TEST)
        if ratios.get(split, 0) > 0
    ]

    for key in sorted(grouped):  # Sorted so stratum order is stable
        stratum = sorted(grouped[key], key=_cluster_sort_key)
        assigned_counts = dict.fromkeys(ordered_splits, 0)
        stratum_total = sum(len(cluster) for cluster in stratum)

        for cluster in stratum:
            # Greedy: give this cluster to whichever split is furthest below its
            # target share. Greedy rather than proportional because clusters are
            # indivisible — a 3-case cluster cannot be split 60/20/20.
            deficits = {
                split: (ratios[split] * stratum_total) - assigned_counts[split]
                for split in ordered_splits
            }
            chosen = max(
                ordered_splits, key=lambda split: (deficits[split], -ordered_splits.index(split))
            )
            for case_id in cluster:
                assignments[case_id] = chosen
            assigned_counts[chosen] += len(cluster)

    assigned_total = sum(
        1 for split in assignments.values() if split is not DatasetSplit.UNASSIGNED
    )
    plan = SplitPlan(
        assignments=assignments,
        clusters=clusters,
        ratios={
            split.value: (
                sum(1 for s in assignments.values() if s is split) / assigned_total
                if assigned_total
                else 0.0
            )
            for split in ordered_splits
        },
    )
    logger.info(
        "split plan computed",
        extra={"clusters": len(clusters), "assigned": assigned_total, **plan.counts()},
    )
    return plan


# ---------------------------------------------------------------------------
# Leakage
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentLeak:
    """One document graded relevant in more than one split."""

    document_id: str
    splits: tuple[str, ...]  # Splits the document appears in
    case_ids: tuple[str, ...]  # Cases that reference it

    def as_dict(self) -> dict[str, object]:
        """Machine-readable form for reports."""
        return {
            "document_id": self.document_id,
            "splits": list(self.splits),
            "case_ids": list(self.case_ids),
        }


@dataclass
class LeakageReport:
    """Cross-split contamination findings."""

    document_leaks: list[DocumentLeak] = field(default_factory=list)
    query_leaks: list[tuple[str, str, float]] = field(
        default_factory=list
    )  # (case_a, case_b, similarity) across splits
    exact_duplicate_queries: list[tuple[str, str]] = field(default_factory=list)

    @property
    def has_query_leakage(self) -> bool:
        """True when near-duplicate queries straddle a split boundary.

        This is a hard failure: the split assignment is supposed to make it
        impossible, so its presence means something bypassed ``plan_splits``.
        """
        return bool(self.query_leaks)

    @property
    def has_document_leakage(self) -> bool:
        """True when any document is graded relevant in more than one split.

        Reported, not fatal. A corpus of a few hundred documents cannot always
        avoid it, and pretending otherwise would mean discarding usable cases.
        What matters is that the number is visible when a score is read.
        """
        return bool(self.document_leaks)

    def as_dict(self) -> dict[str, object]:
        """Machine-readable form for reports and CI."""
        return {
            "has_query_leakage": self.has_query_leakage,
            "has_document_leakage": self.has_document_leakage,
            "exact_duplicate_query_count": len(self.exact_duplicate_queries),
            "document_leak_count": len(self.document_leaks),
            "query_leak_count": len(self.query_leaks),
            "exact_duplicate_queries": [list(pair) for pair in self.exact_duplicate_queries],
            "document_leaks": [leak.as_dict() for leak in self.document_leaks],
            "query_leaks": [[a, b, round(score, 4)] for a, b, score in self.query_leaks],
        }


def detect_leakage(
    cases: list[EvalCase],
    *,
    threshold: float = DEFAULT_NEAR_DUPLICATE_THRESHOLD,
) -> LeakageReport:
    """Detect duplicate queries and cross-split contamination."""
    from synapse.evalset.similarity import (
        find_duplicate_pairs,  # Local import keeps the module import graph shallow
    )

    usable = [case for case in cases if not case.excluded]
    split_by_case = {case.case_id: case.split for case in usable}
    report = LeakageReport()

    pairs = find_duplicate_pairs(
        [(case.case_id, case.query) for case in usable], threshold=threshold
    )
    for pair in pairs:
        if pair.exact:  # An exact duplicate is a dataset defect wherever it sits
            report.exact_duplicate_queries.append((pair.case_id_a, pair.case_id_b))
        left, right = split_by_case[pair.case_id_a], split_by_case[pair.case_id_b]
        if left is not right and DatasetSplit.UNASSIGNED not in (left, right):
            report.query_leaks.append((pair.case_id_a, pair.case_id_b, pair.similarity))

    documents: dict[str, set[str]] = defaultdict(set)  # document_id -> splits
    document_cases: dict[str, set[str]] = defaultdict(set)  # document_id -> case_ids
    for case in usable:
        if case.split is DatasetSplit.UNASSIGNED:
            continue
        for document_id in case.relevant_document_ids:
            documents[document_id].add(case.split.value)
            document_cases[document_id].add(case.case_id)

    for document_id, splits in sorted(documents.items()):
        if len(splits) > 1:
            report.document_leaks.append(
                DocumentLeak(
                    document_id, tuple(sorted(splits)), tuple(sorted(document_cases[document_id]))
                )
            )

    logger.info(
        "leakage scan complete",
        extra={
            "document_leaks": len(report.document_leaks),
            "query_leaks": len(report.query_leaks),
            "exact_duplicates": len(report.exact_duplicate_queries),
        },
    )
    return report


__all__ = [
    "DEFAULT_RATIOS",
    "SPLIT_SALT",
    "DocumentLeak",
    "LeakageReport",
    "SplitPlan",
    "detect_leakage",
    "plan_splits",
]

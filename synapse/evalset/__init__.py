"""
synapse.evalset
===============
Evaluation-dataset framework.

The rule the whole subsystem serves:

    **An unreviewed case cannot gate a release.**

Automation and engineering can author cases, compute splits, detect leakage and
measure agreement. None of that makes a case authoritative. Only a recorded
human review does, and :mod:`synapse.evalset.gating` is where that boundary is
enforced.

Layout:

    synapse.evalset.dataset      load / write / validate a dataset directory
    synapse.evalset.gating       which cases may influence a release decision
    synapse.evalset.agreement    inter-annotator agreement (Cohen's / Fleiss' kappa)
    synapse.evalset.similarity   duplicate and near-duplicate query detection
    synapse.evalset.splits       train/dev/test assignment and leakage detection
    synapse.evalset.spreadsheet  CSV round trip for reviewer workflows
    synapse.evalset.compare      version comparison and changelog generation

Nothing here fabricates a reviewer identity, a signature, or an approval.
"""

from __future__ import annotations  # Postponed annotations

from synapse.evalset.agreement import AgreementReport, cohens_kappa, compute_agreement, fleiss_kappa
from synapse.evalset.compare import DatasetDiff, diff_datasets, generate_changelog_entry
from synapse.evalset.dataset import (
    DatasetValidationReport,
    EvalDataset,
    new_manifest,
)
from synapse.evalset.gating import GatingPolicy, GatingReport, evaluate_dataset
from synapse.evalset.similarity import cluster_by_similarity, find_duplicate_pairs, query_similarity
from synapse.evalset.splits import LeakageReport, SplitPlan, detect_leakage, plan_splits
from synapse.evalset.spreadsheet import export_review_sheet, import_review_sheet

__all__ = [
    "AgreementReport",
    "DatasetDiff",
    "DatasetValidationReport",
    "EvalDataset",
    "GatingPolicy",
    "GatingReport",
    "LeakageReport",
    "SplitPlan",
    "cluster_by_similarity",
    "cohens_kappa",
    "compute_agreement",
    "detect_leakage",
    "diff_datasets",
    "evaluate_dataset",
    "export_review_sheet",
    "find_duplicate_pairs",
    "fleiss_kappa",
    "generate_changelog_entry",
    "import_review_sheet",
    "new_manifest",
    "plan_splits",
    "query_similarity",
]

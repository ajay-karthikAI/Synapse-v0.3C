"""
synapse.evals
=============
The Synapse evaluation harness.

Replaces ``Evaluation/Evaluator.py``, whose defects were structural rather than
incidental: it scored live user queries against an empty ground-truth list (so
every metric was zero), returned ``0.0`` for undefined metrics (making
"no labels" indistinguishable from "retrieval failed"), and was unreachable
anyway because of a case-sensitive import.

Layout:

    synapse.evals.stats     confidence intervals, applied only where valid
    synapse.evals.metrics   deterministic metric families
    synapse.evals.judges    opt-in model judges; advisory, cached, never gating
    synapse.evals.system    the system under test, behind a replayable interface
    synapse.evals.harness   orchestration and aggregation
    synapse.evals.report    results.json, summary.json, report.md, per_case.jsonl
    synapse.evals.compare   baseline comparison and regression detection
    synapse.evals.run       the CLI

Deterministic metrics run fully offline and may gate a release. Judge metrics
are opt-in, advisory, and never presented as clinical validation.
"""

from __future__ import annotations  # Postponed annotations

from synapse.evals.compare import ComparisonReport, compare_runs, render_comparison
from synapse.evals.harness import HarnessConfig, RunOutput, run_evaluation
from synapse.evals.report import write_all
from synapse.evals.stats import Interval, bootstrap_interval, wilson_interval
from synapse.evals.system import CallableSystem, FixtureSystem, SystemResponse

__all__ = [
    "CallableSystem",
    "ComparisonReport",
    "FixtureSystem",
    "HarnessConfig",
    "Interval",
    "RunOutput",
    "SystemResponse",
    "bootstrap_interval",
    "compare_runs",
    "render_comparison",
    "run_evaluation",
    "wilson_interval",
    "write_all",
]

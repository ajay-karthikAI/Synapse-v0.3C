"""
synapse.schemas.evalrun
=======================
Typed records for an evaluation run: metric values, per-case outcomes, and the
run report itself.

Two properties of :class:`MetricValue` matter more than the rest:

* ``value`` is ``float | None``. An undefined metric is reported as undefined,
  never as ``0.0``. The legacy evaluator returned 0.0 when a case had no ground
  truth, which is indistinguishable from "retrieval failed" — and that single
  conflation is why its recall was always zero.
* ``denominator`` and ``n`` are recorded separately and always. Without them a
  reader cannot tell whether 100% means eighteen of eighteen or one of one.

:class:`MetricFamily` keeps deterministic and model-judged results in separate,
non-mergeable buckets. A judge score may inform a human; it may never gate a
release, and the report renders the two under different headings with different
caveats.
"""

from __future__ import annotations  # Postponed annotations

from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from pydantic import Field, model_validator

from synapse.schemas.base import SynapseModel, VersionedModel, require_timezone_aware
from synapse.schemas.enums import EvalCategory, EvalExpectedBehavior, RunMode


class MetricFamily(StrEnum):
    """Which kind of evaluator produced a metric."""

    DETERMINISTIC = "deterministic"  # Computed by arithmetic over recorded outputs. Reproducible, offline, may gate a release.
    JUDGE = "judge"  # Produced by a model. Advisory only, and never clinical validation.


class MetricValue(SynapseModel):
    """One metric, with everything needed to interpret it."""

    name: str = Field(min_length=1, description="Metric identifier, e.g. 'recall_at_5'.")
    family: MetricFamily = Field(description="Deterministic or model-judged. These never merge.")
    value: float | None = Field(
        default=None,
        description="The measurement, or null when undefined. Never zero-as-undefined.",
    )
    numerator: float | None = Field(
        default=None, description="Numerator, where the metric is a ratio."
    )
    denominator: int = Field(
        default=0,
        ge=0,
        description="Denominator. Recorded always, so '100%' can be read as 18/18 or 1/1.",
    )
    n_cases: int = Field(
        default=0,
        ge=0,
        description="Cases contributing to this metric, which may differ from the denominator.",
    )
    ci_low: float | None = Field(
        default=None, description="Lower confidence bound, when one is statistically appropriate."
    )
    ci_high: float | None = Field(default=None, description="Upper confidence bound.")
    ci_method: str = Field(
        default="none", description="'wilson', 'bootstrap_percentile', or 'none'."
    )
    undefined_reason: str | None = Field(
        default=None, description="Why the value is null, when it is."
    )
    unit: str = Field(
        default="ratio", description="'ratio', 'count', 'milliseconds', 'tokens', or 'usd'."
    )

    @model_validator(mode="after")
    def _validate_metric(self) -> MetricValue:
        """A null value must explain itself; a present value must not."""
        if self.value is None and not self.undefined_reason:
            # An unexplained null is indistinguishable from a bug in the harness.
            raise ValueError("a null metric value requires an undefined_reason")
        if self.value is not None and self.undefined_reason:
            raise ValueError("a metric with a value must not carry an undefined_reason")
        if (self.ci_low is None) != (self.ci_high is None):  # Half an interval is not an interval
            raise ValueError("confidence bounds must be supplied together or not at all")
        return self

    @property
    def is_defined(self) -> bool:
        """True when the metric produced a value."""
        return self.value is not None


class CaseOutcome(SynapseModel):
    """What happened for one case, in full."""

    case_id: str = Field(min_length=1)
    category: EvalCategory
    expected_behavior: EvalExpectedBehavior
    observed_behavior: EvalExpectedBehavior | None = Field(
        default=None, description="What the system actually did. Null when the case errored."
    )

    gating_eligible: bool = Field(
        default=False,
        description="Whether this case may influence a release decision. Synthetic and unreviewed cases are false.",
    )

    retrieval: dict[str, float | None] = Field(
        default_factory=dict, description="Per-case retrieval metrics, keyed by name."
    )
    answer: dict[str, float | None] = Field(
        default_factory=dict, description="Per-case answer metrics."
    )
    safety: dict[str, float | None] = Field(
        default_factory=dict, description="Per-case safety outcomes, 1.0 pass / 0.0 fail."
    )

    retrieved_document_ids: list[str] = Field(
        default_factory=list, description="Documents retrieved, in rank order, for audit."
    )
    forbidden_claims_hit: list[str] = Field(
        default_factory=list, description="Forbidden claims the answer contained."
    )
    missing_concepts: list[str] = Field(
        default_factory=list, description="Required concepts the answer omitted."
    )

    latency_ms: dict[str, float] = Field(
        default_factory=dict, description="Stage latencies: retrieval, rerank, generation, total."
    )
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float = Field(default=0.0, ge=0.0)

    errored: bool = Field(default=False, description="Whether the case failed to execute.")
    error_kind: str | None = Field(
        default=None,
        description="Machine-readable error class. Errors are recorded, never swallowed.",
    )
    timed_out: bool = Field(default=False)

    @model_validator(mode="after")
    def _validate_outcome(self) -> CaseOutcome:
        """An errored case must say what kind of error it hit."""
        if self.errored and not self.error_kind:
            # Requirement: never silently swallow evaluation errors. A case that
            # failed without recording why is exactly that failure mode.
            raise ValueError("an errored case must record an error_kind")
        return self


class JudgeProvenance(SynapseModel):
    """Everything needed to interpret — or reproduce — a model-judged metric."""

    provider: str = Field(min_length=1, description="e.g. 'openai'.")
    model: str = Field(min_length=1, description="Exact model identifier.")
    prompt_id: str = Field(min_length=1, description="Versioned prompt template identifier.")
    prompt_sha256: str = Field(min_length=1, description="Digest of the exact prompt text used.")
    judge_version: str = Field(min_length=1, description="Version of the judge implementation.")
    temperature: float = Field(default=0.0, ge=0.0)
    samples_per_item: int = Field(
        default=1, ge=1, description="Repeated judgements per item, for self-consistency."
    )
    agreement: float | None = Field(
        default=None,
        description="Agreement across repeated samples. Low agreement means the judge is unstable on this data.",
    )
    cache_hits: int = Field(default=0, ge=0)
    cache_misses: int = Field(default=0, ge=0)


class EvalRunResults(VersionedModel):
    """The complete result of one evaluation run."""

    SCHEMA_NAME: ClassVar[str] = "eval_run_results"
    SCHEMA_VERSION: ClassVar[str] = "1.0"

    # -- provenance ---------------------------------------------------------
    run_id: str = Field(min_length=1)
    started_at: datetime
    completed_at: datetime
    synapse_version: str = Field(min_length=1)
    git_commit: str | None = Field(default=None, description="Never fabricated.")
    mode: RunMode = Field(description="Offline-deterministic or live.")
    seed: int = Field(
        default=0, description="Seed for every stochastic step, so a run reproduces exactly."
    )

    dataset_id: str = Field(min_length=1)
    dataset_version: str = Field(min_length=1)
    corpus_version: str = Field(min_length=1)
    source_pack_version: str | None = Field(default=None)
    index_id: str | None = Field(default=None)
    pricing_version: str | None = Field(
        default=None, description="Version of the pricing table used for cost estimates."
    )

    # -- counts -------------------------------------------------------------
    cases_total: int = Field(ge=0)
    cases_evaluated: int = Field(ge=0)
    cases_skipped: int = Field(ge=0)
    cases_errored: int = Field(ge=0)
    cases_gating_eligible: int = Field(
        ge=0,
        description="Cases that may influence a release decision. Usually far smaller than cases_total.",
    )

    # -- metrics, kept in separate buckets ----------------------------------
    deterministic_metrics: dict[str, MetricValue] = Field(default_factory=dict)
    judge_metrics: dict[str, MetricValue] = Field(
        default_factory=dict,
        description="Advisory only. Never a release gate, never clinical validation.",
    )
    by_category: dict[str, dict[str, MetricValue]] = Field(
        default_factory=dict, description="Deterministic metrics broken down by query category."
    )

    judge_provenance: list[JudgeProvenance] = Field(default_factory=list)
    provenance_notes: list[str] = Field(
        default_factory=list, description="Statements a reader must see before the numbers."
    )
    error_summary: dict[str, int] = Field(
        default_factory=dict,
        description="Error counts by kind. Present even when empty, so silence is visible.",
    )

    @model_validator(mode="after")
    def _validate_results(self) -> EvalRunResults:
        """Reject naive timestamps, inverted intervals and counts that do not reconcile."""
        require_timezone_aware(self.started_at, "started_at")
        require_timezone_aware(self.completed_at, "completed_at")
        if self.completed_at < self.started_at:  # Usually a timezone bug rather than time travel
            raise ValueError("completed_at must not precede started_at")
        if self.cases_evaluated + self.cases_skipped != self.cases_total:
            raise ValueError("cases_evaluated + cases_skipped must equal cases_total")
        if self.cases_gating_eligible > self.cases_evaluated:
            raise ValueError("cases_gating_eligible must not exceed cases_evaluated")
        for name, metric in self.judge_metrics.items():
            if metric.family is not MetricFamily.JUDGE:
                # Structural guard against a deterministic metric being filed
                # under judge_metrics, or vice versa. The separation is the point.
                raise ValueError(f"judge_metrics['{name}'] must carry family 'judge'")
        for name, metric in self.deterministic_metrics.items():
            if metric.family is not MetricFamily.DETERMINISTIC:
                raise ValueError(
                    f"deterministic_metrics['{name}'] must carry family 'deterministic'"
                )
        return self


__all__ = ["CaseOutcome", "EvalRunResults", "JudgeProvenance", "MetricFamily", "MetricValue"]

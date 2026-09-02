"""
synapse.evals.gates
===================
Release quality gates: threshold evaluation against a checked-in baseline.

Three properties matter more than the arithmetic:

1. **Blocking and informational gates are separate categories, not severities.**
   An informational gate never affects the exit code, however badly it fails.
   Mixing them would let a latency wobble block a release, or — far worse — let
   a safety regression be argued down to "informational".

2. **A gate with too few observations is SKIPPED, not passed.** A metric over
   two cases cannot support a pass/fail decision, and silently passing it is how
   a gate suite becomes decorative. Skips are reported as prominently as
   failures.

3. **An incomparable baseline fails closed.** A baseline from a different
   dataset, corpus or seed is not a baseline; comparing against it produces the
   classic false green where a corpus change resets the reference point and
   everything appears to improve.

Nothing here is clinically validated. The configuration carries a ``status``
field, and every report generated against a non-approved file is banner-flagged.
"""

from __future__ import annotations  # Postponed annotations

import tomllib  # 3.11 stdlib; no PyYAML, no code-execution surface
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from synapse.errors import ArtifactSchemaError
from synapse.logging import get_logger
from synapse.schemas.evalrun import EvalRunResults

logger = get_logger(__name__)


class GateMode(StrEnum):
    """Whether a gate can fail a build."""

    BLOCKING = "blocking"  # Failure fails the build
    INFORMATIONAL = "informational"  # Reported, never fails the build


class GateOutcome(StrEnum):
    """What a gate decided."""

    PASS = "pass"  # noqa: S105  # A gate outcome, not a credential; S105 matches on the name alone
    FAIL = "fail"
    SKIP = "skip"  # Not enough observations, or the metric is absent


class Direction(StrEnum):
    """Which way is better for a metric."""

    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


@dataclass(frozen=True)
class GateSpec:
    """One configured threshold."""

    metric: str
    mode: GateMode
    direction: Direction
    category: str | None = None  # Set for per-category gates
    min_absolute: float | None = None
    max_absolute: float | None = None
    max_regression: float | None = None  # Absolute tolerance, for ratio metrics
    max_regression_relative: float | None = None  # Relative tolerance, for latency/cost
    max_increase: float | None = None  # Absolute increase tolerance, for counts
    rationale: str = ""

    @property
    def label(self) -> str:
        """Display name, qualified by category when there is one."""
        return f"{self.category}/{self.metric}" if self.category else self.metric


@dataclass
class GateConfig:
    """The whole gate configuration."""

    version: str
    status: str  # "provisional" | "approved"
    approved_by: str
    approved_at: str
    rationale: str
    min_denominator: int
    require_comparable_baseline: bool
    specs: list[GateSpec] = field(default_factory=list)

    @property
    def is_approved(self) -> bool:
        """True only when the file records a named approver.

        A ``status`` of "approved" with an empty ``approved_by`` is treated as
        unapproved: the field is the evidence, not the label.
        """
        return self.status == "approved" and bool(self.approved_by.strip())

    def provisional_banner(self) -> str:
        """Banner text emitted into every report generated from this config."""
        if self.is_approved:
            return f"Gate configuration `{self.version}` approved by `{self.approved_by}` on {self.approved_at}."
        return (
            f"**PROVISIONAL GATE CONFIGURATION (`{self.version}`).** These thresholds are conservative "
            "placeholders, not clinically justified targets, and have not been approved by clinical or "
            "product leadership. Results below are engineering regression signals only and must not be "
            "described as clinical validation."
        )


def _parse_spec(metric: str, raw: dict, *, category: str | None = None) -> GateSpec:
    """Build one gate spec from its TOML table."""
    try:
        return GateSpec(
            metric=metric,
            category=category,
            mode=GateMode(raw.get("mode", "informational")),
            direction=Direction(raw.get("direction", "higher_is_better")),
            min_absolute=raw.get("min_absolute"),
            max_absolute=raw.get("max_absolute"),
            max_regression=raw.get("max_regression"),
            max_regression_relative=raw.get("max_regression_relative"),
            max_increase=raw.get("max_increase"),
            rationale=str(raw.get("rationale", "")).strip(),
        )
    except ValueError as exc:  # Unknown mode or direction
        raise ArtifactSchemaError(problem="invalid gate specification", metric=metric) from exc


def load_config(path: Path) -> GateConfig:
    """Read the gate configuration from TOML."""
    if not path.is_file():
        raise ArtifactSchemaError(path=path, problem="quality-gate configuration not found")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))  # SAFE: tomllib cannot execute code
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ArtifactSchemaError(
            path=path, problem="quality-gate configuration is not valid TOML"
        ) from exc

    meta = data.get("meta", {})
    defaults = data.get("defaults", {})
    config = GateConfig(
        version=str(meta.get("version", "unversioned")),
        status=str(meta.get("status", "provisional")),
        approved_by=str(meta.get("approved_by", "")),
        approved_at=str(meta.get("approved_at", "")),
        rationale=str(meta.get("rationale", "")).strip(),
        min_denominator=int(defaults.get("min_denominator", 5)),
        require_comparable_baseline=bool(defaults.get("require_comparable_baseline", True)),
    )

    for metric, raw in data.get("gates", {}).items():
        config.specs.append(_parse_spec(metric, raw))
    for category, metrics in data.get("category_gates", {}).items():
        for metric, raw in metrics.items():
            config.specs.append(_parse_spec(metric, raw, category=category))

    logger.info(
        "gate configuration loaded",
        extra={
            "version": config.version,
            "specs": len(config.specs),
            "approved": config.is_approved,
        },
    )
    return config


@dataclass(frozen=True)
class GateResult:
    """The outcome of evaluating one gate."""

    spec: GateSpec
    outcome: GateOutcome
    current: float | None
    baseline: float | None
    delta: float | None
    detail: str

    @property
    def blocks(self) -> bool:
        """True when this result should fail the build."""
        return self.outcome is GateOutcome.FAIL and self.spec.mode is GateMode.BLOCKING

    def as_dict(self) -> dict[str, object]:
        """Machine-readable form."""
        return {
            "metric": self.spec.metric,
            "category": self.spec.category,
            "mode": self.spec.mode.value,
            "outcome": self.outcome.value,
            "current": self.current,
            "baseline": self.baseline,
            "delta": round(self.delta, 5) if self.delta is not None else None,
            "detail": self.detail,
            "blocks": self.blocks,
        }


def _metric_from(results: EvalRunResults, spec: GateSpec):
    """Look up a metric, honouring the category qualifier."""
    if spec.category is not None:
        return results.by_category.get(spec.category, {}).get(spec.metric)
    return results.deterministic_metrics.get(spec.metric)


def evaluate_gate(
    spec: GateSpec,
    current: EvalRunResults,
    baseline: EvalRunResults | None,
    *,
    min_denominator: int,
) -> GateResult:
    """Evaluate one gate against a run and its baseline."""
    current_metric = _metric_from(current, spec)
    if current_metric is None or current_metric.value is None:
        # Absent or undefined. Skipped rather than passed: a gate that silently
        # passes on a missing metric protects nothing.
        reason = (
            "metric absent from this run"
            if current_metric is None
            else (current_metric.undefined_reason or "metric undefined")
        )
        return GateResult(spec, GateOutcome.SKIP, None, None, None, reason)

    if current_metric.denominator < min_denominator:
        return GateResult(
            spec,
            GateOutcome.SKIP,
            current_metric.value,
            None,
            None,
            f"only {current_metric.denominator} observation(s); below the minimum of {min_denominator} for a pass/fail decision",
        )

    value = current_metric.value
    failures: list[str] = []

    # -- absolute floors and ceilings --------------------------------------
    if spec.min_absolute is not None and value < spec.min_absolute:
        failures.append(f"{value:.4f} is below the minimum {spec.min_absolute:.4f}")
    if spec.max_absolute is not None and value > spec.max_absolute:
        failures.append(f"{value:.4f} exceeds the maximum {spec.max_absolute:.4f}")

    # -- regression against the baseline -----------------------------------
    baseline_metric = _metric_from(baseline, spec) if baseline is not None else None
    baseline_value = baseline_metric.value if baseline_metric else None
    delta = (value - baseline_value) if baseline_value is not None else None

    if delta is not None:
        worsened = delta < 0 if spec.direction is Direction.HIGHER_IS_BETTER else delta > 0
        magnitude = abs(delta)

        if (
            spec.max_increase is not None
            and spec.direction is Direction.LOWER_IS_BETTER
            and delta > spec.max_increase
        ):
            # Absolute-count gate: "the emergency false-negative count does not
            # increase" is a statement about counts, and max_increase=0 means
            # exactly that.
            failures.append(f"increased by {delta:+.0f}, tolerance is +{spec.max_increase:.0f}")
        if worsened and spec.max_regression is not None and magnitude > spec.max_regression:
            failures.append(f"regressed by {delta:+.4f}, tolerance is {spec.max_regression:.4f}")
        if worsened and spec.max_regression_relative is not None and baseline_value:
            relative = magnitude / abs(baseline_value)
            if relative > spec.max_regression_relative:
                failures.append(
                    f"regressed by {relative:.1%}, relative tolerance is {spec.max_regression_relative:.1%}"
                )

    if failures:
        return GateResult(spec, GateOutcome.FAIL, value, baseline_value, delta, "; ".join(failures))

    detail = f"{value:.4f}"
    if delta is not None:
        detail += f" (baseline {baseline_value:.4f}, delta {delta:+.4f})"
    elif baseline is not None:
        detail += " (no baseline value for this metric)"
    return GateResult(spec, GateOutcome.PASS, value, baseline_value, delta, detail)


@dataclass
class GateReport:
    """The outcome of the whole gate suite."""

    config: GateConfig
    results: list[GateResult] = field(default_factory=list)
    baseline_comparable: bool = True
    incomparable_reasons: list[str] = field(default_factory=list)
    run_id: str = ""
    baseline_run_id: str = ""
    cases_gating_eligible: int = 0

    @property
    def blocking_failures(self) -> list[GateResult]:
        """Failures that fail the build."""
        return [result for result in self.results if result.blocks]

    @property
    def informational_failures(self) -> list[GateResult]:
        """Failures that are reported but do not fail the build."""
        return [
            result
            for result in self.results
            if result.outcome is GateOutcome.FAIL and result.spec.mode is GateMode.INFORMATIONAL
        ]

    @property
    def skipped(self) -> list[GateResult]:
        """Gates that could not be decided."""
        return [result for result in self.results if result.outcome is GateOutcome.SKIP]

    @property
    def passed(self) -> bool:
        """True when nothing blocking failed.

        An incomparable baseline fails the suite when the config requires one —
        checked here rather than left to the caller.
        """
        if self.config.require_comparable_baseline and not self.baseline_comparable:
            return False
        return not self.blocking_failures

    def as_dict(self) -> dict[str, object]:
        """Machine-readable form for CI."""
        return {
            "passed": self.passed,
            "gate_config_version": self.config.version,
            "gate_config_approved": self.config.is_approved,
            "run_id": self.run_id,
            "baseline_run_id": self.baseline_run_id,
            "baseline_comparable": self.baseline_comparable,
            "incomparable_reasons": self.incomparable_reasons,
            "cases_gating_eligible": self.cases_gating_eligible,
            "counts": {
                "blocking_failures": len(self.blocking_failures),
                "informational_failures": len(self.informational_failures),
                "skipped": len(self.skipped),
                "total": len(self.results),
            },
            "gates": [result.as_dict() for result in self.results],
        }


def evaluate(
    config: GateConfig,
    current: EvalRunResults,
    baseline: EvalRunResults | None = None,
) -> GateReport:
    """Evaluate every configured gate."""
    report = GateReport(
        config=config,
        run_id=current.run_id,
        baseline_run_id=baseline.run_id if baseline else "",
        cases_gating_eligible=current.cases_gating_eligible,
    )

    if baseline is not None:
        from synapse.evals.compare import check_comparability

        report.incomparable_reasons = check_comparability(baseline, current)
        report.baseline_comparable = not report.incomparable_reasons
    elif config.require_comparable_baseline:
        report.baseline_comparable = False
        report.incomparable_reasons = ["no baseline supplied"]

    for spec in config.specs:
        report.results.append(
            evaluate_gate(spec, current, baseline, min_denominator=config.min_denominator)
        )

    logger.info(
        "gates evaluated",
        extra={
            "passed": report.passed,
            "blocking_failures": len(report.blocking_failures),
            "skipped": len(report.skipped),
        },
    )
    return report


__all__ = [
    "Direction",
    "GateConfig",
    "GateMode",
    "GateOutcome",
    "GateReport",
    "GateResult",
    "GateSpec",
    "evaluate",
    "evaluate_gate",
    "load_config",
]

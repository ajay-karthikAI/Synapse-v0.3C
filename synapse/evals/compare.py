"""
synapse.evals.compare
=====================
Baseline comparison and regression reporting.

Two guards make a comparison trustworthy, and both are easy to omit:

1. **Comparability is checked before anything is compared.** A run against a
   different dataset version, corpus version, or seed is not comparable with
   the baseline, and reporting a delta anyway is the classic false green — a
   corpus change silently resets the reference point and every metric appears
   to improve.
2. **Regression is judged per metric, with a direction.** Most metrics improve
   as they rise, but ``unsupported_claim_rate``, ``error_rate`` and the
   violation rates improve as they *fall*. Treating every delta as
   higher-is-better would report a doubled hallucination rate as an
   improvement.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass, field

from synapse.schemas.evalrun import EvalRunResults

# Metrics where a LOWER value is better. Everything not listed is
# higher-is-better. Getting this wrong inverts the sign of a safety regression,
# so the set is explicit rather than inferred from a name pattern.
LOWER_IS_BETTER = frozenset(
    {
        "unsupported_claim_rate",
        "emergency_false_negative_rate",
        "emergency_false_positive_rate",
        "medication_boundary_violation_rate",
        "forbidden_claim_violations",
        "error_rate",
        "timeout_rate",
        "readability_grade",  # A patient-facing answer should read at a LOWER grade level
        "latency_total_p50_ms",
        "latency_total_p95_ms",
        "estimated_cost_usd",
    }
)

# Default tolerance: a change smaller than this is noise, not a regression.
# Applied to ratio metrics; relative tolerance is used for the rest.
DEFAULT_ABSOLUTE_TOLERANCE = 0.01
DEFAULT_RELATIVE_TOLERANCE = 0.05

# Metrics whose regression is reported as CRITICAL regardless of size. A
# one-case drop in emergency sensitivity is not a rounding error.
CRITICAL_METRICS = frozenset(
    {
        "emergency_sensitivity",
        "negation_accuracy",
        "prompt_injection_resistance",
        "medication_boundary_violation_rate",
    }
)


@dataclass(frozen=True)
class MetricDelta:
    """Change in one metric between a baseline and a current run."""

    name: str
    baseline: float | None
    current: float | None
    delta: float | None
    is_regression: bool
    is_critical: bool
    note: str = ""

    def as_dict(self) -> dict[str, object]:
        """Machine-readable form."""
        return {
            "metric": self.name,
            "baseline": self.baseline,
            "current": self.current,
            "delta": round(self.delta, 4) if self.delta is not None else None,
            "regression": self.is_regression,
            "critical": self.is_critical,
            "note": self.note,
        }


@dataclass
class ComparisonReport:
    """The outcome of comparing a run with a baseline."""

    comparable: bool
    incomparable_reasons: list[str] = field(default_factory=list)
    deltas: list[MetricDelta] = field(default_factory=list)
    category_regressions: dict[str, list[MetricDelta]] = field(default_factory=dict)

    @property
    def regressions(self) -> list[MetricDelta]:
        """Deltas judged to be regressions, worst first."""
        found = [delta for delta in self.deltas if delta.is_regression]
        # Critical regressions first, then by magnitude, so the report leads
        # with what matters.
        found.sort(key=lambda d: (not d.is_critical, -(abs(d.delta) if d.delta is not None else 0)))
        return found

    @property
    def critical_regressions(self) -> list[MetricDelta]:
        """Regressions in safety-critical metrics."""
        return [delta for delta in self.regressions if delta.is_critical]

    def as_dict(self) -> dict[str, object]:
        """Machine-readable form for CI."""
        return {
            "comparable": self.comparable,
            "incomparable_reasons": self.incomparable_reasons,
            "regression_count": len(self.regressions),
            "critical_regression_count": len(self.critical_regressions),
            "deltas": [delta.as_dict() for delta in self.deltas],
            "category_regressions": {
                category: [delta.as_dict() for delta in deltas]
                for category, deltas in sorted(self.category_regressions.items())
            },
        }


def check_comparability(baseline: EvalRunResults, current: EvalRunResults) -> list[str]:
    """Reasons the two runs cannot be compared, if any."""
    reasons: list[str] = []
    if baseline.dataset_version != current.dataset_version:
        reasons.append(
            f"dataset version differs: {baseline.dataset_version} vs {current.dataset_version}"
        )
    if baseline.corpus_version != current.corpus_version:
        # The one that silently invalidates everything: relevance labels are
        # pinned to a corpus version.
        reasons.append(
            f"corpus version differs: {baseline.corpus_version} vs {current.corpus_version}"
        )
    if baseline.seed != current.seed:
        reasons.append(
            f"seed differs: {baseline.seed} vs {current.seed}; confidence intervals are not comparable"
        )
    if baseline.mode is not current.mode:
        reasons.append(f"run mode differs: {baseline.mode.value} vs {current.mode.value}")
    return reasons


def _delta_for(
    name: str,
    baseline_value: float | None,
    current_value: float | None,
    *,
    absolute_tolerance: float,
    relative_tolerance: float,
) -> MetricDelta:
    """Compute one metric delta and decide whether it is a regression."""
    critical = name in CRITICAL_METRICS

    if baseline_value is None or current_value is None:
        # A metric that appeared or disappeared is worth reporting but is not a
        # regression: there is no comparison to make.
        note = (
            "metric undefined in baseline"
            if baseline_value is None
            else "metric undefined in current run"
        )
        return MetricDelta(name, baseline_value, current_value, None, False, critical, note)

    delta = current_value - baseline_value
    lower_is_better = name in LOWER_IS_BETTER
    worsened = delta > 0 if lower_is_better else delta < 0

    magnitude = abs(delta)
    if baseline_value != 0:
        # Ratio metrics use an absolute tolerance; anything else (latency,
        # cost, token counts) uses a relative one, since a 10ms change means
        # something very different at 50ms and at 5000ms.
        tolerance = (
            absolute_tolerance
            if 0 <= baseline_value <= 1
            else abs(baseline_value) * relative_tolerance
        )
    else:
        tolerance = absolute_tolerance

    # A critical metric regresses on ANY worsening; tolerance does not apply.
    is_regression = worsened and (critical or magnitude > tolerance)
    note = "critical metric: any worsening is reported" if critical and worsened else ""
    return MetricDelta(name, baseline_value, current_value, delta, is_regression, critical, note)


def compare_runs(
    baseline: EvalRunResults,
    current: EvalRunResults,
    *,
    absolute_tolerance: float = DEFAULT_ABSOLUTE_TOLERANCE,
    relative_tolerance: float = DEFAULT_RELATIVE_TOLERANCE,
) -> ComparisonReport:
    """Compare a run against a baseline, per metric and per category."""
    reasons = check_comparability(baseline, current)
    report = ComparisonReport(comparable=not reasons, incomparable_reasons=reasons)

    names = sorted(set(baseline.deterministic_metrics) | set(current.deterministic_metrics))
    for name in names:
        baseline_metric = baseline.deterministic_metrics.get(name)
        current_metric = current.deterministic_metrics.get(name)
        report.deltas.append(
            _delta_for(
                name,
                baseline_metric.value if baseline_metric else None,
                current_metric.value if current_metric else None,
                absolute_tolerance=absolute_tolerance,
                relative_tolerance=relative_tolerance,
            )
        )

    # Per-category regressions, which is where a targeted failure shows up
    # while the overall aggregate stays flat.
    for category in sorted(set(baseline.by_category) | set(current.by_category)):
        baseline_metrics = baseline.by_category.get(category, {})
        current_metrics = current.by_category.get(category, {})
        category_deltas = []
        for name in sorted(set(baseline_metrics) | set(current_metrics)):
            delta = _delta_for(
                name,
                baseline_metrics[name].value if name in baseline_metrics else None,
                current_metrics[name].value if name in current_metrics else None,
                absolute_tolerance=absolute_tolerance,
                relative_tolerance=relative_tolerance,
            )
            if delta.is_regression:
                category_deltas.append(delta)
        if category_deltas:
            report.category_regressions[category] = category_deltas

    return report


def render_comparison(report: ComparisonReport, baseline_run_id: str) -> str:
    """Render the comparison as a Markdown section."""
    lines = ["## Comparison against baseline", "", f"Baseline run: `{baseline_run_id}`", ""]

    if not report.comparable:
        # Stated loudly and first: an incomparable delta is worse than none.
        lines.append(
            "> **NOT COMPARABLE.** Deltas below are shown for information only and must not"
        )
        lines.append("> be read as regressions or improvements.")
        lines.append("")
        lines.extend(f"- {reason}" for reason in report.incomparable_reasons)
        lines.append("")

    critical = report.critical_regressions
    if critical:
        lines.append(f"### {len(critical)} CRITICAL regression(s)")
        lines.append("")
        lines.append("| Metric | Baseline | Current | Delta |")
        lines.append("|---|---|---|---|")
        for delta in critical:
            lines.append(
                f"| `{delta.name}` | {delta.baseline:.3f} | {delta.current:.3f} | {delta.delta:+.3f} |"
            )
        lines.append("")

    other = [delta for delta in report.regressions if not delta.is_critical]
    if other:
        lines.append(f"### {len(other)} other regression(s)")
        lines.append("")
        lines.append("| Metric | Baseline | Current | Delta |")
        lines.append("|---|---|---|---|")
        for delta in other:
            lines.append(
                f"| `{delta.name}` | {delta.baseline:.3f} | {delta.current:.3f} | {delta.delta:+.3f} |"
            )
        lines.append("")

    if report.category_regressions:
        lines.append("### Regressions by category")
        lines.append("")
        for category, deltas in sorted(report.category_regressions.items()):
            names = ", ".join(f"`{d.name}` ({d.delta:+.3f})" for d in deltas)
            lines.append(f"- **{category}**: {names}")
        lines.append("")

    if not report.regressions:
        lines.append("No regressions detected.")
        lines.append("")

    return "\n".join(lines)


__all__ = [
    "CRITICAL_METRICS",
    "DEFAULT_ABSOLUTE_TOLERANCE",
    "DEFAULT_RELATIVE_TOLERANCE",
    "LOWER_IS_BETTER",
    "ComparisonReport",
    "MetricDelta",
    "check_comparability",
    "compare_runs",
    "render_comparison",
]

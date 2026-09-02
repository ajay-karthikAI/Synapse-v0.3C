"""
synapse.evals.report
====================
Emits the four required artifacts:

    results.json    the complete typed run record
    summary.json    a small machine-readable digest for CI
    report.md       the human-readable report
    per_case.jsonl  one line per case, for drilling into failures

Two properties are enforced throughout:

* **Deterministic and judge metrics never share a table.** They are rendered
  under separate headings with separate caveats, so a reader cannot
  accidentally quote a model's opinion as a measurement.
* **Caveats come before numbers.** ``provenance_notes`` renders above the
  metrics table, not in a footnote. A reader who stops after the first table
  must already have been told that nothing here is release-gating.
"""

from __future__ import annotations  # Postponed annotations

import json
from dataclasses import dataclass
from pathlib import Path

from synapse.evals.judges import JUDGE_CAVEAT
from synapse.logging import get_logger
from synapse.schemas.evalrun import CaseOutcome, EvalRunResults, MetricValue

logger = get_logger(__name__)

RESULTS_FILENAME = "results.json"
SUMMARY_FILENAME = "summary.json"
REPORT_FILENAME = "report.md"
PER_CASE_FILENAME = "per_case.jsonl"

# Metrics surfaced in summary.json and at the top of report.md. Chosen because
# each answers a distinct question a reader has first; the full set is in
# results.json.
HEADLINE_METRICS = (
    "recall_at_5",
    "ndcg_at_5",
    "mrr",
    "citation_correctness",
    "citation_completeness",
    "unsupported_claim_rate",
    "emergency_sensitivity",
    "emergency_specificity",
    "negation_accuracy",
    "abstention_correctness",
    "prompt_injection_resistance",
    "medication_boundary_violation_rate",
    "followup_resolution_at_5",
)


def _format_metric(metric: MetricValue) -> str:
    """Render one metric as a table cell, always showing its denominator."""
    if metric.value is None:
        return f"— ({metric.undefined_reason})"
    if metric.unit == "ratio":
        rendered = f"{metric.value:.1%}"
    elif metric.unit in ("milliseconds", "tokens"):
        rendered = f"{metric.value:,.0f}"
    elif metric.unit == "usd":
        rendered = f"${metric.value:.4f}"
    else:
        rendered = f"{metric.value:.3f}"
    # The denominator travels with every number, so "100%" can be read as 18/18
    # rather than 1/1.
    suffix = f" (n={metric.denominator})"
    if metric.ci_low is not None and metric.ci_high is not None and metric.unit == "ratio":
        suffix += f" [{metric.ci_low:.1%}–{metric.ci_high:.1%}]"  # noqa: RUF001  # En dash is deliberate: it reads as a range, a hyphen reads as subtraction
    return rendered + suffix


def write_results(directory: Path, results: EvalRunResults) -> Path:
    """Write the complete typed run record."""
    path = directory / RESULTS_FILENAME
    path.write_text(
        json.dumps(results.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False)
        + "\n",  # Sorted so two runs diff cleanly
        encoding="utf-8",
    )
    return path


def write_summary(directory: Path, results: EvalRunResults) -> Path:
    """Write a compact digest for CI to consume."""
    summary = {
        "run_id": results.run_id,
        "dataset_version": results.dataset_version,
        "corpus_version": results.corpus_version,
        "mode": results.mode.value,
        "seed": results.seed,
        "cases_total": results.cases_total,
        "cases_evaluated": results.cases_evaluated,
        "cases_skipped": results.cases_skipped,
        "cases_errored": results.cases_errored,
        "cases_gating_eligible": results.cases_gating_eligible,
        # Stated as its own flag so CI can branch on it without parsing prose.
        "release_gating_capable": results.cases_gating_eligible > 0,
        "headline_metrics": {
            name: {
                "value": results.deterministic_metrics[name].value,
                "denominator": results.deterministic_metrics[name].denominator,
                "ci_low": results.deterministic_metrics[name].ci_low,
                "ci_high": results.deterministic_metrics[name].ci_high,
            }
            for name in HEADLINE_METRICS
            if name in results.deterministic_metrics
        },
        "judge_metrics_present": bool(results.judge_metrics),
        "error_summary": results.error_summary,
        "provenance_notes": results.provenance_notes,
    }
    path = directory / SUMMARY_FILENAME
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_per_case(directory: Path, outcomes: list[CaseOutcome]) -> Path:
    """Write one JSON line per case, for drilling into individual failures."""
    path = directory / PER_CASE_FILENAME
    with path.open("w", encoding="utf-8") as handle:
        for outcome in outcomes:
            handle.write(
                json.dumps(outcome.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
                + "\n"
            )
    return path


def render_markdown(results: EvalRunResults, comparison_section: str = "") -> str:
    """Render the human-readable report."""
    lines: list[str] = [
        f"# Evaluation Report — `{results.run_id}`",
        "",
        f"**Dataset** `{results.dataset_id}@{results.dataset_version}` · "
        f"**Corpus** `{results.corpus_version}` · **Mode** `{results.mode.value}` · **Seed** `{results.seed}`",
        f"**Synapse** `{results.synapse_version}` · **Commit** `{results.git_commit or 'unknown'}` · "
        f"**Pricing** `{results.pricing_version or 'not configured'}`",
        "",
    ]

    # Caveats FIRST. A reader who stops after the first table must already know
    # what these numbers are and are not.
    lines.append("## Before reading the numbers")
    lines.append("")
    lines.extend(f"- {note}" for note in results.provenance_notes)
    lines.append("")

    lines.append("## Cases")
    lines.append("")
    lines.append("| | Count |")
    lines.append("|---|---|")
    lines.append(f"| Total in dataset | {results.cases_total} |")
    lines.append(f"| Evaluated | {results.cases_evaluated} |")
    lines.append(
        f"| Skipped (no ground truth, excluded, version mismatch) | {results.cases_skipped} |"
    )
    lines.append(f"| Errored | {results.cases_errored} |")
    lines.append(f"| **Release-gating eligible** | **{results.cases_gating_eligible}** |")
    lines.append("")

    lines.append("## Deterministic metrics")
    lines.append("")
    lines.append("Computed by arithmetic over recorded outputs. Reproducible offline.")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    for name in HEADLINE_METRICS:
        metric = results.deterministic_metrics.get(name)
        if metric is not None:
            lines.append(f"| `{name}` | {_format_metric(metric)} |")
    lines.append("")

    operational = [
        n
        for n in (
            "latency_total_p50_ms",
            "latency_total_p95_ms",
            "total_prompt_tokens",
            "total_completion_tokens",
            "estimated_cost_usd",
            "error_rate",
            "timeout_rate",
        )
        if n in results.deterministic_metrics
    ]
    if operational:
        lines.append("### Operational")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        lines.extend(
            f"| `{name}` | {_format_metric(results.deterministic_metrics[name])} |"
            for name in operational
        )
        lines.append("")

    if results.by_category:
        lines.append("### By category")
        lines.append("")
        lines.append(
            "An aggregate hides the failures that matter most: a system can score well overall"
        )
        lines.append("while failing every negated-emergency case.")
        lines.append("")
        breakdown = ("behavior_correct", "recall_at_5", "citation_correctness")
        lines.append("| Category | " + " | ".join(f"`{m}`" for m in breakdown) + " |")
        lines.append("|---" * (len(breakdown) + 1) + "|")
        for category, metrics in results.by_category.items():
            cells = [_format_metric(metrics[m]) if m in metrics else "—" for m in breakdown]
            lines.append(f"| `{category}` | " + " | ".join(cells) + " |")
        lines.append("")

    # Judge metrics get their own heading and their own caveat, always.
    lines.append("## Model-judged metrics")
    lines.append("")
    lines.append(f"> {JUDGE_CAVEAT}")
    lines.append("")
    if results.judge_metrics:
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        lines.extend(
            f"| `{name}` | {_format_metric(metric)} |"
            for name, metric in sorted(results.judge_metrics.items())
        )
        lines.append("")
        for provenance in results.judge_provenance:
            lines.append(
                f"- `{provenance.provider}/{provenance.model}` · prompt `{provenance.prompt_id}` "
                f"(`{provenance.prompt_sha256[:12]}`) · {provenance.samples_per_item} sample(s) · "
                f"cache {provenance.cache_hits} hit / {provenance.cache_misses} miss"
            )
        lines.append("")
    else:
        lines.append("No judge ran. Judges are opt-in and unavailable in offline mode.")
        lines.append("")

    if results.error_summary:
        lines.append("## Errors")
        lines.append("")
        lines.append("| Kind | Count |")
        lines.append("|---|---|")
        lines.extend(f"| `{kind}` | {count} |" for kind, count in results.error_summary.items())
        lines.append("")

    if comparison_section:
        lines.append(comparison_section)

    return "\n".join(lines) + "\n"


@dataclass
class ReportPaths:
    """Where the four artifacts landed."""

    results: Path
    summary: Path
    report: Path
    per_case: Path


def write_all(
    directory: Path,
    results: EvalRunResults,
    outcomes: list[CaseOutcome],
    comparison_section: str = "",
) -> ReportPaths:
    """Write all four required artifacts."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = ReportPaths(
        results=write_results(directory, results),
        summary=write_summary(directory, results),
        report=directory / REPORT_FILENAME,
        per_case=write_per_case(directory, outcomes),
    )
    paths.report.write_text(render_markdown(results, comparison_section), encoding="utf-8")
    logger.info(
        "evaluation artifacts written", extra={"directory": directory.name, "cases": len(outcomes)}
    )
    return paths


__all__ = [
    "HEADLINE_METRICS",
    "PER_CASE_FILENAME",
    "REPORT_FILENAME",
    "RESULTS_FILENAME",
    "SUMMARY_FILENAME",
    "ReportPaths",
    "render_markdown",
    "write_all",
]

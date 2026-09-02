"""
synapse.cli.check_gates
=======================
Evaluate release quality gates and write the GitHub Actions job summary.

    python -m synapse.cli.check_gates \\
      --results artifacts/evals/candidate/results.json \\
      --baseline evals/baselines/approved/results.json \\
      --config configs/quality-gates.toml \\
      --summary-out "$GITHUB_STEP_SUMMARY"

Exit codes:

    0  every blocking gate passed
    1  a blocking gate failed, or the baseline is not comparable
    2  a configuration or input error

The failure report names **which** gates regressed, by how much, against what,
and — for per-category gates — in which category. A gate suite that reports only
"FAILED" trains people to re-run it rather than read it.
"""

from __future__ import annotations  # Postponed annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from synapse import __version__ as synapse_version
from synapse.errors import SynapseArtifactError
from synapse.evals.gates import GateOutcome, GateReport, evaluate, load_config
from synapse.logging import configure_logging, get_logger
from synapse.schemas.evalrun import EvalRunResults

logger = get_logger(__name__)

# Emitted into every summary. The evaluation dataset that ships with this
# repository is synthetic and unreviewed, so nothing computed from it may be
# described as clinical validation — stated here rather than left to the reader.
NON_VALIDATION_NOTICE = (
    "These are engineering regression signals computed from a **synthetic, unreviewed** "
    "evaluation dataset. They are **not clinical validation**, they do not establish that "
    "the system is safe or accurate, and no clinician has reviewed the cases they are "
    "computed from."
)

_OUTCOME_ICON = {GateOutcome.PASS: "✅", GateOutcome.FAIL: "❌", GateOutcome.SKIP: "⏭️"}


def _load_results(path: Path, label: str) -> EvalRunResults:
    """Read and validate an evaluation results file."""
    if not path.is_file():
        raise SynapseArtifactError(problem=f"{label} results file not found", path=path)
    try:
        return EvalRunResults.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )  # SAFE deserialisation
    except json.JSONDecodeError as exc:
        raise SynapseArtifactError(
            problem=f"{label} results file is not valid JSON", path=path
        ) from exc
    except ValidationError as exc:
        raise SynapseArtifactError(
            problem=f"{label} results file failed schema validation",
            path=path,
            error_count=exc.error_count(),
        ) from exc


def render_summary(report: GateReport) -> str:
    """Render the Markdown job summary.

    Ordered so the two things a reader needs first come first: whether the build
    passed, and whether these numbers mean anything.
    """
    lines: list[str] = ["# Evaluation quality gates", ""]

    verdict = "✅ **PASSED**" if report.passed else "❌ **FAILED**"
    lines.append(
        f"{verdict} — {len(report.blocking_failures)} blocking failure(s), "
        f"{len(report.informational_failures)} informational, {len(report.skipped)} skipped."
    )
    lines.append("")

    # Caveats before numbers, always.
    lines.append(f"> {report.config.provisional_banner()}")
    lines.append(">")
    lines.append(f"> {NON_VALIDATION_NOTICE}")
    lines.append("")

    lines.append("| | |")
    lines.append("|---|---|")
    lines.append(f"| Run | `{report.run_id}` |")
    lines.append(f"| Baseline | `{report.baseline_run_id or 'none'}` |")
    lines.append(
        f"| Gate config | `{report.config.version}` ({'approved' if report.config.is_approved else 'PROVISIONAL'}) |"
    )
    lines.append(f"| Release-gating cases | **{report.cases_gating_eligible}** |")
    lines.append("")

    if report.cases_gating_eligible == 0:
        # The single most important caveat: gates evaluated over cases that may
        # not gate a release are a smoke test, not a gate.
        lines.append(
            "> ⚠️ **No case in this run is release-gating.** Every case is synthetic or unreviewed, "
            "so the gates below are exercising the mechanism, not certifying the system."
        )
        lines.append("")

    if not report.baseline_comparable:
        lines.append("### ❌ Baseline is not comparable")
        lines.append("")
        lines.append(
            "Regression gates cannot be evaluated. A baseline from a different dataset, corpus or "
            "seed produces a false green: the reference point moves and everything appears to improve."
        )
        lines.append("")
        lines.extend(f"- {reason}" for reason in report.incomparable_reasons)
        lines.append("")

    if report.blocking_failures:
        lines.append(f"### ❌ {len(report.blocking_failures)} blocking gate(s) failed")
        lines.append("")
        lines.append("| Gate | Current | Baseline | Delta | Why |")
        lines.append("|---|---|---|---|---|")
        for result in report.blocking_failures:
            baseline = f"{result.baseline:.4f}" if result.baseline is not None else "—"
            delta = f"{result.delta:+.4f}" if result.delta is not None else "—"
            current = f"{result.current:.4f}" if result.current is not None else "—"
            lines.append(
                f"| `{result.spec.label}` | {current} | {baseline} | {delta} | {result.detail} |"
            )
        lines.append("")

    if report.informational_failures:
        lines.append(f"### ⚠️ {len(report.informational_failures)} informational gate(s) failed")
        lines.append("")
        lines.append("These do not fail the build.")
        lines.append("")
        lines.append("| Gate | Current | Baseline | Delta | Why |")
        lines.append("|---|---|---|---|---|")
        for result in report.informational_failures:
            baseline = f"{result.baseline:.4f}" if result.baseline is not None else "—"
            delta = f"{result.delta:+.4f}" if result.delta is not None else "—"
            lines.append(
                f"| `{result.spec.label}` | {result.current:.4f} | {baseline} | {delta} | {result.detail} |"
            )
        lines.append("")

    if report.skipped:
        lines.append(f"<details><summary>⏭️ {len(report.skipped)} gate(s) skipped</summary>")
        lines.append("")
        lines.append(
            "A skipped gate protects nothing. Most skips mean too few observations to decide."
        )
        lines.append("")
        lines.append("| Gate | Why |")
        lines.append("|---|---|")
        lines.extend(f"| `{result.spec.label}` | {result.detail} |" for result in report.skipped)
        lines.append("")
        lines.append("</details>")
        lines.append("")

    passed = [r for r in report.results if r.outcome is GateOutcome.PASS]
    if passed:
        lines.append(f"<details><summary>✅ {len(passed)} gate(s) passed</summary>")
        lines.append("")
        lines.append("| Gate | Mode | Value |")
        lines.append("|---|---|---|")
        lines.extend(f"| `{r.spec.label}` | {r.spec.mode.value} | {r.detail} |" for r in passed)
        lines.append("")
        lines.append("</details>")
        lines.append("")

    lines.append("---")
    lines.append(f"`synapse {synapse_version}` · gate config `{report.config.version}`")
    return "\n".join(lines) + "\n"


def render_console(report: GateReport) -> str:
    """Render the failure report for a terminal."""
    lines = [
        f"Quality gates: {'PASSED' if report.passed else 'FAILED'}",
        f"  gate config      : {report.config.version} ({'approved' if report.config.is_approved else 'PROVISIONAL'})",
        f"  run / baseline   : {report.run_id} / {report.baseline_run_id or 'none'}",
        f"  gating-eligible  : {report.cases_gating_eligible} case(s)",
        f"  blocking failures: {len(report.blocking_failures)}",
        f"  informational    : {len(report.informational_failures)}",
        f"  skipped          : {len(report.skipped)}",
    ]
    if not report.baseline_comparable:
        lines.append("\n  BASELINE NOT COMPARABLE:")
        lines.extend(f"    - {reason}" for reason in report.incomparable_reasons)
    if report.blocking_failures:
        lines.append("\n  BLOCKING FAILURES:")
        lines.extend(
            f"    {result.spec.label}: {result.detail}" for result in report.blocking_failures
        )
    if report.informational_failures:
        lines.append("\n  informational failures (do not block):")
        lines.extend(
            f"    {result.spec.label}: {result.detail}" for result in report.informational_failures
        )
    if report.skipped:
        lines.append("\n  skipped (a skipped gate protects nothing):")
        lines.extend(f"    {result.spec.label}: {result.detail}" for result in report.skipped)
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m synapse.cli.check_gates",
        description="Evaluate release quality gates against a checked-in baseline.",
    )
    parser.add_argument("--results", type=Path, required=True, help="Candidate results.json.")
    parser.add_argument(
        "--baseline", type=Path, default=None, help="Approved baseline results.json."
    )
    parser.add_argument("--config", type=Path, default=Path("configs/quality-gates.toml"))
    parser.add_argument(
        "--summary-out",
        type=Path,
        default=None,
        help="Write the Markdown summary here (e.g. $GITHUB_STEP_SUMMARY).",
    )
    parser.add_argument(
        "--json-out", type=Path, default=None, help="Write the machine-readable gate report here."
    )
    parser.add_argument(
        "--warn-only",
        action="store_true",
        help="Report failures but always exit 0. For bootstrapping a new baseline only.",
    )
    parser.add_argument("--log-level", default="WARNING")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    args = _build_parser().parse_args(argv)
    configure_logging(args.log_level)

    try:
        config = load_config(args.config)
        current = _load_results(args.results, "candidate")
        baseline = _load_results(args.baseline, "baseline") if args.baseline else None
    except SynapseArtifactError as exc:
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        return 2

    report = evaluate(config, current, baseline)

    if args.summary_out:
        # Appended, not overwritten: GITHUB_STEP_SUMMARY accumulates across steps.
        summary = render_summary(report)
        with args.summary_out.open("a", encoding="utf-8") as handle:
            handle.write(summary)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    print(render_console(report))

    if args.warn_only:
        # Bootstrapping escape hatch. Announced loudly so a permanently
        # warn-only pipeline is visible in the log rather than forgotten.
        print(
            "\n  --warn-only is set: exiting 0 regardless of failures. This must not be the steady state."
        )
        return 0
    return 0 if report.passed else 1


if __name__ == "__main__":  # Enables `python -m synapse.cli.check_gates`
    raise SystemExit(main())

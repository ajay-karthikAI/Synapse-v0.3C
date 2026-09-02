"""
Tests for the release quality gates.

The assertions that matter most are the ones proving a gate cannot be silently
neutralised: a skipped gate is not a pass, an informational gate cannot fail a
build, and an incomparable baseline fails closed rather than producing a false
green.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from synapse.cli.check_gates import NON_VALIDATION_NOTICE, render_summary
from synapse.evals.gates import (
    Direction,
    GateMode,
    GateOutcome,
    GateSpec,
    evaluate,
    evaluate_gate,
    load_config,
)
from synapse.schemas.enums import RunMode
from synapse.schemas.evalrun import EvalRunResults, MetricFamily, MetricValue

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
CONFIG_PATH = Path(__file__).parent.parent / "configs" / "quality-gates.toml"


def a_metric(
    name: str, value: float | None, *, denominator: int = 20, unit: str = "ratio"
) -> MetricValue:
    """A defined metric with enough observations to be gateable."""
    return MetricValue(
        name=name,
        family=MetricFamily.DETERMINISTIC,
        value=value,
        denominator=denominator,
        n_cases=denominator,
        unit=unit,
        undefined_reason=None if value is not None else "no case produced a value",
    )


def a_run(
    metrics: dict[str, MetricValue],
    *,
    run_id: str = "r",
    by_category: dict | None = None,
    **overrides,
) -> EvalRunResults:
    """A minimal results record carrying the given metrics."""
    base: dict = {
        "run_id": run_id,
        "started_at": NOW,
        "completed_at": NOW,
        "synapse_version": "0.1.0",
        "mode": RunMode.OFFLINE_DETERMINISTIC,
        "seed": 0,
        "dataset_id": "d",
        "dataset_version": "0.1.0",
        "corpus_version": "c1",
        "cases_total": 20,
        "cases_evaluated": 20,
        "cases_skipped": 0,
        "cases_errored": 0,
        "cases_gating_eligible": 0,
        "deterministic_metrics": metrics,
        "by_category": by_category or {},
    }
    return EvalRunResults(**{**base, **overrides})


class TestConfigLoading:
    """The shipped configuration must load and must be honest about its status."""

    def test_shipped_config_loads(self) -> None:
        config = load_config(CONFIG_PATH)
        assert config.specs

    def test_shipped_config_is_marked_provisional(self) -> None:
        # Nobody has approved these thresholds, and the file must say so.
        config = load_config(CONFIG_PATH)
        assert config.is_approved is False
        assert "PROVISIONAL" in config.provisional_banner()

    def test_status_approved_without_an_approver_is_still_unapproved(self) -> None:
        # The approver field is the evidence; the label alone is not.
        config = load_config(CONFIG_PATH)
        config.status = "approved"
        config.approved_by = ""
        assert config.is_approved is False

    def test_safety_gates_tolerate_no_regression(self) -> None:
        config = load_config(CONFIG_PATH)
        by_name = {spec.metric: spec for spec in config.specs if spec.category is None}
        assert by_name["emergency_sensitivity"].max_regression == 0.0
        assert by_name["emergency_false_negative_count"].max_increase == 0

    def test_no_gate_asserts_an_unjustified_absolute_floor(self) -> None:
        # Requirement: do not invent impressive thresholds. Every ratio floor in
        # the shipped file is 0.0 — a real floor would be a quality claim nobody
        # has established.
        config = load_config(CONFIG_PATH)
        floors = [spec.min_absolute for spec in config.specs if spec.min_absolute is not None]
        assert floors and all(floor == 0.0 for floor in floors)

    def test_missing_config_raises(self) -> None:
        from synapse.errors import ArtifactSchemaError

        with pytest.raises(ArtifactSchemaError, match="not found"):
            load_config(Path("nope.toml"))


class TestSkipIsNotPass:
    """A gate that cannot decide must not report success."""

    SPEC = GateSpec(
        metric="recall_at_5",
        mode=GateMode.BLOCKING,
        direction=Direction.HIGHER_IS_BETTER,
        max_regression=0.05,
    )

    def test_absent_metric_skips(self) -> None:
        result = evaluate_gate(self.SPEC, a_run({}), None, min_denominator=5)
        assert result.outcome is GateOutcome.SKIP
        assert result.blocks is False

    def test_undefined_metric_skips_with_its_reason(self) -> None:
        run = a_run({"recall_at_5": a_metric("recall_at_5", None)})
        result = evaluate_gate(self.SPEC, run, None, min_denominator=5)
        assert result.outcome is GateOutcome.SKIP
        assert "no case produced a value" in result.detail

    def test_too_few_observations_skips(self) -> None:
        # A 2-case metric cannot support a pass/fail decision.
        run = a_run({"recall_at_5": a_metric("recall_at_5", 1.0, denominator=2)})
        result = evaluate_gate(self.SPEC, run, None, min_denominator=5)
        assert result.outcome is GateOutcome.SKIP
        assert "below the minimum" in result.detail

    def test_skips_are_reported_prominently(self) -> None:
        run = a_run({"recall_at_5": a_metric("recall_at_5", 1.0, denominator=2)})
        report = evaluate(load_config(CONFIG_PATH), run, None)
        assert "skipped" in render_summary(report).lower()


class TestRegressionDetection:
    """Direction, tolerance, and counts."""

    def _spec(self, **overrides) -> GateSpec:
        base = {
            "metric": "recall_at_5",
            "mode": GateMode.BLOCKING,
            "direction": Direction.HIGHER_IS_BETTER,
            "max_regression": 0.05,
        }
        return GateSpec(**{**base, **overrides})

    def test_a_drop_beyond_tolerance_fails(self) -> None:
        baseline = a_run({"recall_at_5": a_metric("recall_at_5", 0.90)})
        current = a_run({"recall_at_5": a_metric("recall_at_5", 0.80)})
        assert (
            evaluate_gate(self._spec(), current, baseline, min_denominator=5).outcome
            is GateOutcome.FAIL
        )

    def test_a_drop_within_tolerance_passes(self) -> None:
        baseline = a_run({"recall_at_5": a_metric("recall_at_5", 0.90)})
        current = a_run({"recall_at_5": a_metric("recall_at_5", 0.88)})
        assert (
            evaluate_gate(self._spec(), current, baseline, min_denominator=5).outcome
            is GateOutcome.PASS
        )

    def test_an_improvement_never_fails(self) -> None:
        baseline = a_run({"recall_at_5": a_metric("recall_at_5", 0.50)})
        current = a_run({"recall_at_5": a_metric("recall_at_5", 0.99)})
        assert (
            evaluate_gate(self._spec(), current, baseline, min_denominator=5).outcome
            is GateOutcome.PASS
        )

    def test_lower_is_better_inverts_the_direction(self) -> None:
        # A RISING unsupported-claim rate is the regression. Treating every
        # delta as higher-is-better would report a doubled hallucination rate
        # as an improvement.
        spec = self._spec(
            metric="unsupported_claim_rate",
            direction=Direction.LOWER_IS_BETTER,
            max_regression=0.02,
        )
        baseline = a_run({"unsupported_claim_rate": a_metric("unsupported_claim_rate", 0.05)})
        current = a_run({"unsupported_claim_rate": a_metric("unsupported_claim_rate", 0.20)})
        assert evaluate_gate(spec, current, baseline, min_denominator=5).outcome is GateOutcome.FAIL

    def test_a_falling_lower_is_better_metric_passes(self) -> None:
        spec = self._spec(
            metric="unsupported_claim_rate",
            direction=Direction.LOWER_IS_BETTER,
            max_regression=0.02,
        )
        baseline = a_run({"unsupported_claim_rate": a_metric("unsupported_claim_rate", 0.20)})
        current = a_run({"unsupported_claim_rate": a_metric("unsupported_claim_rate", 0.05)})
        assert evaluate_gate(spec, current, baseline, min_denominator=5).outcome is GateOutcome.PASS

    def test_any_increase_in_the_emergency_count_fails(self) -> None:
        # max_increase=0: a single additional missed emergency fails, however
        # small the dataset.
        spec = self._spec(
            metric="emergency_false_negative_count",
            direction=Direction.LOWER_IS_BETTER,
            max_increase=0,
            max_regression=None,
        )
        baseline = a_run(
            {
                "emergency_false_negative_count": a_metric(
                    "emergency_false_negative_count", 0.0, unit="count"
                )
            }
        )
        current = a_run(
            {
                "emergency_false_negative_count": a_metric(
                    "emergency_false_negative_count", 1.0, unit="count"
                )
            }
        )
        result = evaluate_gate(spec, current, baseline, min_denominator=5)
        assert result.outcome is GateOutcome.FAIL
        assert "increased by +1" in result.detail

    def test_an_absolute_ceiling_fails_without_a_baseline(self) -> None:
        spec = self._spec(
            metric="error_rate",
            direction=Direction.LOWER_IS_BETTER,
            max_absolute=0.10,
            max_regression=None,
        )
        current = a_run({"error_rate": a_metric("error_rate", 0.40)})
        assert evaluate_gate(spec, current, None, min_denominator=5).outcome is GateOutcome.FAIL

    def test_relative_tolerance_applies_to_latency(self) -> None:
        spec = self._spec(
            metric="latency_total_p95_ms",
            direction=Direction.LOWER_IS_BETTER,
            max_regression=None,
            max_regression_relative=0.25,
        )
        baseline = a_run(
            {"latency_total_p95_ms": a_metric("latency_total_p95_ms", 1000.0, unit="milliseconds")}
        )
        current = a_run(
            {"latency_total_p95_ms": a_metric("latency_total_p95_ms", 1400.0, unit="milliseconds")}
        )
        assert evaluate_gate(spec, current, baseline, min_denominator=5).outcome is GateOutcome.FAIL


class TestBlockingVersusInformational:
    """Informational gates never fail a build."""

    def test_an_informational_failure_does_not_block(self) -> None:
        spec = GateSpec(
            metric="mrr",
            mode=GateMode.INFORMATIONAL,
            direction=Direction.HIGHER_IS_BETTER,
            max_regression=0.01,
        )
        baseline = a_run({"mrr": a_metric("mrr", 0.90)})
        current = a_run({"mrr": a_metric("mrr", 0.10)})
        result = evaluate_gate(spec, current, baseline, min_denominator=5)
        assert result.outcome is GateOutcome.FAIL
        assert result.blocks is False

    def test_report_separates_the_two(self) -> None:
        config = load_config(CONFIG_PATH)
        baseline = a_run({"mrr": a_metric("mrr", 0.9), "recall_at_5": a_metric("recall_at_5", 0.9)})
        current = a_run({"mrr": a_metric("mrr", 0.1), "recall_at_5": a_metric("recall_at_5", 0.1)})
        report = evaluate(config, current, baseline)
        assert any(r.spec.metric == "recall_at_5" for r in report.blocking_failures)
        assert any(r.spec.metric == "mrr" for r in report.informational_failures)
        assert report.passed is False  # Blocked by recall, not by mrr


class TestBaselineComparability:
    """An incomparable baseline fails closed."""

    def test_a_different_corpus_version_is_incomparable(self) -> None:
        config = load_config(CONFIG_PATH)
        baseline = a_run({"recall_at_5": a_metric("recall_at_5", 0.9)}, corpus_version="c1")
        current = a_run({"recall_at_5": a_metric("recall_at_5", 0.9)}, corpus_version="c2")
        report = evaluate(config, current, baseline)
        assert report.baseline_comparable is False
        assert report.passed is False  # Fails closed even though no gate itself failed

    def test_a_different_seed_is_incomparable(self) -> None:
        config = load_config(CONFIG_PATH)
        baseline = a_run({"recall_at_5": a_metric("recall_at_5", 0.9)}, seed=0)
        current = a_run({"recall_at_5": a_metric("recall_at_5", 0.9)}, seed=7)
        assert evaluate(config, current, baseline).baseline_comparable is False

    def test_no_baseline_fails_closed_when_one_is_required(self) -> None:
        config = load_config(CONFIG_PATH)
        report = evaluate(config, a_run({"recall_at_5": a_metric("recall_at_5", 0.9)}), None)
        assert report.passed is False
        assert "no baseline supplied" in report.incomparable_reasons


class TestPerCategoryGates:
    """A targeted failure must not vanish into an aggregate."""

    def test_a_category_regression_is_caught(self) -> None:
        config = load_config(CONFIG_PATH)
        good = {
            "emergency_red_flag": {
                "behavior_correct": a_metric("behavior_correct", 1.0, denominator=10)
            }
        }
        bad = {
            "emergency_red_flag": {
                "behavior_correct": a_metric("behavior_correct", 0.8, denominator=10)
            }
        }
        baseline = a_run({}, by_category=good)
        current = a_run({}, by_category=bad)
        report = evaluate(config, current, baseline)
        assert any(r.spec.category == "emergency_red_flag" and r.blocks for r in report.results)

    def test_category_gates_are_labelled_with_their_category(self) -> None:
        spec = GateSpec(
            metric="behavior_correct",
            category="medication",
            mode=GateMode.BLOCKING,
            direction=Direction.HIGHER_IS_BETTER,
            max_regression=0.0,
        )
        assert spec.label == "medication/behavior_correct"


class TestSummaryRendering:
    """The job summary must lead with the caveats."""

    def _report(self):
        config = load_config(CONFIG_PATH)
        baseline = a_run({"recall_at_5": a_metric("recall_at_5", 0.9)})
        current = a_run({"recall_at_5": a_metric("recall_at_5", 0.5)})
        return evaluate(config, current, baseline)

    def test_summary_states_it_is_not_clinical_validation(self) -> None:
        assert NON_VALIDATION_NOTICE in render_summary(self._report())

    def test_summary_flags_the_provisional_config(self) -> None:
        assert "PROVISIONAL" in render_summary(self._report())

    def test_summary_warns_when_no_case_can_gate(self) -> None:
        # The single most important caveat on this repository's numbers.
        assert "No case in this run is release-gating" in render_summary(self._report())

    def test_caveats_appear_before_the_failure_table(self) -> None:
        summary = render_summary(self._report())
        assert summary.index("not clinical validation") < summary.index("blocking gate")

    def test_failure_report_names_the_metric_and_the_delta(self) -> None:
        # A gate suite that reports only "FAILED" trains people to re-run it
        # rather than read it.
        summary = render_summary(self._report())
        assert "recall_at_5" in summary
        assert "-0.4000" in summary


class TestCli:
    """Exit codes and artifact writing."""

    def _write(self, path: Path, results: EvalRunResults) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(results.model_dump(mode="json")), encoding="utf-8")
        return path

    def test_exit_zero_when_gates_pass(self, tmp_path: Path) -> None:
        from synapse.cli.check_gates import main

        metrics = {"recall_at_5": a_metric("recall_at_5", 0.9)}
        results = self._write(tmp_path / "r.json", a_run(metrics, run_id="cur"))
        baseline = self._write(tmp_path / "b.json", a_run(metrics, run_id="base"))
        assert (
            main(
                [
                    "--results",
                    str(results),
                    "--baseline",
                    str(baseline),
                    "--config",
                    str(CONFIG_PATH),
                ]
            )
            == 0
        )

    def test_exit_one_when_a_blocking_gate_fails(self, tmp_path: Path) -> None:
        from synapse.cli.check_gates import main

        results = self._write(
            tmp_path / "r.json", a_run({"recall_at_5": a_metric("recall_at_5", 0.1)}, run_id="cur")
        )
        baseline = self._write(
            tmp_path / "b.json", a_run({"recall_at_5": a_metric("recall_at_5", 0.9)}, run_id="base")
        )
        assert (
            main(
                [
                    "--results",
                    str(results),
                    "--baseline",
                    str(baseline),
                    "--config",
                    str(CONFIG_PATH),
                ]
            )
            == 1
        )

    def test_exit_two_on_a_missing_input(self, tmp_path: Path, capsys) -> None:
        from synapse.cli.check_gates import main

        assert main(["--results", str(tmp_path / "nope.json"), "--config", str(CONFIG_PATH)]) == 2
        assert "CONFIGURATION ERROR" in capsys.readouterr().err

    def test_warn_only_exits_zero_but_announces_itself(self, tmp_path: Path, capsys) -> None:
        from synapse.cli.check_gates import main

        results = self._write(
            tmp_path / "r.json", a_run({"recall_at_5": a_metric("recall_at_5", 0.1)}, run_id="cur")
        )
        baseline = self._write(
            tmp_path / "b.json", a_run({"recall_at_5": a_metric("recall_at_5", 0.9)}, run_id="base")
        )
        code = main(
            [
                "--results",
                str(results),
                "--baseline",
                str(baseline),
                "--config",
                str(CONFIG_PATH),
                "--warn-only",
            ]
        )
        assert code == 0
        assert "must not be the steady state" in capsys.readouterr().out

    def test_summary_is_appended_not_overwritten(self, tmp_path: Path) -> None:
        # GITHUB_STEP_SUMMARY accumulates across steps; overwriting it would
        # discard whatever an earlier step wrote.
        from synapse.cli.check_gates import main

        summary = tmp_path / "summary.md"
        summary.write_text("EARLIER STEP OUTPUT\n", encoding="utf-8")
        metrics = {"recall_at_5": a_metric("recall_at_5", 0.9)}
        results = self._write(tmp_path / "r.json", a_run(metrics, run_id="cur"))
        baseline = self._write(tmp_path / "b.json", a_run(metrics, run_id="base"))
        main(
            [
                "--results",
                str(results),
                "--baseline",
                str(baseline),
                "--config",
                str(CONFIG_PATH),
                "--summary-out",
                str(summary),
            ]
        )
        assert "EARLIER STEP OUTPUT" in summary.read_text()

    def test_json_report_is_written(self, tmp_path: Path) -> None:
        from synapse.cli.check_gates import main

        metrics = {"recall_at_5": a_metric("recall_at_5", 0.9)}
        results = self._write(tmp_path / "r.json", a_run(metrics, run_id="cur"))
        baseline = self._write(tmp_path / "b.json", a_run(metrics, run_id="base"))
        out = tmp_path / "gate_report.json"
        main(
            [
                "--results",
                str(results),
                "--baseline",
                str(baseline),
                "--config",
                str(CONFIG_PATH),
                "--json-out",
                str(out),
            ]
        )
        payload = json.loads(out.read_text())
        assert payload["gate_config_approved"] is False


class TestShippedCiFixtures:
    """The committed CI dataset and baseline must stay valid and non-gating."""

    ROOT = Path(__file__).parent.parent

    @pytest.mark.skipif(
        not (ROOT / "evals" / "ci" / "cases.jsonl").is_file(), reason="CI fixture absent"
    )
    def test_ci_dataset_has_no_gating_eligible_case(self) -> None:
        # Every CI case is synthetic. If this ever changes, the "not clinical
        # validation" caveat in every report would become a lie.
        from synapse.evalset.dataset import EvalDataset
        from synapse.evalset.gating import evaluate_dataset

        dataset = EvalDataset.load(self.ROOT / "evals" / "ci")
        report = evaluate_dataset(dataset.cases, dataset_version=dataset.manifest.version)
        assert report.eligible == []

    @pytest.mark.skipif(
        not (ROOT / "evals" / "baselines" / "approved" / "results.json").is_file(),
        reason="baseline absent",
    )
    def test_baseline_round_trips_through_the_schema(self) -> None:
        payload = json.loads(
            (self.ROOT / "evals" / "baselines" / "approved" / "results.json").read_text()
        )
        assert EvalRunResults.model_validate(payload).cases_gating_eligible == 0

    @pytest.mark.skipif(
        not (ROOT / "evals" / "baselines" / "approved" / "results.json").is_file(),
        reason="baseline absent",
    )
    def test_baseline_passes_its_own_gates(self) -> None:
        # A baseline that fails its own gate configuration is not a baseline.
        baseline = EvalRunResults.model_validate(
            json.loads(
                (self.ROOT / "evals" / "baselines" / "approved" / "results.json").read_text()
            )
        )
        report = evaluate(load_config(CONFIG_PATH), baseline, baseline)
        assert report.passed is True
        assert report.skipped == []  # Every gate must actually enforce, or the suite is decorative

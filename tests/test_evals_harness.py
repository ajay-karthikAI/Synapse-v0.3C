"""
Integration tests for the evaluation harness, against a small local fixture.

Everything here runs offline: the system under test is a
:class:`FixtureSystem` replaying recorded responses, and no test opens a
socket. ``test_run_makes_no_network_calls`` enforces that structurally by
monkeypatching ``socket.socket`` to raise during a full run.

The groups that matter most:

* ``TestEmptyGroundTruthIsSkipped`` — requirement 4, the defect that made every
  legacy metric zero.
* ``TestErrorsAreNeverSwallowed`` — requirement 6.
* ``TestGatingExcludesUnreviewed`` — requirement 11.
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from synapse.evals.compare import compare_runs, render_comparison
from synapse.evals.harness import HarnessConfig, run_evaluation
from synapse.evals.report import write_all
from synapse.evals.system import FixtureSystem, SystemResponse, record_fixture
from synapse.evalset.dataset import EvalDataset, new_manifest
from synapse.schemas.enums import (
    AnnotationStatus,
    DisagreementStatus,
    EvalCategory,
    EvalExpectedBehavior,
    PrivacyClass,
    RedactionStatus,
    ReviewerRole,
)
from synapse.schemas.evalrun import EvalRunResults, MetricFamily
from synapse.schemas.evalset import CaseReview, EvalCase, GradedRelevance

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def a_review(reviewer: str) -> CaseReview:
    """A complete review with a placeholder pseudonym."""
    return CaseReview(
        reviewer_id=reviewer,
        reviewer_role=ReviewerRole.CLINICIAN,
        reviewed_at=NOW,
        protocol_version="protocol-1",
        category=EvalCategory.ORDINARY_EDUCATION,
        expected_behavior=EvalExpectedBehavior.ANSWER,
    )


def a_case(
    case_id: str,
    query: str,
    *,
    category: EvalCategory = EvalCategory.ORDINARY_EDUCATION,
    behavior: EvalExpectedBehavior = EvalExpectedBehavior.ANSWER,
    docs: tuple[tuple[str, int], ...] = (("pubmed:1", 3),),
    reviewed: bool = False,
    **overrides,
) -> EvalCase:
    """A case with declared provenance; reviewed=True makes it gating-eligible."""
    base: dict = {
        "case_id": case_id,
        "dataset_version": "0.1.0",
        "corpus_version": "corpus-v1",
        "query": query,
        "category": category,
        "expected_behavior": behavior,
        "relevant_documents": [GradedRelevance(target_id=d, grade=g) for d, g in docs],
        "created_at": NOW,
        "privacy_class": PrivacyClass.SYNTHETIC,
        "redaction_status": RedactionStatus.NOT_REQUIRED,
        "known_limitations": "Test fixture.",
    }
    if reviewed:
        base.update(
            {
                "annotation_status": AnnotationStatus.REVIEWED,
                "reviews": [a_review("rev_00000000"), a_review("rev_11111111")],
                "disagreement_status": DisagreementStatus.AGREED,
            }
        )
    return EvalCase(**{**base, **overrides})


def a_response(**overrides) -> SystemResponse:
    """A plausible system response."""
    base: dict = {
        "behavior": EvalExpectedBehavior.ANSWER,
        "answer_text": "Your test measures average blood sugar over about three months.",
        "retrieved_document_ids": ["pubmed:1", "pubmed:9"],
        "retrieved_chunk_ids": ["pubmed:1#0000"],
        "latency_ms": {"retrieval": 40.0, "generation": 900.0, "total": 950.0},
        "prompt_tokens": 800,
        "completion_tokens": 200,
        "model": "gpt-4o-mini",
    }
    return SystemResponse(**{**base, **overrides})


@pytest.fixture
def config() -> HarnessConfig:
    """Offline config pinned to the fixture corpus version."""
    return HarnessConfig(
        k_values=(3, 5),
        seed=0,
        offline=True,
        corpus_version="corpus-v1",
        chunk_texts={"pubmed:1#0000": "HbA1c reflects average plasma glucose over 2-3 months."},
    )


def run(cases: list[EvalCase], responses: dict[str, SystemResponse], config: HarnessConfig):
    """Drive the harness with an in-memory fixture system."""
    system = FixtureSystem(
        responses={
            c.normalized_query: responses[c.case_id] for c in cases if c.case_id in responses
        }
    )
    return run_evaluation(
        cases,
        system,
        config,
        run_id="test-run",
        dataset_id="test-set",
        dataset_version="0.1.0",
        corpus_version="corpus-v1",
        now=NOW,
    )


class TestEmptyGroundTruthIsSkipped:
    """Requirement 4: never evaluate against empty ground truth."""

    def test_answer_case_without_labels_is_skipped_not_scored_zero(
        self, config: HarnessConfig
    ) -> None:
        # THE legacy defect: the app called the evaluator on every live query
        # with an empty relevance list, so recall was always 0.
        case = a_case(
            "c-0001",
            "a query with no labels",
            docs=(),
            expected_behavior=EvalExpectedBehavior.ABSTAIN,
        )
        answerable = a_case("c-0002", "a query with labels")
        output = run([case, answerable], {"c-0001": a_response(), "c-0002": a_response()}, config)
        # The unlabelled case is abstain-expected, so it IS evaluable; the
        # skip applies to answer-expected cases with nothing graded.
        assert output.results.cases_evaluated == 2

    def test_answer_expected_with_no_documents_cannot_be_constructed(self) -> None:
        # Defence in depth: the schema refuses it before the harness sees it.
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="grade at least one document"):
            a_case("c-0001", "query", docs=())

    def test_corpus_version_mismatch_is_skipped_and_named(self, config: HarnessConfig) -> None:
        case = a_case("c-0001", "a query", corpus_version="corpus-v2")
        output = run([case], {"c-0001": a_response()}, config)
        assert output.skipped == {"c-0001": "corpus_version_mismatch"}
        assert output.results.cases_evaluated == 0

    def test_skipped_cases_are_named_in_the_provenance_notes(self, config: HarnessConfig) -> None:
        case = a_case("c-0001", "a query", excluded=True, exclusion_reason="failed redaction")
        output = run([case], {"c-0001": a_response()}, config)
        assert any("skipped" in note for note in output.results.provenance_notes)


class TestErrorsAreNeverSwallowed:
    """Requirement 6."""

    def test_errored_case_is_recorded_with_a_kind(self, config: HarnessConfig) -> None:
        case = a_case("c-0001", "a query")
        response = a_response(errored=True, error_kind="timeout", timed_out=True)
        output = run([case], {"c-0001": response}, config)
        assert output.results.cases_errored == 1
        assert output.results.error_summary == {"timeout": 1}

    def test_errored_cases_are_excluded_from_metric_denominators(
        self, config: HarnessConfig
    ) -> None:
        # A run of two where one crashed must not report a clean average over
        # the survivor without the crash count beside it.
        cases = [a_case("c-0001", "first query"), a_case("c-0002", "second distinct query")]
        responses = {"c-0001": a_response(), "c-0002": a_response(errored=True, error_kind="boom")}
        output = run(cases, responses, config)
        assert output.results.deterministic_metrics["recall_at_5"].denominator == 1
        assert output.results.cases_errored == 1

    def test_error_rate_is_reported(self, config: HarnessConfig) -> None:
        cases = [a_case("c-0001", "first query"), a_case("c-0002", "second distinct query")]
        responses = {"c-0001": a_response(), "c-0002": a_response(errored=True, error_kind="boom")}
        output = run(cases, responses, config)
        assert output.results.deterministic_metrics["error_rate"].value == 0.5

    def test_fixture_miss_raises_rather_than_scoring_zero(self, config: HarnessConfig) -> None:
        from synapse.evals.system import SystemError_

        case = a_case("c-0001", "a query nobody recorded")
        system = FixtureSystem(responses={})
        with pytest.raises(SystemError_, match="no recorded response"):
            system.run(case)


class TestGatingExcludesUnreviewed:
    """Requirement 11."""

    def test_unreviewed_cases_are_not_gating_eligible(self, config: HarnessConfig) -> None:
        output = run([a_case("c-0001", "a query")], {"c-0001": a_response()}, config)
        assert output.results.cases_gating_eligible == 0

    def test_reviewed_cases_are_gating_eligible(self, config: HarnessConfig) -> None:
        output = run([a_case("c-0001", "a query", reviewed=True)], {"c-0001": a_response()}, config)
        assert output.results.cases_gating_eligible == 1

    def test_a_run_with_no_gating_cases_says_so_first(self, config: HarnessConfig) -> None:
        output = run([a_case("c-0001", "a query")], {"c-0001": a_response()}, config)
        assert "NO CASE IN THIS RUN IS RELEASE-GATING" in output.results.provenance_notes[0]

    def test_gating_aggregate_uses_only_eligible_cases(self, config: HarnessConfig) -> None:
        # A reviewed case that retrieves nothing, plus an unreviewed one that
        # retrieves perfectly. The gating aggregate must reflect only the former.
        cases = [
            a_case("c-0001", "reviewed query about glucose", reviewed=True),
            a_case("c-0002", "unreviewed query concerning kidneys"),
        ]
        responses = {
            "c-0001": a_response(retrieved_document_ids=["pubmed:99"]),  # miss
            "c-0002": a_response(retrieved_document_ids=["pubmed:1"]),  # hit
        }
        output = run(cases, responses, config)
        assert output.results.cases_gating_eligible == 1
        assert (
            output.results.deterministic_metrics["recall_at_5"].value == 0.0
        )  # Only the reviewed miss counts


class TestDeterministicAndJudgeStaySeparate:
    """Requirement 1 and 3."""

    def test_every_metric_is_filed_as_deterministic(self, config: HarnessConfig) -> None:
        output = run([a_case("c-0001", "a query")], {"c-0001": a_response()}, config)
        assert all(
            m.family is MetricFamily.DETERMINISTIC
            for m in output.results.deterministic_metrics.values()
        )

    def test_no_judge_ran_offline(self, config: HarnessConfig) -> None:
        output = run([a_case("c-0001", "a query")], {"c-0001": a_response()}, config)
        assert output.results.judge_metrics == {}

    def test_schema_refuses_a_deterministic_metric_under_judge_metrics(
        self, config: HarnessConfig
    ) -> None:
        # Structural guard: the separation cannot be broken by a caller mistake.
        from pydantic import ValidationError

        output = run([a_case("c-0001", "a query")], {"c-0001": a_response()}, config)
        payload = output.results.model_dump()
        payload["judge_metrics"] = {"recall_at_5": payload["deterministic_metrics"]["recall_at_5"]}
        with pytest.raises(ValidationError, match="must carry family 'judge'"):
            EvalRunResults.model_validate(payload)

    def test_judges_cannot_run_offline(self) -> None:
        from synapse.evals.judges import (
            GroundednessJudge,
            JudgeCache,
            JudgeRequest,
            JudgeUnavailableError,
        )

        judge = GroundednessJudge(client=None)
        request = JudgeRequest(item_id="c1", claim_text="a claim", evidence_text="some evidence")
        with pytest.raises(JudgeUnavailableError, match="offline"):
            judge.judge(request, JudgeCache(path=Path("/nonexistent")), offline=True)


class TestJudgeCaching:
    """Requirement 3: cache by input hash, validate structured output."""

    class FakeClient:
        """Records calls so cache behaviour can be asserted."""

        def __init__(self, response: str) -> None:
            self.response = response
            self.calls = 0

        def complete(self, prompt: str, *, temperature: float, seed: int) -> str:
            self.calls += 1
            return self.response

    def test_second_judgement_is_served_from_cache(self, tmp_path: Path) -> None:
        from synapse.evals.judges import GroundednessJudge, JudgeCache, JudgeRequest

        client = self.FakeClient(
            '{"supported": true, "score": 0.9, "rationale": "stated directly"}'
        )
        judge = GroundednessJudge(client=client, provider="fake", model="fake-1")
        cache = JudgeCache(path=tmp_path / "cache.json")
        request = JudgeRequest(item_id="c1", claim_text="a claim", evidence_text="evidence")

        judge.judge(request, cache, offline=False)
        judge.judge(request, cache, offline=False)
        assert client.calls == 1  # Second call served from cache
        assert cache.hits == 1

    def test_malformed_judge_output_is_rejected(self, tmp_path: Path) -> None:
        # A malformed judgement is not a judgement; guessing at what the model
        # meant would fabricate evidence.
        from synapse.evals.judges import (
            GroundednessJudge,
            JudgeCache,
            JudgeRequest,
            JudgeUnavailableError,
        )

        judge = GroundednessJudge(
            client=self.FakeClient("I think it is supported."), provider="fake", model="fake-1"
        )
        with pytest.raises(JudgeUnavailableError, match="not valid JSON"):
            judge.judge(
                JudgeRequest("c1", "claim", "evidence"),
                JudgeCache(path=tmp_path / "c.json"),
                offline=False,
            )

    def test_out_of_range_score_is_rejected(self, tmp_path: Path) -> None:
        from synapse.evals.judges import (
            GroundednessJudge,
            JudgeCache,
            JudgeRequest,
            JudgeUnavailableError,
        )

        judge = GroundednessJudge(
            client=self.FakeClient('{"supported": true, "score": 5, "rationale": "x"}'),
            provider="f",
            model="m",
        )
        with pytest.raises(JudgeUnavailableError, match="did not match the required schema"):
            judge.judge(
                JudgeRequest("c1", "claim", "evidence"),
                JudgeCache(path=tmp_path / "c.json"),
                offline=False,
            )

    def test_cache_key_changes_with_the_prompt(self, tmp_path: Path) -> None:
        # Changing the prompt must invalidate cached scores, not silently reuse
        # judgements produced under different instructions.
        from synapse.evals.judges import GroundednessJudge, JudgeCache, JudgeRequest

        cache = JudgeCache(path=tmp_path / "c.json")
        request = JudgeRequest("c1", "claim", "evidence")
        response = '{"supported": true, "score": 0.9, "rationale": "x"}'
        first = GroundednessJudge(client=self.FakeClient(response), provider="f", model="m")
        second = GroundednessJudge(
            client=self.FakeClient(response),
            provider="f",
            model="m",
            prompt_template="DIFFERENT {evidence} {claim}",
        )
        first.judge(request, cache, offline=False)
        assert (
            cache.get(
                cache.key(
                    provider="f",
                    model="m",
                    prompt_sha256=second.prompt_sha256,
                    input_digest=request.digest(),
                )
            )
            is None
        )

    def test_ties_resolve_to_unsupported(self) -> None:
        # In a patient-facing medical context the conservative reading is right.
        from synapse.evals.judges import JudgeVerdict, aggregate_verdicts

        verdicts = [
            JudgeVerdict(supported=True, score=0.6, rationale="a"),
            JudgeVerdict(supported=False, score=0.6, rationale="b"),
        ]
        assert aggregate_verdicts(verdicts) is False


class TestDeterminismAndOffline:
    """Requirement 2 and 12."""

    def test_run_makes_no_network_calls(self, config: HarnessConfig, monkeypatch) -> None:
        def forbidden(*args, **kwargs):
            raise AssertionError("evaluation attempted a network connection")

        monkeypatch.setattr(socket, "socket", forbidden)
        output = run([a_case("c-0001", "a query")], {"c-0001": a_response()}, config)
        assert output.results.cases_evaluated == 1

    def test_two_runs_produce_identical_metrics(self, config: HarnessConfig) -> None:
        cases = [a_case(f"c-{n:04d}", f"distinct query number {n}") for n in range(12)]
        responses = {c.case_id: a_response() for c in cases}
        first = run(cases, responses, config).results.model_dump(mode="json")
        second = run(cases, responses, config).results.model_dump(mode="json")
        assert first == second  # Includes the seeded bootstrap bounds

    def test_seed_changes_interval_bounds_but_not_values(self, config: HarnessConfig) -> None:
        cases = [a_case(f"c-{n:04d}", f"distinct query number {n}") for n in range(12)]
        responses = {
            c.case_id: a_response(retrieved_document_ids=["pubmed:1"] if n % 2 else ["pubmed:9"])
            for n, c in enumerate(cases)
        }
        seeded = run(
            cases, responses, HarnessConfig(k_values=(5,), seed=1, corpus_version="corpus-v1")
        )
        other = run(
            cases, responses, HarnessConfig(k_values=(5,), seed=2, corpus_version="corpus-v1")
        )
        assert (
            seeded.results.deterministic_metrics["recall_at_5"].value
            == other.results.deterministic_metrics["recall_at_5"].value
        )


class TestArtifacts:
    """Requirement 8: four files, every time."""

    def test_all_four_artifacts_are_written(self, tmp_path: Path, config: HarnessConfig) -> None:
        output = run([a_case("c-0001", "a query")], {"c-0001": a_response()}, config)
        paths = write_all(tmp_path / "run", output.results, output.outcomes)
        for path in (paths.results, paths.summary, paths.report, paths.per_case):
            assert path.is_file()

    def test_results_json_round_trips_through_the_schema(
        self, tmp_path: Path, config: HarnessConfig
    ) -> None:
        output = run([a_case("c-0001", "a query")], {"c-0001": a_response()}, config)
        paths = write_all(tmp_path / "run", output.results, output.outcomes)
        reloaded = EvalRunResults.model_validate(json.loads(paths.results.read_text()))
        assert reloaded.run_id == output.results.run_id

    def test_summary_flags_gating_capability_for_ci(
        self, tmp_path: Path, config: HarnessConfig
    ) -> None:
        output = run([a_case("c-0001", "a query")], {"c-0001": a_response()}, config)
        paths = write_all(tmp_path / "run", output.results, output.outcomes)
        summary = json.loads(paths.summary.read_text())
        assert summary["release_gating_capable"] is False

    def test_report_puts_caveats_before_numbers(
        self, tmp_path: Path, config: HarnessConfig
    ) -> None:
        # A reader who stops after the first table must already know what these
        # numbers are not.
        output = run([a_case("c-0001", "a query")], {"c-0001": a_response()}, config)
        paths = write_all(tmp_path / "run", output.results, output.outcomes)
        text = paths.report.read_text()
        assert text.index("Before reading the numbers") < text.index("Deterministic metrics")

    def test_per_case_has_one_line_per_case(self, tmp_path: Path, config: HarnessConfig) -> None:
        cases = [a_case("c-0001", "first query"), a_case("c-0002", "second distinct query")]
        output = run(cases, {c.case_id: a_response() for c in cases}, config)
        paths = write_all(tmp_path / "run", output.results, output.outcomes)
        assert len(paths.per_case.read_text().strip().splitlines()) == 2

    def test_metric_rendering_always_shows_the_denominator(
        self, tmp_path: Path, config: HarnessConfig
    ) -> None:
        # "100%" over one case must not read like "100%" over two hundred.
        output = run([a_case("c-0001", "a query")], {"c-0001": a_response()}, config)
        paths = write_all(tmp_path / "run", output.results, output.outcomes)
        assert "n=1" in paths.report.read_text()


class TestBaselineComparison:
    """Requirements 9 and 10."""

    def _two_runs(self, config: HarnessConfig, second_docs: list[str]):
        cases = [a_case("c-0001", "a query", reviewed=True)]
        baseline = run(cases, {"c-0001": a_response()}, config).results
        current = run(
            cases, {"c-0001": a_response(retrieved_document_ids=second_docs)}, config
        ).results
        return baseline, current

    def test_a_retrieval_drop_is_reported_as_a_regression(self, config: HarnessConfig) -> None:
        baseline, current = self._two_runs(config, ["pubmed:99"])
        report = compare_runs(baseline, current)
        assert any(d.name == "recall_at_5" and d.is_regression for d in report.deltas)

    def test_an_improvement_is_not_a_regression(self, config: HarnessConfig) -> None:
        cases = [a_case("c-0001", "a query", reviewed=True)]
        baseline = run(
            cases, {"c-0001": a_response(retrieved_document_ids=["pubmed:99"])}, config
        ).results
        current = run(cases, {"c-0001": a_response()}, config).results
        report = compare_runs(baseline, current)
        assert not any(d.name == "recall_at_5" and d.is_regression for d in report.deltas)

    def test_lower_is_better_metrics_invert_correctly(self, config: HarnessConfig) -> None:
        # A rising error rate must be a regression, not an improvement.
        cases = [a_case("c-0001", "a query", reviewed=True)]
        baseline = run(cases, {"c-0001": a_response()}, config).results
        current = run(
            cases, {"c-0001": a_response(errored=True, error_kind="boom")}, config
        ).results
        report = compare_runs(baseline, current)
        assert any(d.name == "error_rate" and d.is_regression for d in report.deltas)

    def test_corpus_version_change_makes_runs_incomparable(self, config: HarnessConfig) -> None:
        baseline, current = self._two_runs(config, ["pubmed:1"])
        drifted = current.model_copy(update={"corpus_version": "corpus-v2"})
        report = compare_runs(baseline, drifted)
        assert report.comparable is False
        assert any("corpus version" in reason for reason in report.incomparable_reasons)

    def test_incomparable_runs_say_so_loudly(self, config: HarnessConfig) -> None:
        baseline, current = self._two_runs(config, ["pubmed:1"])
        drifted = current.model_copy(update={"corpus_version": "corpus-v2"})
        rendered = render_comparison(compare_runs(baseline, drifted), baseline.run_id)
        assert "NOT COMPARABLE" in rendered

    def test_safety_metrics_regress_on_any_worsening(self, config: HarnessConfig) -> None:
        # A one-case drop in emergency sensitivity is not a rounding error, so
        # tolerance does not apply to it.
        escalate = EvalExpectedBehavior.EMERGENCY_ESCALATION
        cases = [
            a_case(
                f"c-{n:04d}",
                f"emergency query number {n}",
                category=EvalCategory.EMERGENCY_RED_FLAG,
                behavior=escalate,
                docs=(),
                reviewed=True,
            )
            for n in range(20)
        ]
        good = {c.case_id: a_response(behavior=escalate) for c in cases}
        bad = dict(good)
        bad["c-0000"] = a_response(behavior=EvalExpectedBehavior.ANSWER)  # One missed emergency
        baseline = run(cases, good, config).results
        current = run(cases, bad, config).results
        report = compare_runs(baseline, current)
        assert any(
            d.name == "emergency_sensitivity" and d.is_critical and d.is_regression
            for d in report.deltas
        )


class TestPerCategoryBreakdown:
    """A targeted failure must not vanish into an aggregate."""

    def test_categories_are_reported_separately(self, config: HarnessConfig) -> None:
        cases = [
            a_case(
                "c-0001", "an ordinary education query", category=EvalCategory.ORDINARY_EDUCATION
            ),
            a_case(
                "c-0002", "a medication query about metformin", category=EvalCategory.MEDICATION
            ),
        ]
        output = run(cases, {c.case_id: a_response() for c in cases}, config)
        assert set(output.results.by_category) == {"ordinary_education", "medication"}

    def test_a_category_failure_is_visible_in_the_breakdown(self, config: HarnessConfig) -> None:
        # Overall behaviour accuracy stays high while one category fails
        # entirely — exactly the case an aggregate hides.
        cases = [a_case(f"c-{n:04d}", f"ordinary query {n}") for n in range(9)]
        cases.append(
            a_case("c-0099", "a negated emergency query", category=EvalCategory.NEGATED_EMERGENCY)
        )
        responses = {c.case_id: a_response() for c in cases[:9]}
        responses["c-0099"] = a_response(
            behavior=EvalExpectedBehavior.EMERGENCY_ESCALATION
        )  # The known failure
        output = run(cases, responses, config)
        assert output.results.by_category["negated_emergency"]["behavior_correct"].value == 0.0
        assert output.results.by_category["ordinary_education"]["behavior_correct"].value == 1.0


class TestFixtureRoundTrip:
    """Fixtures are how a live run becomes reproducible."""

    def test_recorded_fixture_replays_identically(
        self, tmp_path: Path, config: HarnessConfig
    ) -> None:
        case = a_case("c-0001", "a query")
        path = tmp_path / "fixture.jsonl"
        record_fixture(path, [(case.query, a_response())])
        system = FixtureSystem.load(path)
        assert system.run(case).retrieved_document_ids == ["pubmed:1", "pubmed:9"]

    def test_fixture_is_keyed_by_normalised_query(self, tmp_path: Path) -> None:
        # So a fixture stays valid when a case is renumbered or its punctuation
        # is tidied.
        case = a_case("c-0001", "What does an HbA1c test measure?")
        path = tmp_path / "fixture.jsonl"
        record_fixture(path, [("what does an hba1c test measure", a_response())])
        assert FixtureSystem.load(path).run(case) is not None


class TestCli:
    """The command surface."""

    def _dataset(self, tmp_path: Path) -> Path:
        manifest = new_manifest(
            dataset_id="t",
            version="0.1.0",
            corpus_version="corpus-v1",
            protocol_version="p1",
            generator="test",
            now=NOW,
        )
        dataset = EvalDataset(
            manifest=manifest, cases=[a_case("c-0001", "a query")], directory=tmp_path / "ds"
        )
        dataset.write()
        return dataset.directory

    def test_offline_requires_a_fixture(self, tmp_path: Path, capsys) -> None:
        from synapse.evals.run import main

        assert (
            main(["--dataset", str(self._dataset(tmp_path)), "--output", str(tmp_path / "out")])
            == 2
        )
        assert "requires --fixture" in capsys.readouterr().err

    def test_judges_are_refused_offline(self, tmp_path: Path, capsys) -> None:
        from synapse.evals.run import main

        code = main(
            [
                "--dataset",
                str(self._dataset(tmp_path)),
                "--output",
                str(tmp_path / "out"),
                "--fixture",
                "x.jsonl",
                "--enable-judges",
            ]
        )
        assert code == 2
        assert "cannot run offline" in capsys.readouterr().err

    def test_full_offline_run_writes_artifacts(self, tmp_path: Path) -> None:
        from synapse.evals.run import main

        dataset_dir = self._dataset(tmp_path)
        fixture = tmp_path / "fixture.jsonl"
        record_fixture(fixture, [("a query", a_response())])
        output = tmp_path / "out"
        assert (
            main(
                ["--dataset", str(dataset_dir), "--output", str(output), "--fixture", str(fixture)]
            )
            == 0
        )
        for name in ("results.json", "summary.json", "report.md", "per_case.jsonl"):
            assert (output / name).is_file()

    def test_notice_is_always_printed(self, tmp_path: Path, capsys) -> None:
        from synapse.evals.run import main

        main(["--dataset", str(self._dataset(tmp_path)), "--output", str(tmp_path / "out")])
        assert "never clinical validation" in capsys.readouterr().err

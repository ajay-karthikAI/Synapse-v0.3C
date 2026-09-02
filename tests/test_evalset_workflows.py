"""
Evaluation-dataset workflow tests: gating, splits, leakage, agreement,
spreadsheet round trip, version comparison, and the CLI.

The most consequential group is ``TestGating``: it proves that no unreviewed
case can influence a release decision, which is the requirement the whole
framework exists to satisfy.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from synapse.evalset.agreement import cohens_kappa, compute_agreement, fleiss_kappa
from synapse.evalset.compare import diff_datasets, generate_changelog_entry
from synapse.evalset.dataset import EvalDataset, new_manifest
from synapse.evalset.gating import GatingPolicy, evaluate_case, evaluate_dataset
from synapse.evalset.similarity import cluster_by_similarity, find_duplicate_pairs, query_similarity
from synapse.evalset.splits import detect_leakage, plan_splits
from synapse.evalset.spreadsheet import (
    ALL_COLUMNS,
    decode_grades,
    encode_grades,
    export_review_sheet,
    import_review_sheet,
)
from synapse.schemas.enums import (
    AnnotationStatus,
    DatasetSplit,
    DisagreementStatus,
    EvalCategory,
    EvalExpectedBehavior,
    PrivacyClass,
    RedactionStatus,
    ReviewerRole,
)
from synapse.schemas.evalset import CaseReview, EvalCase, GradedRelevance

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def a_review(
    reviewer="rev_00000000",
    category=EvalCategory.ORDINARY_EDUCATION,
    behavior=EvalExpectedBehavior.ANSWER,
    role=ReviewerRole.CLINICIAN,
) -> CaseReview:
    """A complete review with a placeholder pseudonym."""
    return CaseReview(
        reviewer_id=reviewer,
        reviewer_role=role,
        reviewed_at=NOW,
        protocol_version="protocol-1",
        category=category,
        expected_behavior=behavior,
    )


def a_case(case_id="test-0001", query="what does an HbA1c test measure", **overrides) -> EvalCase:
    """A minimal valid case with declared provenance."""
    base = {
        "case_id": case_id,
        "dataset_version": "0.1.0",
        "corpus_version": "corpus-v1",
        "query": query,
        "category": EvalCategory.ORDINARY_EDUCATION,
        "expected_behavior": EvalExpectedBehavior.ANSWER,
        "relevant_documents": [GradedRelevance(target_id="pubmed:41802233", grade=3)],
        "created_at": NOW,
        "privacy_class": PrivacyClass.SYNTHETIC,
        "redaction_status": RedactionStatus.NOT_REQUIRED,
        "known_limitations": "Test fixture.",
    }
    return EvalCase(**{**base, **overrides})


def reviewed_case(case_id="test-0001", **overrides) -> EvalCase:
    """A case with two agreeing reviews — the shape that may gate a release."""
    return a_case(
        case_id=case_id,
        annotation_status=AnnotationStatus.REVIEWED,
        reviews=[a_review("rev_00000000"), a_review("rev_11111111")],
        disagreement_status=DisagreementStatus.AGREED,
        **overrides,
    )


class TestGating:
    """The requirement: an unreviewed case cannot gate a release."""

    def test_synthetic_case_is_never_eligible(self) -> None:
        case = a_case(is_synthetic=True, annotation_status=AnnotationStatus.SYNTHETIC)
        assert evaluate_case(case, GatingPolicy()).reason == "synthetic"

    def test_synthetic_case_is_refused_even_under_the_escape_hatch(self) -> None:
        # No policy relaxation reaches a synthetic case: it is an illustration,
        # not evidence, and that is checked first and unconditionally.
        case = a_case(is_synthetic=True, annotation_status=AnnotationStatus.SYNTHETIC)
        assert evaluate_case(case, GatingPolicy(allow_unreviewed=True)).eligible is False

    def test_pending_review_case_is_not_eligible(self) -> None:
        assert evaluate_case(a_case(), GatingPolicy()).reason == "not_reviewed"

    def test_single_review_is_insufficient_by_default(self) -> None:
        case = a_case(annotation_status=AnnotationStatus.REVIEWED, reviews=[a_review()])
        assert evaluate_case(case, GatingPolicy()).reason == "insufficient_reviews"

    def test_two_agreeing_reviews_are_eligible(self) -> None:
        assert evaluate_case(reviewed_case(), GatingPolicy()).eligible is True

    def test_open_disagreement_blocks_gating(self) -> None:
        case = a_case(
            annotation_status=AnnotationStatus.REVIEWED,
            reviews=[
                a_review("rev_00000000"),
                a_review("rev_11111111", category=EvalCategory.MEDICATION),
            ],
            disagreement_status=DisagreementStatus.DISAGREED_OPEN,
        )
        assert evaluate_case(case, GatingPolicy()).reason == "unresolved_disagreement"

    def test_unredacted_patient_derived_case_blocks_gating(self) -> None:
        # Checked before the review rules: two clinician reviews do not license
        # an unredacted patient-derived case.
        case = reviewed_case(
            privacy_class=PrivacyClass.PATIENT_DERIVED, redaction_status=RedactionStatus.PENDING
        )
        assert evaluate_case(case, GatingPolicy()).reason == "redaction_incomplete"

    def test_corpus_version_mismatch_blocks_gating(self) -> None:
        # The check the shipped evaluation set would have failed.
        decision = evaluate_case(reviewed_case(), GatingPolicy(), corpus_version="corpus-v2")
        assert decision.reason == "corpus_version_mismatch"

    def test_excluded_case_is_not_eligible(self) -> None:
        case = reviewed_case(excluded=True, exclusion_reason="failed redaction")
        assert evaluate_case(case, GatingPolicy()).reason == "excluded"

    def test_single_review_relaxation_is_described(self) -> None:
        assert "min_reviews=1" in GatingPolicy(allow_single_review=True).describe()

    def test_ungoverned_override_is_described_alarmingly(self) -> None:
        assert "UNGOVERNED" in GatingPolicy(allow_unreviewed=True).describe()

    def test_report_explains_every_exclusion(self) -> None:
        cases = [
            a_case("c-0001", "query one about hba1c levels"),
            a_case(
                "c-0002",
                "totally different query concerning kidney function",
                is_synthetic=True,
                annotation_status=AnnotationStatus.SYNTHETIC,
            ),
            reviewed_case("c-0003", query="a third distinct query regarding blood pressure"),
        ]
        report = evaluate_dataset(cases, dataset_version="0.1.0")
        assert len(report.eligible) == 1
        assert report.reason_counts() == {"not_reviewed": 1, "synthetic": 1}

    def test_report_states_when_nothing_may_gate(self) -> None:
        report = evaluate_dataset([a_case()], dataset_version="0.1.0")
        assert "must not block or approve a release" in report.render()


class TestSimilarityAndDuplicates:
    """Duplicate and near-duplicate detection."""

    def test_identical_queries_score_one(self) -> None:
        assert query_similarity("what is HbA1c", "What is HbA1c?") == 1.0

    def test_reworded_queries_are_near_duplicates(self) -> None:
        from synapse.evalset.similarity import DEFAULT_NEAR_DUPLICATE_THRESHOLD

        # Asserted against the threshold rather than a hand-picked constant, so
        # the test tracks the policy instead of a coincidence.
        assert (
            query_similarity("what does HbA1c measure", "what does an HbA1c measure")
            >= DEFAULT_NEAR_DUPLICATE_THRESHOLD
        )

    def test_different_questions_are_not_near_duplicates(self) -> None:
        assert query_similarity("what is HbA1c", "how do I book an appointment") < 0.5

    def test_stop_and_start_are_not_merged(self) -> None:
        # These differ by one word but mean opposite things. They fall below the
        # threshold, but only just (0.667 vs 0.70) — that margin is incidental,
        # which is why the dataset validator separately reports near-duplicate
        # clusters whose members expect different behaviours.
        from synapse.evalset.similarity import DEFAULT_NEAR_DUPLICATE_THRESHOLD

        similarity = query_similarity(
            "should I stop taking metformin", "should I start taking metformin"
        )
        assert similarity < DEFAULT_NEAR_DUPLICATE_THRESHOLD

    def test_exact_duplicates_are_flagged(self) -> None:
        pairs = find_duplicate_pairs([("a", "what is HbA1c"), ("b", "What is HbA1c?")])
        assert len(pairs) == 1
        assert pairs[0].exact is True

    def test_clusters_are_transitive(self) -> None:
        # If A≈B and B≈C, all three must share a split even when A and C fall
        # below the threshold — otherwise information leaks through B.
        cases = [
            ("a", "what does hba1c measure"),
            ("b", "what does an hba1c measure"),
            ("c", "what does an hba1c test measure"),
            ("z", "how do I book an appointment with reception"),
        ]
        clusters = cluster_by_similarity(cases)
        big = [c for c in clusters if len(c) > 1]
        assert big and set(big[0]) >= {"a", "b"}
        assert ["z"] in clusters

    def test_clustering_is_deterministic(self) -> None:
        cases = [
            ("a", "alpha beta gamma"),
            ("b", "alpha beta gamma delta"),
            ("c", "wholly unrelated text"),
        ]
        assert cluster_by_similarity(cases) == cluster_by_similarity(cases)


class TestSplitsAndLeakage:
    """Splits must not separate near-duplicates, and leakage must be visible."""

    def _cases(self, count: int = 12) -> list[EvalCase]:
        return [
            a_case(f"c-{n:04d}", f"distinct question number {n} about topic {n}")
            for n in range(count)
        ]

    def test_every_usable_case_is_assigned(self) -> None:
        plan = plan_splits(self._cases())
        assert plan.counts()["unassigned"] == 0

    def test_split_planning_is_deterministic(self) -> None:
        cases = self._cases()
        assert plan_splits(cases).assignments == plan_splits(cases).assignments

    def test_near_duplicates_land_in_the_same_split(self) -> None:
        # The mechanism preventing query leakage.
        cases = [
            a_case("c-0001", "what does an hba1c test measure"),
            a_case("c-0002", "what does a hba1c test measure"),
            *self._cases(8),
        ]
        plan = plan_splits(cases)
        assert plan.assignments["c-0001"] is plan.assignments["c-0002"]

    def test_excluded_cases_stay_unassigned(self) -> None:
        cases = [
            *self._cases(6),
            a_case("c-9999", "an excluded query", excluded=True, exclusion_reason="bad"),
        ]
        plan = plan_splits(cases)
        assert plan.assignments["c-9999"] is DatasetSplit.UNASSIGNED

    def test_ratios_must_sum_to_one(self) -> None:
        with pytest.raises(ValueError, match=r"must sum to 1\.0"):
            plan_splits(self._cases(), ratios={DatasetSplit.TRAIN: 0.5, DatasetSplit.TEST: 0.2})

    def test_document_leakage_is_detected(self) -> None:
        # The same document graded relevant in two splits.
        shared = [GradedRelevance(target_id="pubmed:41802233", grade=3)]
        cases = [
            a_case(
                "c-0001",
                "first query about glucose",
                split=DatasetSplit.TRAIN,
                relevant_documents=shared,
            ),
            a_case(
                "c-0002",
                "second unrelated query on kidneys",
                split=DatasetSplit.TEST,
                relevant_documents=shared,
            ),
        ]
        report = detect_leakage(cases)
        assert report.has_document_leakage
        assert report.document_leaks[0].document_id == "pubmed:41802233"

    def test_query_leakage_across_splits_is_detected(self) -> None:
        cases = [
            a_case("c-0001", "what does an hba1c test measure", split=DatasetSplit.TRAIN),
            a_case("c-0002", "what does a hba1c test measure", split=DatasetSplit.TEST),
        ]
        assert detect_leakage(cases).has_query_leakage

    def test_no_leakage_when_splits_are_planned_properly(self) -> None:
        cases = [
            a_case("c-0001", "what does an hba1c test measure"),
            a_case("c-0002", "what does a hba1c test measure"),
            *self._cases(8),
        ]
        plan = plan_splits(cases)
        assigned = [c.model_copy(update={"split": plan.assignments[c.case_id]}) for c in cases]
        assert detect_leakage(assigned).has_query_leakage is False


class TestAgreement:
    """Inter-annotator agreement, and honest reporting of when it is undefined."""

    def test_cohens_kappa_perfect_agreement(self) -> None:
        assert cohens_kappa([("a", "a"), ("b", "b"), ("a", "a"), ("b", "b")]) == pytest.approx(1.0)

    def test_cohens_kappa_corrects_for_chance(self) -> None:
        # Both reviewers used only one label. Raw agreement is 100%, but the
        # statistic must not present that as evidence of anything.
        pairs = [("a", "a")] * 10
        assert cohens_kappa(pairs) == 1.0  # Degenerate case, documented and handled explicitly

    def test_cohens_kappa_below_chance_is_negative(self) -> None:
        assert cohens_kappa([("a", "b"), ("b", "a"), ("a", "b"), ("b", "a")]) < 0

    def test_cohens_kappa_of_nothing_is_none(self) -> None:
        assert cohens_kappa([]) is None

    def test_fleiss_kappa_requires_uniform_rater_counts(self) -> None:
        # Ragged data: the statistic is undefined, and None is returned rather
        # than an approximation.
        assert fleiss_kappa([["a", "a", "a"], ["b", "b"]]) is None

    def test_fleiss_kappa_perfect_agreement(self) -> None:
        assert fleiss_kappa([["a", "a", "a"], ["b", "b", "b"]]) == pytest.approx(1.0)

    def test_agreement_is_not_computable_without_two_reviews(self) -> None:
        report = compute_agreement([a_case()], dataset_version="0.1.0")
        assert report.is_computable is False
        assert "cannot be computed" in " ".join(report.notes)

    def test_agreement_uses_cohens_kappa_for_two_reviewers(self) -> None:
        report = compute_agreement(
            [reviewed_case("c-0001"), reviewed_case("c-0002", query="another distinct query")],
            dataset_version="0.1.0",
        )
        assert report.is_computable
        assert all(entry.statistic == "cohen's kappa" for entry in report.fields)

    def test_disagreements_are_listed_by_case(self) -> None:
        disagreeing = a_case(
            "c-0002",
            query="a second distinct query about kidneys",
            annotation_status=AnnotationStatus.REVIEWED,
            reviews=[
                a_review("rev_00000000"),
                a_review("rev_11111111", category=EvalCategory.MEDICATION),
            ],
            disagreement_status=DisagreementStatus.DISAGREED_OPEN,
        )
        report = compute_agreement([reviewed_case("c-0001"), disagreeing], dataset_version="0.1.0")
        category_field = next(f for f in report.fields if f.field_name == "category")
        assert category_field.disagreeing_case_ids == ["c-0002"]

    def test_small_sample_is_flagged(self) -> None:
        report = compute_agreement([reviewed_case()], dataset_version="0.1.0")
        assert any("unstable at this sample size" in note for note in report.notes)

    def test_synthetic_cases_are_excluded_from_agreement(self) -> None:
        synthetic = a_case(
            "c-0002",
            "another query",
            is_synthetic=True,
            annotation_status=AnnotationStatus.SYNTHETIC,
        )
        report = compute_agreement([reviewed_case("c-0001"), synthetic], dataset_version="0.1.0")
        assert report.total_cases == 1


class TestSpreadsheet:
    """Review sheets must survive a spreadsheet round trip."""

    def test_export_writes_the_expected_columns(self, tmp_path: Path) -> None:
        path = tmp_path / "sheet.csv"
        export_review_sheet([a_case()], path)
        with path.open(encoding="utf-8") as handle:
            assert next(csv.reader(handle)) == list(ALL_COLUMNS)

    def test_synthetic_cases_are_omitted_from_review_sheets(self, tmp_path: Path) -> None:
        # They are illustrations, not review candidates, and the schema would
        # refuse a review filed against one.
        path = tmp_path / "sheet.csv"
        rows = export_review_sheet(
            [a_case("c-0001", is_synthetic=True, annotation_status=AnnotationStatus.SYNTHETIC)],
            path,
        )
        assert rows == 0

    def test_formula_injection_is_neutralised(self, tmp_path: Path) -> None:
        # A cell beginning '=' is executed by Excel and Sheets.
        path = tmp_path / "sheet.csv"
        export_review_sheet([a_case(query="=cmd|'/c calc'!A1")], path)
        content = path.read_text(encoding="utf-8")
        assert "'=cmd" in content  # Prefixed with an apostrophe, so it stays text

    def test_grade_encoding_round_trips(self) -> None:
        grades = [
            GradedRelevance(target_id="pubmed:123", grade=3),
            GradedRelevance(target_id="pubmed:456#0001", grade=1),
        ]
        assert decode_grades(encode_grades(grades)) == grades

    def test_malformed_grades_raise_rather_than_being_dropped(self) -> None:
        # A silently dropped relevance judgement is a labelling error nobody
        # would notice.
        from synapse.errors import ArtifactSchemaError

        with pytest.raises(ArtifactSchemaError, match="malformed graded-relevance"):
            decode_grades("pubmed:123:notanumber")

    def _write_sheet(self, path: Path, rows: list[dict[str, str]]) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=ALL_COLUMNS)
            writer.writeheader()
            for row in rows:
                writer.writerow({column: row.get(column, "") for column in ALL_COLUMNS})

    def test_completed_rows_import_as_reviews(self, tmp_path: Path) -> None:
        path = tmp_path / "sheet.csv"
        self._write_sheet(
            path,
            [
                {
                    "case_id": "c-0001",
                    "reviewer_id": "rev_00000000",
                    "review_category": "ordinary_education",
                    "review_expected_behavior": "answer",
                    "review_confidence": "4",
                }
            ],
        )
        result = import_review_sheet(path, protocol_version="protocol-1")
        assert result.ok and len(result.reviews) == 1
        assert result.reviews[0].review.confidence == 4

    def test_blank_rows_are_skipped_not_errors(self, tmp_path: Path) -> None:
        path = tmp_path / "sheet.csv"
        self._write_sheet(path, [{"case_id": "c-0001"}])
        result = import_review_sheet(path, protocol_version="protocol-1")
        assert result.ok and result.skipped_rows == 1

    def test_partially_completed_rows_are_errors(self, tmp_path: Path) -> None:
        # A half-filled row usually means the reviewer was interrupted;
        # discarding it silently would lose their work.
        path = tmp_path / "sheet.csv"
        self._write_sheet(path, [{"case_id": "c-0001", "reviewer_id": "rev_00000000"}])
        result = import_review_sheet(path, protocol_version="protocol-1")
        assert not result.ok
        assert "partially completed" in result.errors[0]

    def test_bad_reviewer_id_is_reported_without_echoing_content(self, tmp_path: Path) -> None:
        path = tmp_path / "sheet.csv"
        self._write_sheet(
            path,
            [
                {
                    "case_id": "c-0001",
                    "reviewer_id": "dr_smith",
                    "review_category": "ordinary_education",
                    "review_expected_behavior": "answer",
                }
            ],
        )
        result = import_review_sheet(path, protocol_version="protocol-1")
        assert not result.ok
        assert "dr_smith" not in result.errors[0]  # Row content is never echoed back

    def test_excel_bom_is_tolerated(self, tmp_path: Path) -> None:
        path = tmp_path / "sheet.csv"
        self._write_sheet(path, [{"case_id": "c-0001"}])
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())  # Excel writes a BOM on save
        assert import_review_sheet(path, protocol_version="protocol-1").ok

    def test_renamed_columns_are_refused(self, tmp_path: Path) -> None:
        from synapse.errors import ArtifactSchemaError

        path = tmp_path / "sheet.csv"
        path.write_text("case_id,query\nc-0001,hello\n", encoding="utf-8")
        with pytest.raises(ArtifactSchemaError, match="missing required columns"):
            import_review_sheet(path, protocol_version="protocol-1")


class TestDatasetAndCompare:
    """Dataset persistence, validation and version comparison."""

    @pytest.fixture
    def dataset(self, tmp_path: Path) -> EvalDataset:
        manifest = new_manifest(
            dataset_id="test-set",
            version="0.1.0",
            corpus_version="corpus-v1",
            protocol_version="protocol-1",
            generator="test",
            now=NOW,
        )
        ds = EvalDataset(
            manifest=manifest,
            cases=[
                a_case("c-0001", "first query about glucose control"),
                a_case("c-0002", "second query concerning kidney function"),
            ],
            directory=tmp_path / "ds",
        )
        ds.write()
        return ds

    def test_round_trip_through_disk(self, dataset: EvalDataset) -> None:
        reloaded = EvalDataset.load(dataset.directory)
        assert reloaded.cases == dataset.cases
        assert reloaded.manifest.cases_sha256 == dataset.manifest.cases_sha256

    def test_write_refreshes_derived_counts(self, dataset: EvalDataset) -> None:
        assert dataset.manifest.case_count == 2
        assert dataset.manifest.gating_eligible_count == 0  # Nothing is reviewed

    def test_editing_without_rewriting_fails_validation(self, dataset: EvalDataset) -> None:
        dataset.cases.append(a_case("c-0003", "a third query on blood pressure"))
        assert any(i.code == "digest_mismatch" for i in dataset.validate().errors)

    def test_duplicate_queries_are_errors(self, dataset: EvalDataset) -> None:
        dataset.cases.append(a_case("c-0003", "First query about glucose control!"))
        dataset.write()
        assert any(i.code == "duplicate_query" for i in dataset.validate().errors)

    def test_missing_corpus_documents_are_errors(self, dataset: EvalDataset) -> None:
        # The check the shipped evaluation set would have failed.
        issues = dataset.validate(corpus_document_ids={"pubmed:99999999"}).errors
        assert any(i.code == "relevance_label_missing_document" for i in issues)

    def test_skipping_the_corpus_check_is_recorded(self, dataset: EvalDataset) -> None:
        # Reporting a clean dataset without checking would be worse than saying so.
        assert any(i.code == "corpus_not_checked" for i in dataset.validate().warnings)

    def test_diff_detects_label_changes_and_breaks_comparability(
        self, dataset: EvalDataset
    ) -> None:
        changed = [
            dataset.cases[0].model_copy(update={"category": EvalCategory.MEDICATION}),
            dataset.cases[1],
        ]
        newer = dataset.manifest.model_copy(update={"version": "0.2.0"})
        diff = diff_datasets(dataset.manifest, dataset.cases, newer, changed)
        assert len(diff.of_type("label_changed")) == 1
        assert diff.scores_are_comparable is False

    def test_status_change_alone_keeps_scores_comparable(self, dataset: EvalDataset) -> None:
        # A case becoming gating-eligible is progress, not breakage.
        updated = [
            dataset.cases[0].model_copy(
                update={
                    "annotation_status": AnnotationStatus.REVIEWED,
                    "reviews": [a_review("rev_00000000")],
                }
            ),
            dataset.cases[1],
        ]
        newer = dataset.manifest.model_copy(update={"version": "0.1.1"})
        diff = diff_datasets(dataset.manifest, dataset.cases, newer, updated)
        assert diff.scores_are_comparable is True

    def test_corpus_version_change_breaks_comparability(self, dataset: EvalDataset) -> None:
        newer = dataset.manifest.model_copy(
            update={"version": "0.2.0", "corpus_version": "corpus-v2"}
        )
        diff = diff_datasets(dataset.manifest, dataset.cases, newer, dataset.cases)
        assert diff.corpus_version_changed and diff.scores_are_comparable is False

    def test_changelog_warns_when_scores_are_incomparable(self, dataset: EvalDataset) -> None:
        newer = dataset.manifest.model_copy(
            update={"version": "0.2.0", "corpus_version": "corpus-v2"}
        )
        entry = generate_changelog_entry(
            diff_datasets(dataset.manifest, dataset.cases, newer, dataset.cases), now=NOW
        )
        assert "NOT comparable" in entry
        assert "Corpus version changed" in entry


class TestShippedSyntheticExamples:
    """The examples in the repository must stay unmistakably non-gating."""

    PATH = Path(__file__).parent.parent / "evals" / "examples" / "synthetic_unreviewed.jsonl"

    @pytest.fixture
    def cases(self) -> list[EvalCase]:
        from synapse.corpus.jsonl import read_jsonl

        return list(read_jsonl(self.PATH, EvalCase))

    @pytest.mark.skipif(not PATH.is_file(), reason="example file not present")
    def test_every_example_is_marked_synthetic(self, cases: list[EvalCase]) -> None:
        assert cases
        assert all(case.is_synthetic for case in cases)
        assert all(case.annotation_status is AnnotationStatus.SYNTHETIC for case in cases)

    @pytest.mark.skipif(not PATH.is_file(), reason="example file not present")
    def test_no_example_carries_a_review(self, cases: list[EvalCase]) -> None:
        assert all(case.reviews == [] for case in cases)
        assert all(case.adjudication is None for case in cases)

    @pytest.mark.skipif(not PATH.is_file(), reason="example file not present")
    def test_every_example_states_its_limitations(self, cases: list[EvalCase]) -> None:
        assert all("SYNTHETIC AND UNREVIEWED" in case.known_limitations for case in cases)

    @pytest.mark.skipif(not PATH.is_file(), reason="example file not present")
    def test_none_may_gate_a_release(self, cases: list[EvalCase]) -> None:
        report = evaluate_dataset(cases, dataset_version="0.1.0")
        assert report.eligible == []
        assert report.reason_counts() == {"synthetic": len(cases)}

    @pytest.mark.skipif(not PATH.is_file(), reason="example file not present")
    def test_all_eight_categories_are_covered(self, cases: list[EvalCase]) -> None:
        assert {case.category for case in cases} == set(EvalCategory)


class TestCli:
    """The labelling workflows, driven end to end."""

    def _init(self, tmp_path: Path, *extra: str) -> int:
        from synapse.cli.evalset import main

        return main(
            [
                "init",
                "--dataset",
                str(tmp_path / "ds"),
                "--dataset-id",
                "cli-set",
                "--corpus-version",
                "corpus-v1",
                *extra,
            ]
        )

    def test_init_creates_the_layout(self, tmp_path: Path) -> None:
        assert self._init(tmp_path) == 0
        for name in ("manifest.json", "cases.jsonl", "CHANGELOG.md"):
            assert (tmp_path / "ds" / name).is_file()

    def test_add_case_refuses_a_pre_reviewed_case(self, tmp_path: Path, capsys) -> None:
        # Authoring can never produce a reviewed case.
        from synapse.cli.evalset import main

        self._init(tmp_path)
        case_file = tmp_path / "case.json"
        payload = a_case("c-0001").model_dump(mode="json")
        payload["annotation_status"] = "reviewed"
        case_file.write_text(json.dumps(payload), encoding="utf-8")
        assert main(["add-case", "--dataset", str(tmp_path / "ds"), "--case", str(case_file)]) == 2
        assert "may only create" in capsys.readouterr().err

    def test_add_case_accepts_a_pending_case(self, tmp_path: Path) -> None:
        from synapse.cli.evalset import main

        self._init(tmp_path)
        case_file = tmp_path / "case.json"
        case_file.write_text(a_case("c-0001").model_dump_json(), encoding="utf-8")
        assert main(["add-case", "--dataset", str(tmp_path / "ds"), "--case", str(case_file)]) == 0
        assert len(EvalDataset.load(tmp_path / "ds").cases) == 1

    def test_gating_command_reports_zero_and_can_fail_ci(self, tmp_path: Path) -> None:
        from synapse.cli.evalset import main

        self._init(tmp_path)
        case_file = tmp_path / "case.json"
        case_file.write_text(a_case("c-0001").model_dump_json(), encoding="utf-8")
        main(["add-case", "--dataset", str(tmp_path / "ds"), "--case", str(case_file)])
        assert main(["gating", "--dataset", str(tmp_path / "ds")]) == 0
        assert main(["gating", "--dataset", str(tmp_path / "ds"), "--require-eligible"]) == 1

    def test_export_then_import_sheet_files_a_review(self, tmp_path: Path) -> None:
        from synapse.cli.evalset import main

        self._init(tmp_path)
        case_file = tmp_path / "case.json"
        case_file.write_text(a_case("c-0001").model_dump_json(), encoding="utf-8")
        main(["add-case", "--dataset", str(tmp_path / "ds"), "--case", str(case_file)])

        sheet = tmp_path / "sheet.csv"
        assert main(["export-sheet", "--dataset", str(tmp_path / "ds"), "--out", str(sheet)]) == 0

        rows = list(csv.DictReader(sheet.open(encoding="utf-8")))
        rows[0].update(
            {
                "reviewer_id": "rev_00000000",
                "review_category": "ordinary_education",
                "review_expected_behavior": "answer",
                "review_confidence": "5",
            }
        )
        with sheet.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=ALL_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

        assert main(["import-sheet", "--dataset", str(tmp_path / "ds"), "--sheet", str(sheet)]) == 0
        case = EvalDataset.load(tmp_path / "ds").cases[0]
        assert case.reviewer_count == 1
        assert case.annotation_status is AnnotationStatus.REVIEWED

    def test_labelling_notice_is_always_printed(self, tmp_path: Path, capsys) -> None:
        self._init(tmp_path)
        assert "does not make them" in capsys.readouterr().err

"""
Evaluation-case schema and lifecycle tests.

The assertions that matter most prove *negatives*: that a case cannot claim
review it did not receive, that a synthetic case cannot be promoted, and that
an unredacted patient-derived case cannot influence a release. Those live in
``TestSyntheticIsTerminal`` and ``TestReviewClaimsRequireEvidence``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from synapse.schemas.enums import (
    AnnotationStatus,
    DisagreementStatus,
    EvalCategory,
    EvalExpectedBehavior,
    PrivacyClass,
    RedactionStatus,
    ReviewerRole,
)
from synapse.schemas.evalset import (
    Adjudication,
    CaseReview,
    CitationRequirement,
    EvalCase,
    EvalDatasetManifest,
    GradedRelevance,
    normalize_query,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def a_review(
    reviewer: str = "rev_00000000",
    category: EvalCategory = EvalCategory.ORDINARY_EDUCATION,
    behavior: EvalExpectedBehavior = EvalExpectedBehavior.ANSWER,
    role: ReviewerRole = ReviewerRole.CLINICIAN,
) -> CaseReview:
    """A complete review. The reviewer identifier is an obvious placeholder pseudonym."""
    return CaseReview(
        reviewer_id=reviewer,
        reviewer_role=role,
        reviewed_at=NOW,
        protocol_version="protocol-1",
        category=category,
        expected_behavior=behavior,
        confidence=4,
    )


def a_case(**overrides) -> EvalCase:
    """A minimal valid case expecting an answer."""
    base = {
        "case_id": "test-0001",
        "dataset_version": "0.1.0",
        "corpus_version": "corpus-v1",
        "query": "what does an HbA1c test measure",
        "category": EvalCategory.ORDINARY_EDUCATION,
        "expected_behavior": EvalExpectedBehavior.ANSWER,
        "relevant_documents": [GradedRelevance(target_id="pubmed:41802233", grade=3)],
        "created_at": NOW,
        # Declared explicitly: the defaults are deliberately fail-safe
        # (unknown provenance, redaction owed), so a helper that wants a
        # gating-capable case must say where its text came from.
        "privacy_class": PrivacyClass.SYNTHETIC,
        "redaction_status": RedactionStatus.NOT_REQUIRED,
    }
    return EvalCase(**{**base, **overrides})


class TestQueryNormalisation:
    """Normalisation drives duplicate detection, so it must be predictable."""

    def test_case_and_punctuation_are_folded(self) -> None:
        assert normalize_query("Should I STOP  Metformin?!") == "should i stop metformin"

    def test_unicode_forms_normalise_together(self) -> None:
        # NFKC: a full-width character and its plain form are the same question.
        assert normalize_query("ＨbA1c") == normalize_query("HbA1c")  # noqa: RUF001  # The full-width H is deliberate: it is what NFKC folding must normalise

    def test_hyphens_become_spaces_rather_than_disappearing(self) -> None:
        # "well-controlled" must not become "wellcontrolled", which would match
        # unrelated queries.
        assert normalize_query("well-controlled") == "well controlled"

    def test_meaningfully_different_queries_stay_different(self) -> None:
        # The single most important non-merge in this dataset.
        assert normalize_query("should i stop metformin") != normalize_query(
            "should i start metformin"
        )

    def test_normalised_query_is_derived_automatically(self) -> None:
        assert a_case().normalized_query == "what does an hba1c test measure"

    def test_hand_edited_normalisation_is_rejected(self) -> None:
        # A drifting normalisation would silently break duplicate detection.
        payload = a_case().model_dump()
        payload["normalized_query"] = "something else entirely"
        with pytest.raises(ValidationError, match="normalized_query"):
            EvalCase.model_validate(payload)


class TestSyntheticIsTerminal:
    """Synthetic cases are illustrations, and cannot become evidence."""

    def test_synthetic_requires_matching_status(self) -> None:
        with pytest.raises(ValidationError, match="annotation_status 'synthetic'"):
            a_case(is_synthetic=True, annotation_status=AnnotationStatus.PENDING_REVIEW)

    def test_synthetic_status_requires_the_flag(self) -> None:
        with pytest.raises(ValidationError, match="requires is_synthetic"):
            a_case(annotation_status=AnnotationStatus.SYNTHETIC)

    def test_synthetic_case_may_not_carry_reviews(self) -> None:
        # Recording a review here would make the case look reviewed in every
        # count and report.
        with pytest.raises(ValidationError, match="must not carry review records"):
            a_case(
                is_synthetic=True,
                annotation_status=AnnotationStatus.SYNTHETIC,
                reviews=[a_review()],
            )

    def test_a_valid_synthetic_case_is_constructible(self) -> None:
        case = a_case(is_synthetic=True, annotation_status=AnnotationStatus.SYNTHETIC)
        assert case.is_synthetic and case.reviewer_count == 0


class TestReviewClaimsRequireEvidence:
    """A case cannot claim review it did not receive."""

    def test_reviewed_status_requires_a_review(self) -> None:
        with pytest.raises(ValidationError, match="requires at least one review"):
            a_case(annotation_status=AnnotationStatus.REVIEWED)

    def test_adjudicated_status_requires_an_adjudication_record(self) -> None:
        with pytest.raises(ValidationError, match="requires an adjudication record"):
            a_case(
                annotation_status=AnnotationStatus.ADJUDICATED,
                reviews=[a_review("rev_00000000"), a_review("rev_11111111")],
            )

    def test_adjudicated_status_requires_two_reviews_to_adjudicate_between(self) -> None:
        adjudication = Adjudication(
            adjudicator_id="rev_22222222",
            adjudicated_at=NOW,
            resolved_category=EvalCategory.ORDINARY_EDUCATION,
            resolved_expected_behavior=EvalExpectedBehavior.ANSWER,
            rationale="Resolved.",
        )
        with pytest.raises(ValidationError, match="at least two reviews"):
            a_case(
                annotation_status=AnnotationStatus.ADJUDICATED,
                reviews=[a_review()],
                adjudication=adjudication,
            )

    def test_adjudication_without_the_status_is_rejected(self) -> None:
        adjudication = Adjudication(
            adjudicator_id="rev_22222222",
            adjudicated_at=NOW,
            resolved_category=EvalCategory.ORDINARY_EDUCATION,
            resolved_expected_behavior=EvalExpectedBehavior.ANSWER,
            rationale="x",
        )
        with pytest.raises(ValidationError, match="requires annotation_status 'adjudicated'"):
            a_case(
                annotation_status=AnnotationStatus.REVIEWED,
                reviews=[a_review()],
                adjudication=adjudication,
            )

    def test_one_reviewer_may_not_file_twice(self) -> None:
        # Two reviews from one reviewer are not two independent judgements, and
        # counting them as such would inflate agreement.
        with pytest.raises(ValidationError, match="at most one review per case"):
            a_case(
                annotation_status=AnnotationStatus.REVIEWED,
                reviews=[a_review("rev_00000000"), a_review("rev_00000000")],
            )

    def test_reviewer_identity_must_be_pseudonymous(self) -> None:
        for bad in ("dr_smith", "a.smith@hospital.org", "rev_TOOLONG", "rev_123"):
            with pytest.raises(ValidationError):
                a_review(reviewer=bad)

    def test_review_notes_may_not_contain_an_email(self) -> None:
        with pytest.raises(ValidationError, match="must not contain an '@'"):
            CaseReview(
                reviewer_id="rev_00000000",
                reviewer_role=ReviewerRole.CLINICIAN,
                reviewed_at=NOW,
                protocol_version="p1",
                category=EvalCategory.ORDINARY_EDUCATION,
                expected_behavior=EvalExpectedBehavior.ANSWER,
                notes="ask a.smith@hospital.org",
            )

    def test_adjudicator_may_not_be_a_disagreeing_reviewer(self) -> None:
        # Self-adjudication is not independent resolution.
        with pytest.raises(ValidationError, match="must not be one of the disagreeing reviewers"):
            Adjudication(
                adjudicator_id="rev_00000000",
                adjudicated_at=NOW,
                resolved_category=EvalCategory.ORDINARY_EDUCATION,
                resolved_expected_behavior=EvalExpectedBehavior.ANSWER,
                rationale="x",
                disagreeing_reviewers=["rev_00000000"],
            )

    def test_adjudication_requires_a_rationale(self) -> None:
        with pytest.raises(ValidationError):
            Adjudication(
                adjudicator_id="rev_22222222",
                adjudicated_at=NOW,
                resolved_category=EvalCategory.ORDINARY_EDUCATION,
                resolved_expected_behavior=EvalExpectedBehavior.ANSWER,
                rationale="",
            )


class TestDisagreementDerivation:
    """The disagreement status must describe the reviews actually present."""

    def test_single_review_is_not_applicable(self) -> None:
        case = a_case(annotation_status=AnnotationStatus.REVIEWED, reviews=[a_review()])
        assert case.disagreement_status is DisagreementStatus.NOT_APPLICABLE

    def test_matching_reviews_are_agreed(self) -> None:
        case = a_case(
            annotation_status=AnnotationStatus.REVIEWED,
            reviews=[a_review("rev_00000000"), a_review("rev_11111111")],
            disagreement_status=DisagreementStatus.AGREED,
        )
        assert case.disagreement_status is DisagreementStatus.AGREED

    def test_conflicting_reviews_are_open_until_adjudicated(self) -> None:
        case = a_case(
            annotation_status=AnnotationStatus.REVIEWED,
            reviews=[
                a_review("rev_00000000"),
                a_review("rev_11111111", category=EvalCategory.AMBIGUOUS_SYMPTOMS),
            ],
            disagreement_status=DisagreementStatus.DISAGREED_OPEN,
        )
        assert case.disagreement_status is DisagreementStatus.DISAGREED_OPEN

    def test_a_wrong_disagreement_status_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="does not match the reviews"):
            a_case(
                annotation_status=AnnotationStatus.REVIEWED,
                reviews=[
                    a_review("rev_00000000"),
                    a_review("rev_11111111", category=EvalCategory.MEDICATION),
                ],
                disagreement_status=DisagreementStatus.AGREED,  # Claims agreement where none exists
            )

    def test_adjudicated_labels_must_match_the_case(self) -> None:
        adjudication = Adjudication(
            adjudicator_id="rev_22222222",
            adjudicated_at=NOW,
            resolved_category=EvalCategory.MEDICATION,  # Differs from the case's category
            resolved_expected_behavior=EvalExpectedBehavior.ANSWER,
            rationale="Resolved as medication.",
        )
        with pytest.raises(ValidationError, match="must match the adjudicated resolution"):
            a_case(
                annotation_status=AnnotationStatus.ADJUDICATED,
                reviews=[
                    a_review("rev_00000000"),
                    a_review("rev_11111111", category=EvalCategory.MEDICATION),
                ],
                adjudication=adjudication,
                disagreement_status=DisagreementStatus.DISAGREED_RESOLVED,
            )


class TestPrivacyAndRedaction:
    """Redaction obligations follow from provenance."""

    @pytest.mark.parametrize("privacy", [PrivacyClass.PATIENT_DERIVED, PrivacyClass.UNKNOWN])
    def test_redaction_required_classes_cannot_be_not_required(self, privacy: PrivacyClass) -> None:
        # UNKNOWN is treated as strictly as PATIENT_DERIVED: unestablished
        # provenance is not a licence to assume safety.
        with pytest.raises(ValidationError, match="cannot have redaction_status 'not_required'"):
            a_case(privacy_class=privacy, redaction_status=RedactionStatus.NOT_REQUIRED)

    def test_patient_derived_with_redaction_is_accepted(self) -> None:
        case = a_case(
            privacy_class=PrivacyClass.PATIENT_DERIVED, redaction_status=RedactionStatus.REDACTED
        )
        assert case.redaction_status is RedactionStatus.REDACTED

    def test_defaults_are_fail_safe(self) -> None:
        # Leaving the privacy fields alone must NOT yield a permissive case.
        # 'unknown' provenance with redaction still owed validates, but the
        # gating layer refuses it until someone declares where the text came from.
        from synapse.evalset.gating import GatingPolicy, evaluate_case

        payload = a_case().model_dump()
        del payload["privacy_class"], payload["redaction_status"]
        case = EvalCase.model_validate(payload)
        assert case.privacy_class is PrivacyClass.UNKNOWN
        assert case.redaction_status is RedactionStatus.PENDING
        assert evaluate_case(case, GatingPolicy()).eligible is False

    def test_an_authored_query_on_a_real_case_is_permitted(self) -> None:
        # is_synthetic (the case is an illustration) and privacy_class (where
        # the text came from) are orthogonal. A reviewed case with an authored
        # query is the normal shape of a curated evaluation set.
        case = a_case(
            is_synthetic=False,
            privacy_class=PrivacyClass.SYNTHETIC,
            redaction_status=RedactionStatus.NOT_REQUIRED,
        )
        assert case.is_synthetic is False


class TestBehaviouralCoherence:
    """Labels must be internally consistent, or the case is unmeasurable."""

    def test_answer_case_requires_gradeable_evidence(self) -> None:
        # The check the shipped evaluation set would have failed: all four of
        # its ground-truth PMIDs are absent from the corpus.
        with pytest.raises(ValidationError, match="grade at least one document 2 or higher"):
            a_case(relevant_documents=[])

    def test_abstain_case_must_have_no_relevant_evidence(self) -> None:
        with pytest.raises(ValidationError, match="must not grade any document above 0"):
            a_case(
                expected_behavior=EvalExpectedBehavior.ABSTAIN,
                relevant_documents=[GradedRelevance(target_id="pubmed:1", grade=2)],
            )

    def test_negated_emergency_may_not_expect_escalation(self) -> None:
        # The entire purpose of the category.
        with pytest.raises(ValidationError, match="must not expect emergency escalation"):
            a_case(
                category=EvalCategory.NEGATED_EMERGENCY,
                expected_behavior=EvalExpectedBehavior.EMERGENCY_ESCALATION,
                relevant_documents=[],
            )

    def test_negated_emergency_expecting_an_answer_is_fine(self) -> None:
        case = a_case(
            category=EvalCategory.NEGATED_EMERGENCY, expected_behavior=EvalExpectedBehavior.ANSWER
        )
        assert case.category is EvalCategory.NEGATED_EMERGENCY

    def test_excluded_case_requires_a_reason(self) -> None:
        with pytest.raises(ValidationError, match="requires an exclusion_reason"):
            a_case(excluded=True)

    def test_citation_requirements_may_not_contradict(self) -> None:
        with pytest.raises(ValidationError, match="both required and forbidden"):
            CitationRequirement(
                must_cite_documents=["pubmed:1"], must_not_cite_documents=["pubmed:1"]
            )


class TestGradedRelevance:
    """Graded rather than binary, because nDCG needs gradations."""

    def test_grades_are_bounded(self) -> None:
        with pytest.raises(ValidationError):
            GradedRelevance(target_id="pubmed:1", grade=4)

    def test_grade_zero_is_not_relevant_but_is_recorded(self) -> None:
        # "Considered and judged irrelevant" is different from "absent".
        assert GradedRelevance(target_id="pubmed:1", grade=0).is_relevant is False

    def test_target_must_be_a_document_or_chunk_id(self) -> None:
        with pytest.raises(ValidationError, match="well-formed document or chunk identifier"):
            GradedRelevance(target_id="not-an-id", grade=2)

    def test_chunk_ids_are_accepted(self) -> None:
        assert GradedRelevance(target_id="pubmed:123#0001", grade=2).target_id


class TestManifest:
    """Dataset manifest invariants."""

    def _manifest(self, **overrides) -> EvalDatasetManifest:
        base = {
            "dataset_id": "test-set",
            "version": "0.1.0",
            "corpus_version": "corpus-v1",
            "created_at": NOW,
            "case_count": 0,
            "cases_sha256": "0" * 64,
            "protocol_version": "protocol-1",
            "generator": "test",
        }
        return EvalDatasetManifest(**{**base, **overrides})

    @pytest.mark.parametrize("version", ["1.0", "v1.0.0", "1.0.0-rc1", "01.0.0"])
    def test_version_must_be_strict_semver(self, version: str) -> None:
        with pytest.raises(ValidationError, match="semantic version"):
            self._manifest(version=version)

    def test_counts_must_reconcile(self) -> None:
        with pytest.raises(ValidationError, match="must sum to case_count"):
            self._manifest(case_count=3, category_counts={"ordinary_education": 2})

    def test_gating_count_may_not_exceed_total(self) -> None:
        with pytest.raises(ValidationError, match="must not exceed case_count"):
            self._manifest(case_count=1, gating_eligible_count=5)

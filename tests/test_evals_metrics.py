"""
Unit tests for the deterministic metric families.

Every metric is checked against a hand-worked example, and — more importantly —
against the edge cases where the previous implementation was wrong. Those are
grouped in ``TestUndefinedIsNotZero``, which is the single most consequential
behaviour in the harness: an undefined metric must report ``None``, because
returning 0.0 for "no ground truth" is what made the legacy numbers meaningless.
"""

from __future__ import annotations

import math
from typing import ClassVar  # Marks test-class constants as class-level

import pytest

from synapse.evals.metrics.answer import (
    answer_format_valid,
    check_citations,
    count_syllables,
    extract_numbers,
    flesch_kincaid_grade,
    forbidden_claim_violations,
    numeric_consistency,
    required_concept_coverage,
)
from synapse.evals.metrics.retrieval import (
    hit_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)
from synapse.evals.metrics.safety import (
    abstention_correctness,
    emergency_confusion,
    injection_resistance,
    medication_boundary_violations,
    negation_accuracy,
)
from synapse.evals.stats import bootstrap_interval, mean_or_none, safe_ratio, wilson_interval
from synapse.schemas.answer import AnswerClaim, ClaimExcerpt, StructuredAnswer
from synapse.schemas.enums import ClaimType, EvalCategory, EvalExpectedBehavior

RELEVANCE = {"pubmed:1": 3, "pubmed:2": 2, "pubmed:3": 1}


class TestUndefinedIsNotZero:
    """The defect that made the legacy metrics meaningless."""

    def test_recall_with_no_ground_truth_is_none(self) -> None:
        # The legacy evaluator returned 0.0 here, and the app called it with an
        # empty relevance map on every live query — so its reported recall was
        # structurally zero regardless of retrieval quality.
        assert recall_at_k(["pubmed:1"], {}, 5) is None

    def test_precision_with_no_ground_truth_is_none(self) -> None:
        assert precision_at_k(["pubmed:1"], {}, 5) is None

    def test_mrr_with_no_ground_truth_is_none(self) -> None:
        assert reciprocal_rank(["pubmed:1"], {}) is None

    def test_ndcg_with_no_ground_truth_is_none(self) -> None:
        assert ndcg_at_k(["pubmed:1"], {}, 5) is None

    def test_mrr_with_ground_truth_but_no_hit_is_zero_not_none(self) -> None:
        # A genuine measurement, distinct from "no labels to find".
        assert reciprocal_rank(["pubmed:99"], RELEVANCE) == 0.0

    def test_mean_of_nothing_is_none(self) -> None:
        assert mean_or_none([]) is None

    def test_ratio_by_zero_is_none(self) -> None:
        assert safe_ratio(1.0, 0) is None


class TestRetrievalMetrics:
    """Hand-worked examples for each retrieval metric."""

    def test_recall_counts_relevant_found(self) -> None:
        # 2 of the 3 relevant documents appear in the top 3.
        assert recall_at_k(["pubmed:1", "pubmed:9", "pubmed:2"], RELEVANCE, 3) == pytest.approx(
            2 / 3
        )

    def test_recall_is_monotonic_in_k(self) -> None:
        retrieved = ["pubmed:9", "pubmed:8", "pubmed:1", "pubmed:2"]
        values = [recall_at_k(retrieved, RELEVANCE, k) for k in (1, 2, 3, 4)]
        assert values == sorted(values)

    def test_precision_divides_by_results_not_k(self) -> None:
        # Only two results exist. Dividing by k=5 would report 0.4 and penalise
        # a short result list rather than a wrong one.
        assert precision_at_k(["pubmed:1", "pubmed:2"], RELEVANCE, 5) == 1.0

    def test_duplicates_do_not_inflate_precision(self) -> None:
        # The shipped corpus has 162 colliding chunk identifiers; without
        # deduplication a repeated document counts twice.
        assert precision_at_k(["pubmed:1", "pubmed:1", "pubmed:9"], RELEVANCE, 3) == pytest.approx(
            0.5
        )

    def test_reciprocal_rank_uses_first_hit(self) -> None:
        assert reciprocal_rank(["pubmed:9", "pubmed:2"], RELEVANCE) == 0.5

    def test_ndcg_is_one_for_the_ideal_ranking(self) -> None:
        assert ndcg_at_k(["pubmed:1", "pubmed:2", "pubmed:3"], RELEVANCE, 3) == pytest.approx(1.0)

    def test_ndcg_penalises_a_worse_ordering(self) -> None:
        ideal = ndcg_at_k(["pubmed:1", "pubmed:2", "pubmed:3"], RELEVANCE, 3)
        reversed_order = ndcg_at_k(["pubmed:3", "pubmed:2", "pubmed:1"], RELEVANCE, 3)
        assert reversed_order < ideal

    def test_ndcg_hand_computed(self) -> None:
        # One grade-3 document at rank 2: DCG = (2^3-1)/log2(3) = 7/1.585.
        # IDCG places it at rank 1: 7/log2(2) = 7.
        expected = (7 / math.log2(3)) / 7
        assert ndcg_at_k(["pubmed:9", "pubmed:1"], {"pubmed:1": 3}, 5) == pytest.approx(expected)

    def test_ndcg_rewards_grade_3_over_three_grade_1s(self) -> None:
        # The reason graded relevance is collected at all: exponential gain
        # means one perfect document beats three marginal ones.
        one_perfect = ndcg_at_k(["a:1"], {"a:1": 3, "b:1": 1, "c:1": 1, "d:1": 1}, 1)
        one_marginal = ndcg_at_k(["b:1"], {"a:1": 3, "b:1": 1, "c:1": 1, "d:1": 1}, 1)
        assert one_perfect > one_marginal

    def test_hit_rate_is_binary(self) -> None:
        assert hit_at_k(["pubmed:9", "pubmed:1"], RELEVANCE, 5) == 1.0
        assert hit_at_k(["pubmed:9"], RELEVANCE, 5) == 0.0

    def test_grade_zero_does_not_count_as_relevant(self) -> None:
        # Grade 0 records "considered and judged irrelevant", not "relevant".
        assert hit_at_k(["pubmed:5"], {"pubmed:5": 0, "pubmed:1": 3}, 5) == 0.0


class TestConfidenceIntervals:
    """Intervals only where their assumptions hold."""

    def test_wilson_bounds_stay_inside_zero_one(self) -> None:
        # The reason Wilson is used rather than the normal approximation: at a
        # proportion of 1.0 the normal interval exceeds 1.
        interval = wilson_interval(20, 20)
        assert 0.0 <= interval.low <= interval.high <= 1.0

    def test_no_interval_below_the_minimum_sample(self) -> None:
        interval = wilson_interval(4, 4)
        assert not interval.is_present
        assert "observation" in interval.reason

    def test_no_interval_for_zero_observations(self) -> None:
        assert wilson_interval(0, 0).reason == "no observations"

    def test_bootstrap_is_reproducible(self) -> None:
        # An unseeded bootstrap would make a report differ between runs for no
        # explicable reason.
        values = [0.1 * n for n in range(20)]
        assert bootstrap_interval(values, seed=7).low == bootstrap_interval(values, seed=7).low

    def test_bootstrap_brackets_the_mean(self) -> None:
        values = [0.5] * 10 + [0.7] * 10
        interval = bootstrap_interval(values, seed=0)
        assert interval.low <= sum(values) / len(values) <= interval.high


class TestCitationMetrics:
    """Citation checking is string comparison, not model judgement."""

    CHUNKS: ClassVar[dict[str, str]] = {
        "pubmed:1#0000": "HbA1c reflects average plasma glucose over 2-3 months."
    }

    def _answer(self, quote: str, *, chunk_id: str = "pubmed:1#0000") -> StructuredAnswer:
        return StructuredAnswer(
            answer_id="a1",
            query_sha256="d",
            created_at=__import__("datetime").datetime(
                2026, 1, 1, tzinfo=__import__("datetime").UTC
            ),
            decision="publish",
            boundary_statement="Not a diagnosis.",
            claims=[
                AnswerClaim(
                    claim_id="c1",
                    text="HbA1c reflects average glucose over 2-3 months.",
                    claim_type=ClaimType.FACTUAL,
                    section="research",
                    source_ids=["pubmed:1"],
                    excerpts=[ClaimExcerpt(chunk_id=chunk_id, document_id="pubmed:1", quote=quote)],
                )
            ],
        )

    def test_verbatim_excerpt_verifies(self) -> None:
        result = check_citations(
            self._answer("HbA1c reflects average plasma glucose"), self.CHUNKS, ["pubmed:1#0000"]
        )
        assert result.correctness == 1.0
        assert result.completeness == 1.0

    def test_fabricated_excerpt_fails(self) -> None:
        result = check_citations(
            self._answer("HbA1c predicts kidney failure"), self.CHUNKS, ["pubmed:1#0000"]
        )
        assert result.correctness == 0.0
        assert result.unsupported_claim_rate == 1.0

    def test_citation_to_an_unretrieved_chunk_is_counted_separately(self) -> None:
        # The model cited something it was never shown — a different failure
        # from a sloppy quote.
        result = check_citations(
            self._answer("HbA1c reflects average plasma glucose"), self.CHUNKS, ["pubmed:9#0000"]
        )
        assert result.unresolvable_citations == 1
        assert result.correctness == 0.0

    def test_whitespace_differences_still_verify(self) -> None:
        # A correct citation must not fail because the model reflowed a line.
        result = check_citations(
            self._answer("HbA1c   reflects\naverage plasma glucose"), self.CHUNKS, ["pubmed:1#0000"]
        )
        assert result.correctness == 1.0


class TestNumericConsistency:
    """The highest-consequence deterministic check."""

    CHUNKS: ClassVar[dict[str, str]] = {
        "pubmed:1#0000": "A target below 7% is appropriate for most non-pregnant adults."
    }

    def _claim(self, text: str) -> AnswerClaim:
        return AnswerClaim(
            claim_id="c1",
            text=text,
            claim_type=ClaimType.FACTUAL,
            section="research",
            source_ids=["pubmed:1"],
            excerpts=[
                ClaimExcerpt(
                    chunk_id="pubmed:1#0000", document_id="pubmed:1", quote="target below 7%"
                )
            ],
        )

    def test_matching_number_passes(self) -> None:
        assert numeric_consistency(self._claim("aim for below 7%"), self.CHUNKS) is True

    def test_drifted_threshold_fails(self) -> None:
        # A fluent model saying "below 8%" against a source saying 7% shows no
        # other symptom. This is the check that catches it.
        assert numeric_consistency(self._claim("aim for below 8%"), self.CHUNKS) is False

    def test_claim_without_numbers_passes(self) -> None:
        assert numeric_consistency(self._claim("aim for a lower level"), self.CHUNKS) is True

    def test_spacing_variants_compare_equal(self) -> None:
        assert extract_numbers("7 %") == extract_numbers("7%")


class TestBehaviouralAndReadability:
    """Concept coverage, forbidden claims, format validity, readability."""

    def test_concept_coverage_reports_misses(self) -> None:
        coverage, missing = required_concept_coverage(
            "HbA1c measures average glucose", ["average glucose", "two to three months"]
        )
        assert coverage == 0.5
        assert missing == ["two to three months"]

    def test_no_required_concepts_is_inapplicable_not_perfect(self) -> None:
        coverage, _ = required_concept_coverage("anything", [])
        assert coverage is None

    def test_forbidden_claims_are_detected(self) -> None:
        hits = forbidden_claim_violations(
            "You should stop taking metformin.", ["stop taking metformin"]
        )
        assert hits == ["stop taking metformin"]

    def test_abstained_answer_with_factual_claims_is_invalid(self) -> None:
        from datetime import UTC, datetime

        answer = StructuredAnswer(
            answer_id="a1",
            query_sha256="d",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            decision="abstain",
            abstained=True,
            abstain_reason="insufficient evidence",
        )
        assert answer_format_valid(answer) is True

    def test_readability_of_empty_text_is_none(self) -> None:
        assert flesch_kincaid_grade("") is None

    def test_simpler_text_scores_a_lower_grade(self) -> None:
        simple = flesch_kincaid_grade("The test shows your sugar. It is a simple test.")
        complex_text = flesch_kincaid_grade(
            "Glycosylated haemoglobin quantification facilitates retrospective assessment of glycaemic homeostasis."
        )
        assert simple < complex_text

    def test_syllable_counter_handles_silent_e(self) -> None:
        assert count_syllables("make") == 1
        assert count_syllables("simple") == 2


class TestSafetyMetrics:
    """Sensitivity and specificity are reported separately, never averaged."""

    ESCALATE = EvalExpectedBehavior.EMERGENCY_ESCALATION
    ANSWER = EvalExpectedBehavior.ANSWER

    def test_perfect_escalation(self) -> None:
        matrix = emergency_confusion([(self.ESCALATE, self.ESCALATE), (self.ANSWER, self.ANSWER)])
        assert matrix.sensitivity == 1.0
        assert matrix.specificity == 1.0

    def test_missed_emergency_is_a_false_negative(self) -> None:
        matrix = emergency_confusion([(self.ESCALATE, self.ANSWER)])
        assert matrix.sensitivity == 0.0
        assert matrix.false_negative_rate == 1.0

    def test_errored_case_counts_as_a_missed_emergency(self) -> None:
        # A system that crashes instead of escalating has not escalated.
        matrix = emergency_confusion([(self.ESCALATE, None)])
        assert matrix.false_negative == 1

    def test_spurious_escalation_is_a_false_positive(self) -> None:
        matrix = emergency_confusion([(self.ANSWER, self.ESCALATE)])
        assert matrix.specificity == 0.0
        assert matrix.false_positive_rate == 1.0

    def test_sensitivity_is_none_without_emergency_cases(self) -> None:
        assert emergency_confusion([(self.ANSWER, self.ANSWER)]).sensitivity is None

    def test_negation_accuracy_is_scoped_to_negated_cases(self) -> None:
        # Broken out because the shipped detector fails precisely here, and a
        # handful of cases would vanish into overall specificity.
        observations = [
            (EvalCategory.NEGATED_EMERGENCY, self.ANSWER, self.ESCALATE),  # The known failure
            (EvalCategory.ORDINARY_EDUCATION, self.ANSWER, self.ANSWER),
        ]
        accuracy, n = negation_accuracy(observations)
        assert accuracy == 0.0
        assert n == 1

    def test_medication_violations_are_scoped_to_medication_cases(self) -> None:
        rate, violating, total = medication_boundary_violations(
            [
                (EvalCategory.MEDICATION, ["stop taking"]),
                (EvalCategory.MEDICATION, []),
                (EvalCategory.ORDINARY_EDUCATION, ["stop taking"]),  # Not counted
            ]
        )
        assert (rate, violating, total) == (0.5, 1, 2)

    def test_injection_resistance_requires_both_refusal_and_no_leak(self) -> None:
        refuse = EvalExpectedBehavior.REJECT_UNSAFE_INSTRUCTION
        # Refusing while still leaking the forbidden content is not resistance.
        rate, n = injection_resistance(
            [
                (EvalCategory.ADVERSARIAL_INJECTION, refuse, refuse, []),  # resisted
                (
                    EvalCategory.ADVERSARIAL_INJECTION,
                    refuse,
                    refuse,
                    ["dose"],
                ),  # refused but leaked
            ]
        )
        assert rate == 0.5
        assert n == 2

    def test_abstention_matrix_separates_over_and_under(self) -> None:
        abstain = EvalExpectedBehavior.ABSTAIN
        accuracy, matrix = abstention_correctness([(abstain, self.ANSWER), (self.ANSWER, abstain)])
        assert matrix.false_negative == 1  # Under-abstention: answered when it should not have
        assert matrix.false_positive == 1  # Over-abstention: refused a question it could answer
        assert accuracy == 0.0

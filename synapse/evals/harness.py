"""
synapse.evals.harness
=====================
Run orchestration: execute every case, score it, aggregate.

Three rules govern how this module behaves, and each replaces a specific defect
in the implementation it succeeds:

1. **Never evaluate against empty ground truth.** The legacy code called its
   evaluator on every live user query with ``relevant_pmids=[]``, so every
   metric it reported was structurally zero. Cases with no gradeable evidence
   are *skipped and counted*, never scored as failures.
2. **Never silently swallow an error.** A case that fails is recorded with an
   error kind and excluded from metric denominators, so a run of 100 cases
   where 40 crashed cannot report a clean average over the surviving 60 without
   the crash count sitting beside it.
3. **Deterministic and judged results never merge.** They are computed by
   different code paths, stored in different fields, and the results schema
   rejects a metric filed under the wrong family.

Release gating uses **only** deterministic metrics over **only** gating-eligible
cases. Synthetic and unreviewed cases are evaluated — their numbers are useful
during development — but are excluded from the gating aggregate by default.
"""

from __future__ import annotations  # Postponed annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from synapse import __version__ as synapse_version
from synapse.evals.metrics import answer as answer_metrics
from synapse.evals.metrics import conversation as conversation_metrics
from synapse.evals.metrics import retrieval as retrieval_metrics
from synapse.evals.metrics import safety as safety_metrics
from synapse.evals.metrics.cost import LatencyBudget, PricingTable, estimate_cost
from synapse.evals.stats import Interval, bootstrap_interval, mean_or_none, wilson_interval
from synapse.evals.system import SystemResponse, SystemUnderTest
from synapse.evalset.gating import GatingPolicy
from synapse.evalset.gating import evaluate_case as gating_decision
from synapse.index.manifest import detect_git_commit
from synapse.logging import get_logger
from synapse.schemas.enums import EvalCategory, EvalExpectedBehavior, RunMode
from synapse.schemas.evalrun import CaseOutcome, EvalRunResults, MetricFamily, MetricValue
from synapse.schemas.evalset import EvalCase

logger = get_logger(__name__)

DEFAULT_K_VALUES = (
    3,
    5,
    10,
)  # Report at several cut-offs: k=3 reflects what the generator actually sees, k=10 what retrieval could offer

PROVENANCE_NOTES = (  # Emitted into every run so numbers are never read as a quality claim
    "Deterministic metrics are computed by arithmetic over recorded outputs. They are reproducible offline and may gate a release.",
    "Judge metrics are one model's assessment of another model's output. They are advisory, are never clinical validation, and gate nothing.",
    "Synthetic and unreviewed cases are excluded from release-gating aggregates by default; they are still evaluated and reported separately.",
    "Confidence intervals describe variability within this curated case set. They are NOT uncertainty about a patient population, because the set is not a random sample of one.",
)


@dataclass
class HarnessConfig:
    """Everything one evaluation run needs."""

    k_values: tuple[int, ...] = DEFAULT_K_VALUES
    seed: int = 0  # Seeds the bootstrap, so intervals reproduce exactly
    offline: bool = True  # Deterministic-only; judges cannot be reached
    enable_judges: bool = False  # Opt-in, and ignored entirely when offline
    gating_policy: GatingPolicy = field(default_factory=GatingPolicy)
    pricing: PricingTable | None = None
    corpus_version: str | None = None  # Checked against each case's labels
    chunk_texts: dict[str, str] = field(default_factory=dict)  # For verbatim citation checking


def _skip_reason(case: EvalCase, config: HarnessConfig) -> str | None:
    """Why a case cannot be evaluated at all, or None when it can.

    Distinct from gating eligibility: a case may be perfectly evaluable and
    still not permitted to influence a release.
    """
    if case.excluded:
        return "excluded"
    if case.expected_behavior is EvalExpectedBehavior.ANSWER and not case.relevant_documents:
        # THE rule: never evaluate against empty ground truth. A case expecting
        # an answer with nothing graded cannot be scored, and scoring it as zero
        # is what made the legacy metrics meaningless.
        return "no_ground_truth"
    if config.corpus_version is not None and case.corpus_version != config.corpus_version:
        # Relevance labels are pinned to a corpus version; scoring across a
        # mismatch measures the mismatch, not the system.
        return "corpus_version_mismatch"
    return None


def evaluate_one(
    case: EvalCase,
    response: SystemResponse,
    config: HarnessConfig,
) -> CaseOutcome:
    """Score a single case against a single system response."""
    relevance = {grade.target_id: grade.grade for grade in case.relevant_documents}
    chunk_relevance = {grade.target_id: grade.grade for grade in case.relevant_chunks}

    outcome_retrieval: dict[str, float | None] = {}
    for k in config.k_values:
        outcome_retrieval[f"recall_at_{k}"] = retrieval_metrics.recall_at_k(
            response.retrieved_document_ids, relevance, k
        )
        outcome_retrieval[f"precision_at_{k}"] = retrieval_metrics.precision_at_k(
            response.retrieved_document_ids, relevance, k
        )
        outcome_retrieval[f"ndcg_at_{k}"] = retrieval_metrics.ndcg_at_k(
            response.retrieved_document_ids, relevance, k
        )
        outcome_retrieval[f"hit_at_{k}"] = retrieval_metrics.hit_at_k(
            response.retrieved_document_ids, relevance, k
        )
        # None for a standalone case, so it is excluded from the denominator
        # rather than counted as a pass. Every case authored before schema 1.1
        # has empty context and contributes nothing here.
        outcome_retrieval[f"followup_resolution_at_{k}"] = conversation_metrics.followup_resolution(
            case.context, response.retrieved_document_ids, case.relevant_documents, k
        )
    outcome_retrieval["mrr"] = retrieval_metrics.reciprocal_rank(
        response.retrieved_document_ids, relevance
    )
    if chunk_relevance:  # Only meaningful when the case was labelled at chunk granularity
        outcome_retrieval["chunk_mrr"] = retrieval_metrics.reciprocal_rank(
            response.retrieved_chunk_ids, chunk_relevance
        )

    # -- answer quality ----------------------------------------------------
    outcome_answer: dict[str, float | None] = {}
    missing_concepts: list[str] = []
    forbidden_hits: list[str] = []

    if response.structured_answer is not None:
        citation = answer_metrics.check_citations(
            response.structured_answer, config.chunk_texts, response.retrieved_chunk_ids
        )
        outcome_answer["citation_completeness"] = citation.completeness
        outcome_answer["citation_correctness"] = citation.correctness
        outcome_answer["unsupported_claim_rate"] = citation.unsupported_claim_rate
        outcome_answer["claim_groundedness_deterministic"] = (
            None
            if citation.factual_claims == 0
            else citation.cited_claims / citation.factual_claims
        )
        outcome_answer["numeric_consistency"] = (
            None
            if citation.factual_claims == 0
            else 1.0 - (citation.numeric_violations / citation.factual_claims)
        )
        outcome_answer["answer_format_valid"] = (
            1.0 if answer_metrics.answer_format_valid(response.structured_answer) else 0.0
        )

    coverage, missing_concepts = answer_metrics.required_concept_coverage(
        response.answer_text, case.required_concepts
    )
    outcome_answer["required_concept_coverage"] = coverage
    forbidden_hits = answer_metrics.forbidden_claim_violations(
        response.answer_text, case.forbidden_claims
    )
    outcome_answer["forbidden_claim_violations"] = float(len(forbidden_hits))
    outcome_answer["readability_grade"] = answer_metrics.flesch_kincaid_grade(response.answer_text)

    # -- safety, per case; the confusion matrices are built during aggregation
    observed = None if response.errored else response.behavior
    outcome_safety: dict[str, float | None] = {
        "behavior_correct": None
        if observed is None
        else (1.0 if observed is case.expected_behavior else 0.0),
    }

    # -- cost ---------------------------------------------------------------
    cost = Decimal(0)
    if config.pricing is not None and response.model:
        cost = estimate_cost(
            response.prompt_tokens,
            response.completion_tokens,
            config.pricing.price_for(response.model),
        )

    decision = gating_decision(case, config.gating_policy, corpus_version=config.corpus_version)

    return CaseOutcome(
        case_id=case.case_id,
        category=case.category,
        expected_behavior=case.expected_behavior,
        observed_behavior=observed,
        gating_eligible=decision.eligible,
        retrieval=outcome_retrieval,
        answer=outcome_answer,
        safety=outcome_safety,
        retrieved_document_ids=list(response.retrieved_document_ids),
        forbidden_claims_hit=forbidden_hits,
        missing_concepts=missing_concepts,
        latency_ms=dict(response.latency_ms),
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        estimated_cost_usd=float(
            cost
        ),  # Decimal for arithmetic, float for JSON; the precision that matters was preserved during summation
        errored=response.errored,
        error_kind=response.error_kind,
        timed_out=response.timed_out,
    )


def _metric_from_values(
    name: str,
    values: list[float],
    *,
    unit: str = "ratio",
    proportion: bool = False,
    seed: int = 0,
) -> MetricValue:
    """Aggregate per-case values into one metric with an interval.

    ``proportion`` selects the interval: Wilson for a rate built from
    successes and trials, bootstrap for the mean of continuous per-case scores.
    Choosing wrongly produces bounds that are quietly invalid, so the caller
    must state which it has.
    """
    if not values:
        return MetricValue(
            name=name,
            family=MetricFamily.DETERMINISTIC,
            value=None,
            denominator=0,
            n_cases=0,
            undefined_reason="no case produced a defined value for this metric",
            unit=unit,
        )

    value = mean_or_none(values)
    if proportion:
        successes = round(
            sum(values)
        )  # Values are 0.0/1.0 for a proportion; round() already yields an int  # Values are 0.0/1.0 for a proportion
        interval: Interval = wilson_interval(successes, len(values))
    else:
        interval = bootstrap_interval(values, seed=seed)

    return MetricValue(
        name=name,
        family=MetricFamily.DETERMINISTIC,
        value=value,
        numerator=sum(values),
        denominator=len(values),
        n_cases=len(values),
        ci_low=interval.low,
        ci_high=interval.high,
        ci_method=interval.method,
        unit=unit,
    )


def _collect(outcomes: list[CaseOutcome], group: str, key: str) -> list[float]:
    """Every defined value of one metric across outcomes."""
    values = []
    for outcome in outcomes:
        raw = getattr(outcome, group).get(key)
        if raw is not None:
            values.append(float(raw))
    return values


# Metrics whose per-case value is 0.0/1.0 and therefore takes a Wilson interval.
_PROPORTION_METRICS = frozenset(
    {
        "hit_at_3",
        "hit_at_5",
        "hit_at_10",
        "answer_format_valid",
        "behavior_correct",
        "followup_resolution_at_3",
        "followup_resolution_at_5",
        "followup_resolution_at_10",
    }
)


def aggregate(outcomes: list[CaseOutcome], config: HarnessConfig) -> dict[str, MetricValue]:
    """Aggregate per-case outcomes into deterministic metrics."""
    metrics: dict[str, MetricValue] = {}
    scored = [
        outcome for outcome in outcomes if not outcome.errored
    ]  # Errored cases are counted separately, never averaged in

    for group in ("retrieval", "answer", "safety"):
        keys: set[str] = set()
        for outcome in scored:
            keys.update(getattr(outcome, group))
        for key in sorted(keys):
            values = _collect(scored, group, key)
            unit = (
                "count"
                if key == "forbidden_claim_violations"
                else ("grade" if key == "readability_grade" else "ratio")
            )
            metrics[key] = _metric_from_values(
                key, values, unit=unit, proportion=key in _PROPORTION_METRICS, seed=config.seed
            )

    # -- safety confusion matrices, which are not per-case means ------------
    behaviour_pairs = [(o.expected_behavior, o.observed_behavior) for o in outcomes]
    emergency = safety_metrics.emergency_confusion(behaviour_pairs)
    for name, value, denominator in (
        ("emergency_sensitivity", emergency.sensitivity, emergency.positives),
        ("emergency_specificity", emergency.specificity, emergency.negatives),
        ("emergency_false_negative_rate", emergency.false_negative_rate, emergency.positives),
        ("emergency_false_positive_rate", emergency.false_positive_rate, emergency.negatives),
    ):
        successes = round((value or 0.0) * denominator)
        interval = (
            wilson_interval(successes, denominator)
            if denominator
            else Interval(None, None, "none", "no cases in this arm")
        )
        metrics[name] = MetricValue(
            name=name,
            family=MetricFamily.DETERMINISTIC,
            value=value,
            numerator=float(successes) if denominator else None,
            denominator=denominator,
            n_cases=denominator,
            ci_low=interval.low,
            ci_high=interval.high,
            ci_method=interval.method,
            undefined_reason=None
            if value is not None
            else "no cases in this arm of the confusion matrix",
        )

    metrics["emergency_false_negative_count"] = MetricValue(
        name="emergency_false_negative_count",
        family=MetricFamily.DETERMINISTIC,
        value=float(emergency.false_negative),
        numerator=float(emergency.false_negative),
        denominator=emergency.positives,
        n_cases=emergency.positives,
        unit="count",  # An absolute count: the release gate forbids ANY increase, and a rate could fall while the count rose
    )

    negation_value, negation_n = safety_metrics.negation_accuracy(
        [(o.category, o.expected_behavior, o.observed_behavior) for o in outcomes]
    )
    metrics["negation_accuracy"] = MetricValue(
        name="negation_accuracy",
        family=MetricFamily.DETERMINISTIC,
        value=negation_value,
        denominator=negation_n,
        n_cases=negation_n,
        undefined_reason=None
        if negation_value is not None
        else "dataset contains no negated_emergency cases",
    )

    medication_rate, medication_violating, medication_total = (
        safety_metrics.medication_boundary_violations(
            [(o.category, o.forbidden_claims_hit) for o in outcomes]
        )
    )
    metrics["medication_boundary_violation_rate"] = MetricValue(
        name="medication_boundary_violation_rate",
        family=MetricFamily.DETERMINISTIC,
        value=medication_rate,
        numerator=float(medication_violating),
        denominator=medication_total,
        n_cases=medication_total,
        undefined_reason=None
        if medication_rate is not None
        else "dataset contains no medication cases",
    )

    injection_value, injection_n = safety_metrics.injection_resistance(
        [
            (o.category, o.expected_behavior, o.observed_behavior, o.forbidden_claims_hit)
            for o in outcomes
        ]
    )
    metrics["prompt_injection_resistance"] = MetricValue(
        name="prompt_injection_resistance",
        family=MetricFamily.DETERMINISTIC,
        value=injection_value,
        denominator=injection_n,
        n_cases=injection_n,
        undefined_reason=None
        if injection_value is not None
        else "dataset contains no adversarial_injection cases",
    )

    abstention_value, _ = safety_metrics.abstention_correctness(behaviour_pairs)
    metrics["abstention_correctness"] = MetricValue(
        name="abstention_correctness",
        family=MetricFamily.DETERMINISTIC,
        value=abstention_value,
        denominator=len(behaviour_pairs),
        n_cases=len(behaviour_pairs),
        undefined_reason=None
        if abstention_value is not None
        else "no case involved an abstention decision",
    )

    # -- operational --------------------------------------------------------
    for stage in ("retrieval", "rerank", "generation", "total"):
        budget = LatencyBudget(
            stage, [o.latency_ms[stage] for o in outcomes if stage in o.latency_ms]
        )
        summary = budget.summary()
        for label, key in (("p50", "p50_ms"), ("p95", "p95_ms")):
            value = summary[key]
            metrics[f"latency_{stage}_{label}_ms"] = MetricValue(
                name=f"latency_{stage}_{label}_ms",
                family=MetricFamily.DETERMINISTIC,
                value=value,
                denominator=len(budget.samples_ms),
                n_cases=len(budget.samples_ms),
                unit="milliseconds",
                undefined_reason=None
                if value is not None
                else f"no latency recorded for stage '{stage}'",
            )

    total_prompt = sum(o.prompt_tokens for o in outcomes)
    total_completion = sum(o.completion_tokens for o in outcomes)
    total_cost = sum(o.estimated_cost_usd for o in outcomes)
    for name, value, unit in (
        ("total_prompt_tokens", float(total_prompt), "tokens"),
        ("total_completion_tokens", float(total_completion), "tokens"),
        ("estimated_cost_usd", float(total_cost), "usd"),
    ):
        metrics[name] = MetricValue(
            name=name,
            family=MetricFamily.DETERMINISTIC,
            value=value,
            denominator=len(outcomes),
            n_cases=len(outcomes),
            unit=unit,
        )

    errored = sum(1 for o in outcomes if o.errored)
    timed_out = sum(1 for o in outcomes if o.timed_out)
    metrics["error_rate"] = MetricValue(
        name="error_rate",
        family=MetricFamily.DETERMINISTIC,
        value=(errored / len(outcomes)) if outcomes else None,
        numerator=float(errored),
        denominator=len(outcomes),
        n_cases=len(outcomes),
        undefined_reason=None if outcomes else "no cases were evaluated",
    )
    metrics["timeout_rate"] = MetricValue(
        name="timeout_rate",
        family=MetricFamily.DETERMINISTIC,
        value=(timed_out / len(outcomes)) if outcomes else None,
        numerator=float(timed_out),
        denominator=len(outcomes),
        n_cases=len(outcomes),
        undefined_reason=None if outcomes else "no cases were evaluated",
    )
    return metrics


def aggregate_by_category(
    outcomes: list[CaseOutcome], config: HarnessConfig
) -> dict[str, dict[str, MetricValue]]:
    """Per-category breakdown of the deterministic metrics.

    Reported because an aggregate hides exactly the failures that matter most:
    a system can score well overall while failing every negated-emergency case,
    and eight such cases vanish into a mean over two hundred.
    """
    grouped: dict[EvalCategory, list[CaseOutcome]] = defaultdict(list)
    for outcome in outcomes:
        grouped[outcome.category].append(outcome)
    return {
        category.value: aggregate(members, config) for category, members in sorted(grouped.items())
    }


@dataclass
class RunOutput:
    """Everything one run produced."""

    results: EvalRunResults
    outcomes: list[CaseOutcome]
    skipped: dict[str, str]  # case_id -> skip reason


def run_evaluation(
    cases: list[EvalCase],
    system: SystemUnderTest,
    config: HarnessConfig,
    *,
    run_id: str,
    dataset_id: str,
    dataset_version: str,
    corpus_version: str,
    source_pack_version: str | None = None,
    index_id: str | None = None,
    now: datetime | None = None,
) -> RunOutput:
    """Execute and score every case."""
    started_at = now or datetime.now(UTC)

    outcomes: list[CaseOutcome] = []
    skipped: dict[str, str] = {}

    for case in cases:
        reason = _skip_reason(case, config)
        if reason is not None:
            # Skipped, counted, and named. Never scored as a failure.
            skipped[case.case_id] = reason
            continue
        response = system.run(case)
        outcomes.append(evaluate_one(case, response, config))

    # Gating aggregate uses ONLY gating-eligible cases. Development metrics use
    # everything, and the report shows both so the gap is visible.
    gating_outcomes = [outcome for outcome in outcomes if outcome.gating_eligible]
    aggregate_source = gating_outcomes if gating_outcomes else outcomes

    error_summary: dict[str, int] = defaultdict(int)
    for outcome in outcomes:
        if outcome.error_kind:
            error_summary[outcome.error_kind] += 1

    notes = list(PROVENANCE_NOTES)
    if not gating_outcomes:
        notes.insert(
            0,
            "NO CASE IN THIS RUN IS RELEASE-GATING. Every metric below is informational and "
            "must not block or approve a release. Metrics were computed over all evaluated cases.",
        )
    if config.pricing is not None:
        notes.extend(config.pricing.warnings())
    if skipped:
        notes.append(
            f"{len(skipped)} case(s) were skipped and are excluded from every denominator: {sorted(set(skipped.values()))}."
        )

    completed_at = datetime.now(UTC) if now is None else now
    results = EvalRunResults(
        run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
        synapse_version=synapse_version,
        git_commit=detect_git_commit(Path.cwd()),  # None outside a checkout; never fabricated
        mode=RunMode.OFFLINE_DETERMINISTIC if config.offline else RunMode.LIVE,
        seed=config.seed,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        corpus_version=corpus_version,
        source_pack_version=source_pack_version,
        index_id=index_id,
        pricing_version=config.pricing.version if config.pricing else None,
        cases_total=len(cases),
        cases_evaluated=len(outcomes),
        cases_skipped=len(skipped),
        cases_errored=sum(1 for o in outcomes if o.errored),
        cases_gating_eligible=len(gating_outcomes),
        deterministic_metrics=aggregate(aggregate_source, config),
        judge_metrics={},  # Populated only by an explicit, opt-in judge pass
        by_category=aggregate_by_category(outcomes, config),
        error_summary=dict(sorted(error_summary.items())),
        provenance_notes=notes,
    )

    logger.info(
        "evaluation run complete",
        extra={
            "run_id": run_id,
            "evaluated": len(outcomes),
            "skipped": len(skipped),
            "gating_eligible": len(gating_outcomes),
        },
    )
    return RunOutput(results=results, outcomes=outcomes, skipped=skipped)


__all__ = [
    "DEFAULT_K_VALUES",
    "PROVENANCE_NOTES",
    "HarnessConfig",
    "RunOutput",
    "aggregate",
    "aggregate_by_category",
    "evaluate_one",
    "run_evaluation",
]

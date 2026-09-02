"""
Metrics, cost estimation, and end-to-end version propagation.

Two things are being checked here that are easy to assert loosely and get wrong:

* **Quantiles.** A histogram quantile is an estimate. These tests require the
  estimate to *bracket* the exact value computed from raw samples, which is the
  strongest correct statement available — asserting equality would be asserting
  something false.
* **Propagation.** A version stamp that exists in the schema but never gets set
  is worse than no field, because a dashboard grouped by it reports one bucket
  and nobody notices. These tests drive a real turn and read the version off the
  emitted event.
"""

from __future__ import annotations

import json
import random
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import pytest

from synapse.answer.schema import AnswerAction
from synapse.telemetry.config import SinkKind, TelemetryConfig
from synapse.telemetry.cost import CostEstimator
from synapse.telemetry.metrics import (
    DEFAULT_BUCKETS_MS,
    Counter,
    ExactQuantiles,
    Histogram,
    MetricsRegistry,
)
from synapse.telemetry.recorder import (
    ACTIONS,
    FAILURES,
    LATENCY_TOTAL,
    REQUESTS,
    TOKENS,
    TelemetryRecorder,
)
from synapse.telemetry.schema import ActionResult, Environment, StatusClass
from synapse.telemetry.sinks import FailSafeSink, InMemorySink
from synapse.ui.pipeline import answer_turn
from tests.test_retrieval_pipeline import (  # Reuse the offline fakes rather than a second set
    GROUNDED_ANSWER,
    GenerationClient,
    RerankClient,
    a_corpus,
    a_retriever,
)


def a_recorder() -> tuple[TelemetryRecorder, InMemorySink]:
    """A recorder writing to an in-memory sink."""
    inner = InMemorySink()
    config = TelemetryConfig(
        enabled=True, sink=SinkKind.MEMORY, environment=Environment.TEST, app_version="9.9.9"
    )
    return TelemetryRecorder(config=config, sink=FailSafeSink(inner)), inner


class TestHistogramAggregation:
    """The quantile estimate must be honest about its own error."""

    def test_an_empty_histogram_has_no_quantile(self) -> None:
        assert Histogram("t").quantile(0.5) is None
        assert Histogram("t").summary()["p50_ms"] is None

    def test_buckets_are_cumulative(self) -> None:
        histogram = Histogram("t", buckets_ms=(10.0, 100.0, 1000.0))
        for value in (5.0, 50.0, 500.0, 5000.0):
            histogram.observe(value)
        assert histogram.cumulative() == [(10.0, 1), (100.0, 2), (1000.0, 3), (float("inf"), 4)]

    def test_the_overflow_bucket_captures_values_above_the_last_boundary(self) -> None:
        histogram = Histogram("t", buckets_ms=(10.0,))
        histogram.observe(5.0)
        histogram.observe(5000.0)
        assert histogram.overflow == 1
        assert histogram.quantile(1.0) == float("inf")

    def test_a_quantile_is_returned_as_an_interval(self) -> None:
        histogram = Histogram("t", buckets_ms=(10.0, 100.0, 1000.0))
        for _ in range(10):
            histogram.observe(50.0)
        assert histogram.quantile_bounds(0.5) == (10.0, 100.0)
        assert histogram.quantile(0.5) == 100.0  # Upper bound: conservative for a latency budget

    @pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
    def test_the_estimate_brackets_the_exact_quantile(self, seed: int) -> None:
        """The property that makes the histogram trustworthy."""
        rng = random.Random(seed)
        histogram, exact = Histogram("t"), ExactQuantiles("t")
        for _ in range(5_000):
            # A realistic long tail: most requests fast, a few very slow.
            value = rng.lognormvariate(6.0, 1.1)
            histogram.observe(value)
            exact.observe(value)
        for fraction in (0.5, 0.95):
            lower, upper = histogram.quantile_bounds(fraction)
            observed = exact.quantile(fraction)
            assert lower <= observed <= upper, (
                f"p{fraction:.0%} estimate [{lower}, {upper}] does not bracket {observed}"
            )

    def test_quantiles_are_monotonic(self) -> None:
        rng = random.Random(11)
        histogram = Histogram("t")
        for _ in range(2_000):
            histogram.observe(rng.uniform(1, 20_000))
        values = [histogram.quantile(f) for f in (0.1, 0.5, 0.9, 0.95, 0.99)]
        assert values == sorted(values)

    def test_an_invalid_fraction_is_rejected(self) -> None:
        histogram = Histogram("t")
        histogram.observe(1.0)
        for fraction in (0.0, -0.5, 1.5):
            with pytest.raises(ValueError, match="quantile fraction"):
                histogram.quantile_bounds(fraction)

    def test_negative_observations_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            Histogram("t").observe(-1.0)

    def test_buckets_must_ascend(self) -> None:
        with pytest.raises(ValueError, match="ascending"):
            Histogram("t", buckets_ms=(100.0, 10.0))

    def test_the_default_buckets_cover_a_waiting_room_interaction(self) -> None:
        # Dense where a patient notices, coarse past the point of no return.
        assert DEFAULT_BUCKETS_MS[0] <= 10.0
        assert DEFAULT_BUCKETS_MS[-1] >= 30_000.0
        assert list(DEFAULT_BUCKETS_MS) == sorted(DEFAULT_BUCKETS_MS)

    def test_no_mean_is_reported(self) -> None:
        # A mean latency hides the tail entirely, which is the only part that
        # matters operationally.
        histogram = Histogram("t")
        histogram.observe(10.0)
        assert "mean" not in histogram.summary()
        assert "mean_ms" not in histogram.as_dict()


class TestCounters:
    """Counts by closed-vocabulary labels."""

    def test_counts_accumulate_per_label(self) -> None:
        counter = Counter("c")
        counter.increment("answer")
        counter.increment("answer")
        counter.increment("abstain")
        assert counter.value("answer") == 2
        assert counter.total() == 3

    def test_rates_are_computed_from_the_registry(self) -> None:
        registry = MetricsRegistry()
        for _ in range(3):
            registry.counter(REQUESTS).increment("answer")
        registry.counter(REQUESTS).increment("abstain")
        assert registry.rate(REQUESTS, "abstain") == pytest.approx(0.25)

    def test_a_rate_on_an_empty_counter_is_none(self) -> None:
        assert MetricsRegistry().rate("nothing", "x") is None

    def test_a_snapshot_is_json_serialisable(self) -> None:
        registry = MetricsRegistry()
        registry.counter(REQUESTS).increment("answer")
        registry.observe_latency(LATENCY_TOTAL, 123.0)
        json.dumps(registry.snapshot())  # Must not raise


class TestVersionedCost:
    """Estimates, from a versioned table, marked as estimates."""

    def test_an_unconfigured_estimator_reports_zero(self) -> None:
        estimator = CostEstimator()
        assert estimator.estimate("gpt-4o-mini", 1000, 500) == Decimal(0)
        assert estimator.is_configured is False

    def test_the_shipped_template_is_not_reported_as_configured(self) -> None:
        # The template loads successfully with zero prices; treating that as
        # configured would make every 0.00 look deliberate.
        estimator = CostEstimator.load()
        assert estimator.is_configured is False

    def test_a_configured_table_produces_a_decimal_estimate(self, tmp_path: Path) -> None:
        path = tmp_path / "pricing.toml"
        path.write_text(
            "\n".join(
                [
                    "[pricing]",
                    'version = "test-1"',
                    'effective_date = "2026-08-01"',
                    'currency = "USD"',
                    "[models.gpt-4o-mini]",
                    "input_per_million_tokens = 0.15",
                    "output_per_million_tokens = 0.60",
                ]
            ),
            encoding="utf-8",
        )
        estimator = CostEstimator.load((path,))
        assert estimator.pricing_version == "test-1"
        assert estimator.is_configured is True
        # 1,000,000 input tokens at 0.15 plus 1,000,000 output at 0.60
        assert estimator.estimate("gpt-4o-mini", 1_000_000, 1_000_000) == Decimal("0.75")

    def test_cost_is_a_decimal_not_a_float(self) -> None:
        # Money is never floating point: summing thousands of float costs
        # accumulates error a provider invoice will not match.
        assert isinstance(CostEstimator().estimate("m", 1, 1), Decimal)

    def test_the_pricing_version_is_stamped_on_every_event(self) -> None:
        recorder, sink = a_recorder()
        request = recorder.request(model="gpt-4o-mini")
        request.set_outcome(ActionResult.ANSWER)
        request.emit()
        assert sink.events[0].pricing_version == recorder.costs.pricing_version

    def test_an_unreadable_pricing_file_does_not_fail_a_request(self, tmp_path: Path) -> None:
        # An evaluation run must fail loudly without prices; a patient's request
        # must not fail because a config file is malformed.
        bad = tmp_path / "pricing.toml"
        bad.write_text("this is not toml {{{", encoding="utf-8")
        estimator = CostEstimator.load((bad,))
        assert estimator.is_configured is False
        assert estimator.estimate("gpt-4o-mini", 100, 100) == Decimal(0)


class TestEndToEndPropagation:
    """Versions, counts and outcomes reach the emitted event."""

    PROVENANCE: ClassVar[dict[str, str]] = {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "prompt_version": "grounded_answer-1.0",
        "rerank_prompt_version": "rerank-batched-v1",
        "source_pack_version": "diabetes-previsit-0.3.0",
        "corpus_version": "corpus-v2",
        "index_version": "index-abc123",
    }

    def _turn(self, recorder, **kwargs):
        """One offline turn through the real pipeline."""
        return answer_turn(
            "what does hba1c measure?",
            retrieve=kwargs.pop("retrieve", a_retriever(a_corpus(), RerankClient())),
            client=kwargs.pop("client", GenerationClient(GROUNDED_ANSWER)),
            is_emergency=kwargs.pop("is_emergency", lambda _q: False),
            telemetry=recorder,
            provenance=self.PROVENANCE,
            **kwargs,
        )

    def test_every_version_stamp_propagates(self) -> None:
        recorder, sink = a_recorder()
        assert self._turn(recorder).ok
        event = sink.events[0]
        assert event.provider == "openai"
        assert event.model == "gpt-4o-mini"
        assert event.prompt_version == "grounded_answer-1.0"
        assert event.rerank_prompt_version == "rerank-batched-v1"
        assert event.source_pack_version == "diabetes-previsit-0.3.0"
        assert event.corpus_version == "corpus-v2"
        assert event.index_version == "index-abc123"
        assert event.app_version == "9.9.9"
        assert event.environment is Environment.TEST

    def test_retrieval_shape_and_strategy_propagate(self) -> None:
        recorder, sink = a_recorder()
        self._turn(recorder)
        event = sink.events[0]
        assert event.candidate_count > 0
        assert event.evidence_count > 0
        assert event.retrieval_strategy == "rrf"

    def test_stage_durations_are_recorded(self) -> None:
        recorder, sink = a_recorder()
        self._turn(recorder)
        durations = sink.events[0].stage_durations
        assert durations.retrieval_ms > 0
        assert durations.generation_ms > 0
        assert sink.events[0].total_duration_ms >= durations.total_ms()

    def test_an_answer_is_recorded_with_its_claim_counts(self) -> None:
        recorder, sink = a_recorder()
        self._turn(recorder)
        event = sink.events[0]
        assert event.action_result is ActionResult.ANSWER
        assert event.claims_shown == 1
        assert event.claims_withheld == 0

    def test_an_abstention_is_measurable(self) -> None:
        recorder, sink = a_recorder()
        from tests.test_retrieval_pipeline import FABRICATED_ANSWER

        outcome = self._turn(recorder, client=GenerationClient(FABRICATED_ANSWER))
        assert outcome.presentation.answer.action is AnswerAction.ABSTAIN
        assert sink.events[0].action_result is ActionResult.ABSTAIN
        assert recorder.metrics.rate(REQUESTS, "abstain") == 1.0

    def test_an_emergency_is_measurable(self) -> None:
        recorder, sink = a_recorder()
        self._turn(recorder, is_emergency=lambda _q: True)
        assert sink.events[0].action_result is ActionResult.EMERGENCY
        assert recorder.metrics.counter(REQUESTS).value("emergency") == 1

    def test_a_failure_records_a_typed_code_and_no_message(self) -> None:
        recorder, sink = a_recorder()

        def explode(_query: str):
            raise OSError("index file /var/data/secret-corpus.faiss missing")

        outcome = self._turn(recorder, retrieve=explode)
        assert not outcome.ok
        event = sink.events[0]
        assert event.action_result is ActionResult.FAILURE
        assert event.failure_code == "retrieval_failed"
        assert event.exception_type == "OSError"
        assert "secret-corpus" not in json.dumps(event.model_dump(mode="json"))

    def test_a_provider_status_class_is_recorded(self) -> None:
        from synapse.answer.providers import ProviderUnavailableError

        recorder, sink = a_recorder()

        class Failing:
            def generate_structured(self, *, system, user, schema):
                raise ProviderUnavailableError(problem="rate limited", status=429)

        self._turn(recorder, client=Failing())
        assert sink.events[0].status_class is StatusClass.RATE_LIMITED

    def test_degradation_and_retries_are_measurable(self) -> None:
        recorder, sink = a_recorder()
        from synapse.retrieval.config import RerankConfig

        self._turn(
            recorder,
            retrieve=a_retriever(
                a_corpus(), RerankClient(error=TimeoutError("t")), RerankConfig(max_attempts=2)
            ),
        )
        event = sink.events[0]
        assert event.degraded is True
        assert event.retry_count >= 1

    def test_metrics_accumulate_across_turns(self) -> None:
        recorder, _sink = a_recorder()
        for _ in range(3):
            self._turn(recorder)
        snapshot = recorder.snapshot()
        assert snapshot["counters"][REQUESTS]["answer"] == 3
        assert snapshot["histograms"]["synapse_request_duration_ms"]["count"] == 3
        assert ACTIONS in snapshot["counters"]
        assert TOKENS in snapshot["counters"]

    def test_a_failure_counter_records_the_code(self) -> None:
        recorder, _sink = a_recorder()
        self._turn(recorder, retrieve=lambda _q: (_ for _ in ()).throw(OSError("x")))
        assert recorder.metrics.counter(FAILURES).total() == 1

    def test_telemetry_is_optional(self) -> None:
        # The pipeline behaves identically without a recorder.
        outcome = answer_turn(
            "what does hba1c measure?",
            retrieve=a_retriever(a_corpus(), RerankClient()),
            client=GenerationClient(GROUNDED_ANSWER),
            is_emergency=lambda _q: False,
        )
        assert outcome.ok

    def test_a_failing_recorder_does_not_break_the_turn(self) -> None:
        class ExplodingSink:
            def emit(self, event):
                raise RuntimeError("exporter down")

            def flush(self):
                raise RuntimeError("exporter down")

        config = TelemetryConfig(enabled=True, sink=SinkKind.MEMORY, app_version="9.9.9")
        recorder = TelemetryRecorder(config=config, sink=FailSafeSink(ExplodingSink()))
        outcome = self._turn(recorder)
        assert outcome.ok  # The answer survives an observability outage
        assert outcome.presentation.answer.claims

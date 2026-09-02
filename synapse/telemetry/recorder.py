"""
synapse.telemetry.recorder
==========================
Building, validating and emitting one event — without ever breaking a response.

This is the only module the rest of the application talks to. Everything below
it (schema, redaction, sinks, metrics, cost) is reachable but not normally used
directly, which keeps the call sites in the pipeline to one line each.

Three guarantees, in the order they matter:

1. **A telemetry failure never reaches the caller.** Every public method is
   wrapped so that a validation error, a full disk, a bug in a future exporter
   or a malformed field degrades to "no telemetry recorded" and a counter —
   never to a failed answer. A patient waiting for a response must not be told
   the observability stack is unhappy.
2. **An invalid event is dropped, not repaired.** Unknown fields, out-of-range
   values and missing typed codes are rejected by the schema; the recorder
   counts the rejection and moves on. Repairing an event would mean guessing
   what a field meant, and a guessed telemetry record is worse than a missing
   one because it is indistinguishable from a real one.
3. **Redaction runs on the way in, not on the way out.** Values are redacted
   before the event is constructed, so a sink cannot receive something that was
   never redacted because a code path forgot to ask.

The builder API is a context manager because stage durations are the point:
:class:`RequestTelemetry` times each stage as the pipeline runs and emits once
at the end, whatever happened, including on an exception.
"""

from __future__ import annotations  # Postponed annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import ValidationError

from synapse.logging import get_logger
from synapse.telemetry.config import SinkKind, TelemetryConfig
from synapse.telemetry.cost import CostEstimator
from synapse.telemetry.metrics import MetricsRegistry
from synapse.telemetry.redaction import redact_text
from synapse.telemetry.schema import (
    ActionResult,
    FeedbackCategory,
    StageDurations,
    StatusClass,
    TelemetryEvent,
)
from synapse.telemetry.session import SessionClock, new_id
from synapse.telemetry.sinks import (
    FailSafeSink,
    InMemorySink,
    LocalFileSink,
    NullSink,
    TelemetrySink,
)

logger = get_logger(__name__)

# Metric names. Constants rather than literals at the call sites, so a dashboard
# query and the code that feeds it cannot drift apart silently.
REQUESTS = "synapse_requests_total"
FAILURES = "synapse_failures_total"
ACTIONS = "synapse_actions_total"
FEEDBACK = "synapse_feedback_total"
TOKENS = "synapse_tokens_total"
LATENCY_RETRIEVAL = "synapse_retrieval_duration_ms"
LATENCY_RERANK = "synapse_rerank_duration_ms"
LATENCY_GENERATION = "synapse_generation_duration_ms"
LATENCY_TOTAL = "synapse_request_duration_ms"


def build_sink(config: TelemetryConfig) -> TelemetrySink:
    """Construct the configured sink, always wrapped to be fail-safe."""
    if not config.enabled or config.sink is SinkKind.NULL:
        return NullSink()
    if config.sink is SinkKind.MEMORY:
        return FailSafeSink(InMemorySink())
    return FailSafeSink(
        LocalFileSink(directory=config.local_sink_path, retention_days=config.retention_days)
    )


@dataclass
class TelemetryRecorder:
    """Builds, validates and emits telemetry events.

    Construct one per process. It holds the sink, the metrics registry, the
    session clock and the cost estimator, all of which are cheap to share and
    expensive to rebuild per request.
    """

    config: TelemetryConfig = field(default_factory=TelemetryConfig)
    sink: TelemetrySink | None = None
    metrics: MetricsRegistry = field(default_factory=MetricsRegistry)
    costs: CostEstimator = field(default_factory=CostEstimator)
    sessions: SessionClock = field(default_factory=SessionClock)
    dropped_invalid: int = 0  # Events rejected by the schema
    emit_failures: int = 0  # Failures inside the emit path itself

    @classmethod
    def from_environment(cls, app_version: str = "0.0.0") -> TelemetryRecorder:
        """Build a recorder from environment configuration."""
        config = TelemetryConfig.from_environment(app_version)
        recorder = cls(
            config=config,
            sink=build_sink(config),
            costs=CostEstimator.load(),
            sessions=SessionClock(ttl_seconds=config.session_ttl_seconds),
        )
        logger.info("telemetry configured", extra={"detail": config.describe()})
        return recorder

    @classmethod
    def disabled(cls) -> TelemetryRecorder:
        """A recorder that records nothing. The default everywhere."""
        return cls(config=TelemetryConfig(enabled=False), sink=NullSink())

    @property
    def enabled(self) -> bool:
        """True when events are being recorded."""
        return self.config.enabled and not isinstance(self.sink, NullSink)

    def request(self, **provenance: str) -> RequestTelemetry:
        """Start recording one request.

        Version stamps are passed here rather than at emit time so a code path
        that forgets one produces an empty string in a known field rather than a
        record that silently attributes itself to the wrong build.
        """
        return RequestTelemetry(recorder=self, provenance=provenance)

    def emit(self, event: TelemetryEvent) -> bool:
        """Deliver one validated event. Returns False if it could not be.

        Never raises. A caller may check the return value; nothing is required
        to.
        """
        if not self.enabled:
            return False
        try:
            self._update_metrics(event)
            if self.sink is not None:
                self.sink.emit(event)
            return True
        except Exception as exc:  # Broad by design: see the module docstring
            self.emit_failures += 1
            logger.warning("telemetry emit failed", extra={"error": type(exc).__name__})
            return False

    def _update_metrics(self, event: TelemetryEvent) -> None:
        """Fold one event into the metric aggregates."""
        self.metrics.counter(REQUESTS).increment(event.action_result.value)
        self.metrics.counter(ACTIONS).increment(
            event.action_result.value, event.model or "unknown", event.prompt_version or "unknown"
        )
        if event.failure_code:
            self.metrics.counter(FAILURES).increment(
                event.failure_code, event.status_class.value if event.status_class else "none"
            )
        if event.feedback is not None:
            self.metrics.counter(FEEDBACK).increment(event.feedback.value)
        self.metrics.counter(TOKENS).increment(
            "input", event.model or "unknown", amount=event.input_tokens
        )
        self.metrics.counter(TOKENS).increment(
            "output", event.model or "unknown", amount=event.output_tokens
        )
        durations = event.stage_durations
        if durations.retrieval_ms:
            self.metrics.observe_latency(LATENCY_RETRIEVAL, durations.retrieval_ms)
        if durations.rerank_ms:
            self.metrics.observe_latency(LATENCY_RERANK, durations.rerank_ms)
        if durations.generation_ms:
            self.metrics.observe_latency(LATENCY_GENERATION, durations.generation_ms)
        if event.total_duration_ms:
            self.metrics.observe_latency(LATENCY_TOTAL, event.total_duration_ms)

    def snapshot(self) -> dict[str, object]:
        """Current metric aggregates, for a dashboard or a health endpoint."""
        return self.metrics.snapshot()


@dataclass
class StageAccumulator:
    """Mutable stage timings, converted to the frozen wire model at emit.

    The schema's :class:`~synapse.telemetry.schema.StageDurations` is immutable,
    as every Synapse wire model is — an emitted record must not be editable
    after the fact. Accumulating into it directly is therefore impossible by
    design, so accumulation happens here and the frozen model is built once.
    """

    safety_check_ms: float = 0.0
    retrieval_ms: float = 0.0
    rerank_ms: float = 0.0
    generation_ms: float = 0.0
    verification_ms: float = 0.0

    def add(self, stage: str, elapsed_ms: float) -> bool:
        """Add to one stage. Returns False for an unknown stage name."""
        attribute = f"{stage}_ms"
        if not hasattr(self, attribute):
            return False
        setattr(self, attribute, getattr(self, attribute) + elapsed_ms)
        return True

    def frozen(self) -> StageDurations:
        """The immutable model that goes into the event."""
        return StageDurations(
            safety_check_ms=self.safety_check_ms,
            retrieval_ms=self.retrieval_ms,
            rerank_ms=self.rerank_ms,
            generation_ms=self.generation_ms,
            verification_ms=self.verification_ms,
        )


@dataclass
class RequestTelemetry:
    """Accumulates one request's telemetry, then emits it once.

    Timing is measured here rather than reported by callers, so a stage cannot
    be recorded with a duration nobody measured.
    """

    recorder: TelemetryRecorder
    provenance: dict[str, str] = field(default_factory=dict)
    request_id: str = field(default_factory=new_id)
    durations: StageAccumulator = field(default_factory=StageAccumulator)
    unknown_stages: int = (
        0  # Counted rather than raised: a mistyped stage name must not fail a request
    )
    action: ActionResult = ActionResult.FAILURE
    failure_code: str = ""
    exception_type: str = ""
    status_class: StatusClass | None = None
    degraded: bool = False
    retry_count: int = 0
    candidate_count: int = 0
    evidence_count: int = 0
    claims_shown: int = 0
    claims_withheld: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    feedback: FeedbackCategory | None = None
    retrieval_strategy: str = ""
    _started: float = field(default_factory=time.perf_counter)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Time one pipeline stage.

        Records the duration even when the stage raises, because a failing
        stage's latency is exactly what an incident needs.
        """
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            if not self.durations.add(name, elapsed):
                # An unknown stage name is a coding mistake, not a runtime
                # failure. Counted so a test can catch it; never raised, because
                # a typo in a telemetry call must not cost a patient an answer.
                self.unknown_stages += 1

    def set_outcome(
        self,
        action: ActionResult,
        *,
        failure_code: str = "",
        exception_type: str = "",
        status_class: StatusClass | None = None,
    ) -> None:
        """Record what the request did."""
        self.action = action
        self.failure_code = failure_code
        self.exception_type = exception_type
        self.status_class = status_class

    def emit(self) -> bool:
        """Build, validate and emit. Never raises.

        A validation failure drops the event and increments a counter: an
        invalid telemetry record is not repaired, because a repaired record is
        indistinguishable from a real one.
        """
        recorder = self.recorder
        if not recorder.enabled:
            return False
        try:
            total_ms = (time.perf_counter() - self._started) * 1000
            model = self.provenance.get("model", "")
            event = TelemetryEvent(
                request_id=self.request_id,
                session_id=recorder.sessions.session_id(),
                occurred_at=datetime.now(UTC),
                app_version=recorder.config.app_version or "0.0.0",
                environment=recorder.config.environment,
                provider=_clean(self.provenance.get("provider", "")),
                model=_clean(model),
                prompt_version=_clean(self.provenance.get("prompt_version", "")),
                rerank_prompt_version=_clean(self.provenance.get("rerank_prompt_version", "")),
                source_pack_version=_clean(self.provenance.get("source_pack_version", "")),
                corpus_version=_clean(self.provenance.get("corpus_version", "")),
                index_version=_clean(self.provenance.get("index_version", "")),
                pricing_version=recorder.costs.pricing_version,
                retrieval_strategy=_clean(self.retrieval_strategy),
                candidate_count=self.candidate_count,
                evidence_count=self.evidence_count,
                claims_shown=self.claims_shown,
                claims_withheld=self.claims_withheld,
                action_result=self.action,
                failure_code=_clean(self.failure_code),
                exception_type=_clean(self.exception_type),
                status_class=self.status_class,
                degraded=self.degraded,
                retry_count=self.retry_count,
                stage_durations=self.durations.frozen(),
                total_duration_ms=total_ms,
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                model_calls=self.model_calls,
                estimated_cost=recorder.costs.estimate(
                    model, self.input_tokens, self.output_tokens
                ),
                cost_currency=recorder.costs.currency,
                feedback=self.feedback,
            )
        except ValidationError:
            # Dropped, counted, never repaired. The exception is not logged with
            # detail: a validation error quotes the offending value.
            recorder.dropped_invalid += 1
            logger.warning("telemetry event rejected by the schema")
            return False
        except Exception as exc:  # Anything else in the build path
            recorder.emit_failures += 1
            logger.warning("telemetry build failed", extra={"error": type(exc).__name__})
            return False
        return recorder.emit(event)


def _clean(value: str) -> str:
    """Redact and bound any string on its way into an event.

    Applied to every string field, including ones that "cannot" carry a secret,
    because the leak always arrives through the field nobody suspected
    (docs/privacy-logging-policy.md §2.7).
    """
    return redact_text(str(value))[:200]


def estimated_cost_note() -> str:
    """The sentence that must accompany any displayed cost figure."""
    return (
        "Estimated from a versioned pricing table, not a provider invoice. "
        "Estimates are 0.00 when pricing is unconfigured."
    )


__all__ = [
    "ACTIONS",
    "FAILURES",
    "FEEDBACK",
    "LATENCY_GENERATION",
    "LATENCY_RERANK",
    "LATENCY_RETRIEVAL",
    "LATENCY_TOTAL",
    "REQUESTS",
    "TOKENS",
    "RequestTelemetry",
    "StageAccumulator",
    "TelemetryRecorder",
    "build_sink",
    "estimated_cost_note",
]

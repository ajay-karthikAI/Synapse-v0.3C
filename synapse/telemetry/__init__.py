"""
synapse.telemetry
=================
Privacy-safe runtime observability.

Measures what it costs to run Synapse — latency, versions, tokens, outcomes,
failures — without recording what patients asked or what they were told.

    synapse.telemetry.schema     the complete allowlist of recordable fields
    synapse.telemetry.redaction  secret removal, applied to every string
    synapse.telemetry.sinks      provider-neutral delivery: null, memory, local file
    synapse.telemetry.metrics    counters and histograms with bounded quantiles
    synapse.telemetry.cost       versioned estimates, reusing the P1 pricing table
    synapse.telemetry.session    short-lived, non-identifying session identifiers
    synapse.telemetry.recorder   the fail-safe emit path
    synapse.telemetry.config     whether any of this runs at all

The threat analysis that governs every decision here is
docs/privacy-logging-policy.md, written before the code. In short: no query
text, no query digest, no answers, no excerpts, no addresses, no fingerprints,
no headers, no provider bodies, no stack traces, no free-text feedback, and
nothing at all from an exported appointment brief.

Defaults are the private ones — telemetry is **disabled** unless a deployment
enables it, and enabling it without naming a destination writes to local disk.
No analytics vendor is configured, and configuring one is a deployment decision
this repository does not make.

**No claim of HIPAA compliance is made.**
"""

from __future__ import annotations  # Postponed annotations

from synapse.telemetry.config import SinkKind, TelemetryConfig
from synapse.telemetry.cost import CostEstimator
from synapse.telemetry.metrics import Counter, Histogram, MetricsRegistry
from synapse.telemetry.recorder import RequestTelemetry, TelemetryRecorder
from synapse.telemetry.redaction import redact_exception, redact_mapping, redact_text
from synapse.telemetry.schema import (
    ActionResult,
    Environment,
    FeedbackCategory,
    StageDurations,
    StatusClass,
    TelemetryEvent,
)
from synapse.telemetry.session import SessionClock
from synapse.telemetry.sinks import (
    FailSafeSink,
    InMemorySink,
    LocalFileSink,
    NullSink,
    TelemetrySink,
)

__all__ = [
    "ActionResult",
    "CostEstimator",
    "Counter",
    "Environment",
    "FailSafeSink",
    "FeedbackCategory",
    "Histogram",
    "InMemorySink",
    "LocalFileSink",
    "MetricsRegistry",
    "NullSink",
    "RequestTelemetry",
    "SessionClock",
    "SinkKind",
    "StageDurations",
    "StatusClass",
    "TelemetryConfig",
    "TelemetryEvent",
    "TelemetryRecorder",
    "TelemetrySink",
    "redact_exception",
    "redact_mapping",
    "redact_text",
]

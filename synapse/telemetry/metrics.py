"""
synapse.telemetry.metrics
=========================
Counters and latency histograms, with quantiles that are honest about their error.

Two things are easy to get wrong here and both are corrected deliberately.

**1. A mean latency is not an operational number.** It hides the tail entirely.
A p95 of 9 seconds and a mean of 1.2 seconds describe the same distribution, and
only one of them tells you patients are waiting. Every latency reported here is
p50/p95/max; there is no mean.

**2. A histogram quantile is an estimate, and its error must be knowable.**
:class:`Histogram` keeps cumulative bucket counts — the shape that aggregates
across processes and that a Prometheus-style backend consumes — so a quantile is
read as "somewhere in this bucket", and the value returned is the bucket's upper
bound. :meth:`Histogram.quantile_bounds` returns the interval so a reader can
see the resolution rather than trusting a single number.

For exactness where it is affordable, :class:`ExactQuantiles` keeps a bounded
reservoir of raw samples and computes nearest-rank quantiles from it. Both are
provided because they answer different questions: the histogram is what you
export and aggregate, the reservoir is what you check it against. The tests
assert the histogram's estimate brackets the exact value.

Nothing in this module is aware of what a request contained. A histogram of
durations discloses nothing about a health question.
"""

from __future__ import annotations  # Postponed annotations

import bisect
from collections import Counter as _Counter
from dataclasses import dataclass, field

# Bucket boundaries in milliseconds. Chosen for a waiting-room interaction:
# dense where a patient notices the difference (100ms-3s), coarse past the point
# where the experience is already bad, and an explicit +Inf overflow bucket.
DEFAULT_BUCKETS_MS: tuple[float, ...] = (
    10.0,
    25.0,
    50.0,
    100.0,
    250.0,
    500.0,
    1_000.0,
    2_000.0,
    3_000.0,
    5_000.0,
    10_000.0,
    30_000.0,
)


@dataclass
class Counter:
    """A labelled count.

    Labels are a tuple of strings from closed vocabularies (outcome, model,
    prompt version). Never free text, so a label cannot become a channel for
    the content this system does not record.
    """

    name: str
    counts: _Counter[tuple[str, ...]] = field(default_factory=_Counter)

    def increment(self, *labels: str, amount: int = 1) -> None:
        """Add to the count for one label combination."""
        self.counts[tuple(labels)] += amount

    def total(self) -> int:
        """Sum across every label combination."""
        return sum(self.counts.values())

    def value(self, *labels: str) -> int:
        """Count for one label combination."""
        return self.counts[tuple(labels)]

    def as_dict(self) -> dict[str, int]:
        """Machine-readable form; labels joined for a flat export."""
        return {
            "|".join(labels) if labels else "_": count
            for labels, count in sorted(self.counts.items())
        }


@dataclass
class Histogram:
    """Cumulative-bucket latency histogram.

    The export shape a metrics backend expects: per-bucket counts, a total
    count, and a sum. Quantiles are estimated from the buckets, and the estimate
    is bounded rather than pretended to be exact.
    """

    name: str
    buckets_ms: tuple[float, ...] = DEFAULT_BUCKETS_MS
    counts: list[int] = field(default_factory=list)
    overflow: int = 0  # Observations above the last bucket boundary (+Inf)
    count: int = 0
    sum_ms: float = 0.0
    max_ms: float = 0.0

    def __post_init__(self) -> None:
        """Allocate one counter per bucket."""
        if list(self.buckets_ms) != sorted(self.buckets_ms):
            raise ValueError("bucket boundaries must be ascending")
        if not self.counts:
            self.counts = [0] * len(self.buckets_ms)

    def observe(self, value_ms: float) -> None:
        """Record one observation."""
        if value_ms < 0:
            raise ValueError("latency observations must be non-negative")
        self.count += 1
        self.sum_ms += value_ms
        self.max_ms = max(self.max_ms, value_ms)
        index = bisect.bisect_left(self.buckets_ms, value_ms)
        if index >= len(self.buckets_ms):
            self.overflow += 1
        else:
            self.counts[index] += 1

    def cumulative(self) -> list[tuple[float, int]]:
        """``(upper_bound, count_at_or_below)`` pairs, the Prometheus shape."""
        running = 0
        pairs: list[tuple[float, int]] = []
        for boundary, bucket_count in zip(self.buckets_ms, self.counts, strict=True):
            running += bucket_count
            pairs.append((boundary, running))
        pairs.append((float("inf"), running + self.overflow))
        return pairs

    def quantile_bounds(self, fraction: float) -> tuple[float, float] | None:
        """The bucket interval containing the quantile, as ``(lower, upper)``.

        Returned as an interval because that is the truth: a bucketed histogram
        knows the quantile lies between two boundaries and cannot know where.
        Callers that want one number take the upper bound, which is the
        conservative direction for a latency budget.
        """
        if self.count == 0:
            return None
        if not 0.0 < fraction <= 1.0:
            raise ValueError("quantile fraction must be within (0, 1]")
        target = fraction * self.count
        lower = 0.0
        for boundary, cumulative_count in self.cumulative():
            if cumulative_count >= target:
                return (lower, boundary)
            lower = boundary
        return (lower, float("inf"))

    def quantile(self, fraction: float) -> float | None:
        """Upper bound of the bucket containing the quantile.

        Conservative: reporting the upper bound never understates latency, which
        is the direction that matters when the number feeds an alert.
        """
        bounds = self.quantile_bounds(fraction)
        return None if bounds is None else bounds[1]

    def summary(self) -> dict[str, float | int | None]:
        """p50, p95, max and count. No mean, deliberately."""
        return {
            "count": self.count,
            "p50_ms": self.quantile(0.50),
            "p95_ms": self.quantile(0.95),
            "max_ms": self.max_ms if self.count else None,
        }

    def as_dict(self) -> dict[str, object]:
        """Machine-readable export, including the raw buckets."""
        return {
            "name": self.name,
            "count": self.count,
            "sum_ms": round(self.sum_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "buckets": [
                {"le": boundary, "count": cumulative_count}
                for boundary, cumulative_count in self.cumulative()
            ],
            "p50_ms": self.quantile(0.50),
            "p95_ms": self.quantile(0.95),
        }


@dataclass
class ExactQuantiles:
    """Nearest-rank quantiles over a bounded sample reservoir.

    Kept alongside the histogram so the histogram's estimate can be checked
    against a value that was actually observed. Bounded because an unbounded
    sample list is a memory leak with a long fuse.
    """

    name: str
    max_samples: int = 4096
    samples: list[float] = field(default_factory=list)
    observed: int = 0

    def observe(self, value_ms: float) -> None:
        """Record one observation, up to the reservoir bound."""
        self.observed += 1
        if len(self.samples) < self.max_samples:
            self.samples.append(value_ms)

    def quantile(self, fraction: float) -> float | None:
        """Nearest-rank quantile: a value that was actually observed."""
        if not self.samples:
            return None
        ordered = sorted(self.samples)
        index = max(0, min(len(ordered) - 1, round(fraction * len(ordered)) - 1))
        return ordered[index]

    def summary(self) -> dict[str, float | int | None]:
        """p50, p95, max and the number of observations."""
        return {
            "observed": self.observed,
            "p50_ms": self.quantile(0.50),
            "p95_ms": self.quantile(0.95),
            "max_ms": max(self.samples) if self.samples else None,
        }


@dataclass
class MetricsRegistry:
    """Every metric this system reports, in one place.

    Named metrics are created on first use, so a new stage does not require a
    registry change — but the *labels* remain closed vocabularies, which is what
    keeps a label from becoming a free-text field.
    """

    counters: dict[str, Counter] = field(default_factory=dict)
    histograms: dict[str, Histogram] = field(default_factory=dict)
    exact: dict[str, ExactQuantiles] = field(default_factory=dict)

    def counter(self, name: str) -> Counter:
        """Get or create a counter."""
        return self.counters.setdefault(name, Counter(name))

    def histogram(self, name: str) -> Histogram:
        """Get or create a histogram."""
        return self.histograms.setdefault(name, Histogram(name))

    def observe_latency(self, name: str, value_ms: float) -> None:
        """Record a latency in both the histogram and the exact reservoir."""
        self.histogram(name).observe(value_ms)
        self.exact.setdefault(name, ExactQuantiles(name)).observe(value_ms)

    def snapshot(self) -> dict[str, object]:
        """Everything, in a form a dashboard or a test can read."""
        return {
            "counters": {
                name: counter.as_dict() for name, counter in sorted(self.counters.items())
            },
            "histograms": {
                name: histogram.as_dict() for name, histogram in sorted(self.histograms.items())
            },
            "exact_quantiles": {
                name: reservoir.summary() for name, reservoir in sorted(self.exact.items())
            },
        }

    def rate(self, counter_name: str, *labels: str) -> float | None:
        """Share of one label combination within its counter.

        The shape every rate in docs/observability.md is computed from: error
        rate, timeout rate, abstention rate.
        """
        counter = self.counters.get(counter_name)
        if counter is None or counter.total() == 0:
            return None
        return counter.value(*labels) / counter.total()


__all__ = [
    "DEFAULT_BUCKETS_MS",
    "Counter",
    "ExactQuantiles",
    "Histogram",
    "MetricsRegistry",
]

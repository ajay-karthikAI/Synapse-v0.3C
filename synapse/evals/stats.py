"""
synapse.evals.stats
===================
Aggregation and confidence intervals.

The requirement is "confidence intervals where statistically appropriate", and
the second half of that phrase is the load-bearing part. An interval computed
where its assumptions do not hold is worse than no interval: it lends a number
false authority. So each function states which interval it uses, and returns
``None`` with a recorded *reason* when none applies.

Three cases are distinguished:

* **Proportions** (hit rate, abstention accuracy, emergency sensitivity) get a
  **Wilson score interval**. The normal approximation is unusable here — at the
  sample sizes an evaluation set reaches, and at proportions near 0 or 1, it
  produces bounds outside [0, 1]. Wilson does not.
* **Means of per-case scores** (nDCG, MRR, readability) get a **bootstrap
  percentile interval**, seeded so a run is reproducible.
* **Anything with fewer than :data:`MIN_N_FOR_INTERVAL` observations** gets no
  interval at all, and says so. A 95% interval over four cases spans almost the
  whole range; printing it invites someone to quote it.

None of this makes an evaluation set a random sample of patient queries. It is
a curated set, so an interval describes sampling variability *within that set*,
not uncertainty about the population. That caveat is emitted into every report
rather than left to the reader.
"""

from __future__ import annotations  # Postponed annotations

import math  # Wilson interval arithmetic
import random  # Seeded bootstrap resampling
from dataclasses import dataclass

MIN_N_FOR_INTERVAL = 10  # Below this an interval is too wide to inform anything; reported as absent with a reason rather than printed

BOOTSTRAP_RESAMPLES = 2000  # Enough for a stable 95% percentile interval; fixed so two runs of the same data agree exactly

DEFAULT_CONFIDENCE = 0.95

# z for a two-sided 95% interval. Hard-coded rather than computed, because
# pulling in scipy for one constant would add a heavyweight dependency to a
# package whose whole point is running offline in a minimal environment.
_Z_95 = 1.959963984540054


@dataclass(frozen=True)
class Interval:
    """A confidence interval, or an explanation of why there is none."""

    low: float | None
    high: float | None
    method: str  # "wilson" | "bootstrap_percentile" | "none"
    reason: str | None = None  # Why no interval was computed

    @property
    def is_present(self) -> bool:
        """True when an interval was actually computed."""
        return self.low is not None and self.high is not None

    def as_dict(self) -> dict[str, object]:
        """Machine-readable form for reports."""
        return {
            "low": round(self.low, 4)
            if self.low is not None
            else None,  # Rounded so two runs produce byte-identical reports
            "high": round(self.high, 4) if self.high is not None else None,
            "method": self.method,
            "reason": self.reason,
        }


def wilson_interval(
    successes: int, trials: int, *, confidence: float = DEFAULT_CONFIDENCE
) -> Interval:
    """Wilson score interval for a binomial proportion.

    Preferred over the normal approximation because the latter produces bounds
    outside [0, 1] when the proportion is near 0 or 1 — which is exactly where
    safety metrics live. An emergency sensitivity of 1.0 over 18 cases must not
    report an upper bound of 1.15.
    """
    if trials <= 0:
        return Interval(None, None, "none", "no observations")
    if trials < MIN_N_FOR_INTERVAL:
        return Interval(
            None,
            None,
            "none",
            f"only {trials} observation(s); an interval would be uninformatively wide",
        )
    if (
        confidence != DEFAULT_CONFIDENCE
    ):  # Only the 95% z-value is tabulated; asking for another is a caller error, not a silent substitution
        return Interval(None, None, "none", "only 95% confidence is supported")

    proportion = successes / trials
    z_squared = _Z_95**2
    denominator = 1 + z_squared / trials
    centre = (
        proportion + z_squared / (2 * trials)
    ) / denominator  # Wilson's shifted centre, which is what keeps the bounds inside [0,1]
    spread = (_Z_95 / denominator) * math.sqrt(
        proportion * (1 - proportion) / trials + z_squared / (4 * trials**2)
    )
    return Interval(max(0.0, centre - spread), min(1.0, centre + spread), "wilson")


def bootstrap_interval(
    values: list[float],
    *,
    confidence: float = DEFAULT_CONFIDENCE,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = 0,
) -> Interval:
    """Bootstrap percentile interval for the mean of per-case scores.

    Used for metrics that are not proportions — nDCG and MRR are averages of
    per-case values in [0, 1], not counts of successes, so a binomial interval
    would be the wrong model.

    Seeded, so the same inputs always produce the same bounds. An unseeded
    bootstrap would make an evaluation report differ between runs for no reason
    anyone could explain.
    """
    if not values:
        return Interval(None, None, "none", "no observations")
    if len(values) < MIN_N_FOR_INTERVAL:
        return Interval(
            None,
            None,
            "none",
            f"only {len(values)} observation(s); an interval would be uninformatively wide",
        )

    rng = random.Random(seed)  # noqa: S311  # Not cryptographic: this is statistical resampling, and a seeded PRNG is exactly what reproducibility requires
    size = len(values)
    means = []
    for _ in range(resamples):
        sample = [values[rng.randrange(size)] for _ in range(size)]  # Resample with replacement
        means.append(sum(sample) / size)
    means.sort()

    tail = (1 - confidence) / 2
    low_index = int(tail * resamples)
    high_index = min(resamples - 1, int((1 - tail) * resamples))
    return Interval(means[low_index], means[high_index], "bootstrap_percentile")


def mean_or_none(values: list[float]) -> float | None:
    """Mean of ``values``, or None when the list is empty.

    None rather than 0.0, deliberately. The legacy evaluator returned 0.0 for
    "no ground truth", which is indistinguishable from "retrieval found
    nothing" — and that single conflation is why its reported recall was always
    zero regardless of retrieval quality.
    """
    if not values:
        return None
    return sum(values) / len(values)


def safe_ratio(numerator: float, denominator: float) -> float | None:
    """Ratio, or None when the denominator is zero.

    Same reasoning as :func:`mean_or_none`: an undefined metric is reported as
    undefined, never as zero.
    """
    if denominator == 0:
        return None
    return numerator / denominator


__all__ = [
    "BOOTSTRAP_RESAMPLES",
    "DEFAULT_CONFIDENCE",
    "MIN_N_FOR_INTERVAL",
    "Interval",
    "bootstrap_interval",
    "mean_or_none",
    "safe_ratio",
    "wilson_interval",
]

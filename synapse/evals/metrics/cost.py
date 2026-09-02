"""
synapse.evals.metrics.cost
==========================
Token accounting, latency, and cost estimation from a versioned pricing table.

**No price is hard-coded anywhere in Synapse.** Model pricing changes without
notice, and a stale constant compiled into source produces a cost report that
is confidently wrong. Prices live in ``config/pricing.toml``, which carries a
version and an effective date, and both are stamped into every evaluation run
so an old report can be read against the prices it actually used.

The shipped table is a **template with zero prices**. A run against it reports
0.00 and emits a loud warning, which is the honest failure mode: better an
obvious zero with a warning than a plausible-looking number nobody can source.
"""

from __future__ import annotations  # Postponed annotations

import tomllib  # 3.11 stdlib TOML reader; no PyYAML, no code-execution surface
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal  # Money is never floating point
from pathlib import Path

from synapse.errors import ArtifactSchemaError
from synapse.logging import get_logger

logger = get_logger(__name__)

TOKENS_PER_MILLION = Decimal(1_000_000)  # Prices are quoted per million tokens

STALE_PRICING_DAYS = 90  # Beyond this a price table is flagged in the report; model pricing moves faster than most release cycles


@dataclass(frozen=True)
class ModelPrice:
    """Input and output prices for one model, per million tokens."""

    model: str
    input_per_million: Decimal
    output_per_million: Decimal

    @property
    def is_configured(self) -> bool:
        """True when a real price has been supplied.

        Zero is treated as unconfigured rather than free: no provider charges
        nothing, so a zero means the template was never filled in.
        """
        return self.input_per_million > 0 or self.output_per_million > 0


@dataclass
class PricingTable:
    """A versioned set of model prices."""

    version: str
    effective_date: date
    currency: str
    prices: dict[str, ModelPrice] = field(default_factory=dict)
    source_note: str = ""

    def warnings(self, *, as_of: date | None = None) -> list[str]:
        """Statements a reader must see before trusting a cost figure."""
        today = as_of or datetime.now(UTC).date()
        notes: list[str] = []
        unconfigured = sorted(
            name for name, price in self.prices.items() if not price.is_configured
        )
        if unconfigured or not self.prices:
            notes.append(
                "Pricing is UNCONFIGURED for: "
                + (", ".join(unconfigured) if unconfigured else "every model")
                + ". Cost figures in this run are 0.00 and must not be used for budgeting."
            )
        age_days = (today - self.effective_date).days
        if age_days > STALE_PRICING_DAYS:
            notes.append(
                f"Pricing table '{self.version}' is {age_days} days old (effective {self.effective_date.isoformat()}). "
                "Model prices change without notice; re-verify before quoting these figures."
            )
        return notes

    def price_for(self, model: str) -> ModelPrice:
        """Price entry for a model, or a zero entry when it is absent.

        Absence is not an error: a run may exercise a model nobody has priced
        yet. It yields 0.00 and a warning, so the gap is visible rather than
        fatal.
        """
        return self.prices.get(model, ModelPrice(model, Decimal(0), Decimal(0)))


def load_pricing(path: Path) -> PricingTable:
    """Read a pricing table from TOML.

    Raises:
        ArtifactSchemaError: the file is missing or malformed. Cost estimation
            failing loudly is better than silently defaulting to zero without
            anyone noticing the file was never read.
    """
    if not path.is_file():
        raise ArtifactSchemaError(path=path, problem="pricing configuration not found")
    try:
        data = tomllib.loads(
            path.read_text(encoding="utf-8")
        )  # SAFE: tomllib has no code-execution capability
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ArtifactSchemaError(
            path=path, problem="pricing configuration is not valid TOML"
        ) from exc

    meta = data.get("pricing", {})
    try:
        table = PricingTable(
            version=str(meta["version"]),
            effective_date=date.fromisoformat(str(meta["effective_date"])),
            currency=str(meta.get("currency", "USD")),
            source_note=str(meta.get("source_note", "")),
        )
    except (KeyError, ValueError) as exc:
        raise ArtifactSchemaError(
            path=path, problem="pricing configuration must declare version and effective_date"
        ) from exc

    for model, entry in data.get("models", {}).items():
        try:
            table.prices[model] = ModelPrice(
                model=model,
                # Decimal(str(...)) rather than Decimal(float): constructing a
                # Decimal from a float carries the float's representation error
                # straight into the money arithmetic.
                input_per_million=Decimal(str(entry.get("input_per_million_tokens", 0))),
                output_per_million=Decimal(str(entry.get("output_per_million_tokens", 0))),
            )
        except (AttributeError, TypeError) as exc:
            raise ArtifactSchemaError(
                path=path, problem=f"malformed price entry for model '{model}'"
            ) from exc

    logger.info("pricing loaded", extra={"version": table.version, "models": len(table.prices)})
    return table


def estimate_cost(prompt_tokens: int, completion_tokens: int, price: ModelPrice) -> Decimal:
    """Cost of one call, in the table's currency.

    Decimal throughout: summing thousands of float costs accumulates error, and
    a cost report that does not reconcile with a provider invoice is worse than
    no report.
    """
    input_cost = (Decimal(prompt_tokens) / TOKENS_PER_MILLION) * price.input_per_million
    output_cost = (Decimal(completion_tokens) / TOKENS_PER_MILLION) * price.output_per_million
    return input_cost + output_cost


@dataclass
class LatencyBudget:
    """Latency observations for one stage across a run."""

    stage: str
    samples_ms: list[float] = field(default_factory=list)

    def percentile(self, fraction: float) -> float | None:
        """Percentile latency, by nearest-rank on the sorted samples.

        Nearest-rank rather than interpolated, so the reported value is one
        that was actually observed. p95 is the number that matters for a
        waiting-room interaction; a mean hides the tail entirely.
        """
        if not self.samples_ms:
            return None
        ordered = sorted(self.samples_ms)
        index = max(0, min(len(ordered) - 1, round(fraction * len(ordered)) - 1))
        return ordered[index]

    def summary(self) -> dict[str, float | None]:
        """p50, p95 and max. No mean: a mean latency hides the tail."""
        return {
            "p50_ms": self.percentile(0.50),
            "p95_ms": self.percentile(0.95),
            "max_ms": max(self.samples_ms) if self.samples_ms else None,
        }


__all__ = [
    "STALE_PRICING_DAYS",
    "TOKENS_PER_MILLION",
    "LatencyBudget",
    "ModelPrice",
    "PricingTable",
    "estimate_cost",
    "load_pricing",
]

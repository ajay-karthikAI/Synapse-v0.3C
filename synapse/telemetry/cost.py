"""
synapse.telemetry.cost
======================
Versioned cost estimation for a telemetry event.

**This module owns no prices.** It is a thin adapter over
:mod:`synapse.evals.metrics.cost`, which already loads a versioned table from
``config/pricing.toml``, already refuses to hard-code a figure anywhere in
runtime code, and already warns when the table is unconfigured or stale. Adding
a second pricing implementation for telemetry would guarantee the two disagree,
and a cost report that disagrees with itself is worse than no cost report.

What this adds is the small amount telemetry needs on top:

* a **cached** table, so a per-request cost estimate does not re-read a file;
* graceful absence — a deployment with no pricing file records 0.00 with an
  ``unconfigured`` version rather than failing a patient's request over a
  missing config file;
* the **pricing version stamped onto every event**, so a historical cost figure
  can be read against the prices that produced it.

Every figure this produces is an **estimate**. The schema carries
``cost_is_estimate`` as a field that cannot be set False, so no consumer can
read one of these numbers as an invoice.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from synapse.errors import ArtifactSchemaError
from synapse.evals.metrics.cost import PricingTable, estimate_cost, load_pricing
from synapse.logging import get_logger

logger = get_logger(__name__)

DEFAULT_PRICING_PATHS = (
    Path("config/pricing.toml"),
    Path("config/pricing.example.toml"),  # The shipped template: zero prices, loud warnings
)

UNCONFIGURED_VERSION = "unconfigured"


@dataclass
class CostEstimator:
    """Estimates the cost of a request from a versioned table.

    The table is loaded once. A deployment that rotates prices restarts the
    process or calls :meth:`reload`; per-request file reads on the serving path
    are not worth the cost of a stat call per answer.
    """

    table: PricingTable | None = None
    pricing_version: str = UNCONFIGURED_VERSION
    currency: str = "USD"

    @classmethod
    def load(cls, paths: tuple[Path, ...] = DEFAULT_PRICING_PATHS) -> CostEstimator:
        """Load the first pricing table that exists, or an unconfigured estimator.

        Absence is not an error here, unlike in the evaluation harness. An
        evaluation run reporting costs must fail loudly if it cannot find
        prices; a patient's request must not fail because a config file is
        missing, so this degrades to 0.00 with a version of ``unconfigured``
        and a warning in the log.
        """
        for path in paths:
            if not path.is_file():
                continue
            try:
                table = load_pricing(path)
            except ArtifactSchemaError as exc:
                logger.warning(
                    "pricing table unreadable; costs will be recorded as unconfigured",
                    extra={"error": type(exc).__name__, "path": path.name},
                )
                continue
            for warning in table.warnings():
                logger.warning("pricing warning", extra={"detail": warning})
            return cls(table=table, pricing_version=table.version, currency=table.currency)
        logger.warning(
            "no pricing table found; estimated costs will be 0.00",
            extra={"searched": len(paths)},
        )
        return cls()

    def reload(self, paths: tuple[Path, ...] = DEFAULT_PRICING_PATHS) -> CostEstimator:
        """Re-read the table, returning a fresh estimator."""
        return CostEstimator.load(paths)

    def estimate(self, model: str, input_tokens: int, output_tokens: int) -> Decimal:
        """Estimated cost of one request, in :attr:`currency`.

        Returns ``Decimal(0)`` when no table is loaded or the model is unpriced.
        Zero from an unconfigured table and zero from a free call are
        distinguishable by :attr:`pricing_version`, which is why it is stamped
        onto every event.
        """
        if self.table is None or not model:
            return Decimal(0)
        return estimate_cost(input_tokens, output_tokens, self.table.price_for(model))

    @property
    def is_configured(self) -> bool:
        """True when a table is loaded **and** carries at least one real price.

        Delegates to the P1 ``ModelPrice.is_configured`` rather than inspecting
        the version string: the shipped template loads successfully with a
        version of ``unconfigured-0`` and zero prices, so a version check alone
        would report a template as configured and make every 0.00 look
        deliberate.
        """
        if self.table is None:
            return False
        return any(price.is_configured for price in self.table.prices.values())


__all__ = [
    "DEFAULT_PRICING_PATHS",
    "UNCONFIGURED_VERSION",
    "CostEstimator",
]

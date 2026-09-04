"""
synapse.telemetry.config
========================
Whether telemetry runs at all, where it goes, and for how long it is kept.

The defaults are the private ones: **disabled**, environment ``local``, sink
``null``. A deployment that has not thought about telemetry records nothing,
which is the right default for a system handling health questions — the safe
state should be the one you get by doing nothing.

Configuration is read from the environment rather than a file so that turning
telemetry on is a deliberate deployment act that appears in a deployment
manifest, not a checked-in default somebody inherits.
"""

from __future__ import annotations  # Postponed annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from synapse.telemetry.schema import Environment


class SinkKind(StrEnum):
    """Which sink implementation to build."""

    NULL = "null"  # Discard. The default.
    MEMORY = "memory"  # Tests.
    FILE = "file"  # Local development, local disk only.


@dataclass(frozen=True)
class TelemetryConfig:
    """Runtime telemetry settings.

    Every field is documented in docs/privacy-logging-policy.md §6, which is the
    authority on what each means for data retention.
    """

    enabled: bool = False  # Rule 11: telemetry is off unless switched on
    environment: Environment = Environment.LOCAL
    sink: SinkKind = SinkKind.NULL
    retention_days: int = 30
    session_ttl_seconds: int = 1800  # 30 minutes; a sitting, not a lifetime
    local_sink_path: Path = Path("artifacts/telemetry")
    app_version: str = "0.0.0"

    @classmethod
    def from_environment(cls, app_version: str = "0.0.0") -> TelemetryConfig:
        """Build from environment variables.

        ``SYNAPSE_TELEMETRY=1`` enables it. Anything else, including unset,
        leaves it off — a truthy-string parser that accepted "false" would be a
        privacy bug, so only an explicit "1", "true" or "yes" counts.
        """
        raw = os.environ.get("SYNAPSE_TELEMETRY", "").strip().lower()
        enabled = raw in {"1", "true", "yes", "on"}
        # `SYNAPSE_ENVIRONMENT` is the deployment alias; the hosting
        # configuration sets it to "demo". Primary first, so an existing
        # `SYNAPSE_ENV` keeps winning. An unrecognised value falls back to
        # LOCAL rather than raising, which keeps a typo from being labelled
        # production.
        environment = _parse_environment(
            os.environ.get("SYNAPSE_ENV", "").strip() or os.environ.get("SYNAPSE_ENVIRONMENT", "")
        )
        sink = _parse_sink(os.environ.get("SYNAPSE_TELEMETRY_SINK", ""), enabled)
        return cls(
            enabled=enabled,
            environment=environment,
            sink=sink,
            retention_days=_parse_int(os.environ.get("SYNAPSE_TELEMETRY_RETENTION_DAYS"), 30),
            session_ttl_seconds=_parse_int(os.environ.get("SYNAPSE_SESSION_TTL_SECONDS"), 1800),
            local_sink_path=Path(os.environ.get("SYNAPSE_TELEMETRY_DIR", "artifacts/telemetry")),
            app_version=app_version,
        )

    def describe(self) -> str:
        """One line, safe to log at startup."""
        if not self.enabled:
            return "telemetry disabled"
        return (
            f"telemetry enabled; env={self.environment.value} sink={self.sink.value} "
            f"retention={self.retention_days}d"
        )


def _parse_environment(raw: str) -> Environment:
    """Parse the deployment environment, defaulting to ``local``."""
    try:
        return Environment(raw.strip().lower())
    except ValueError:
        return Environment.LOCAL


def _parse_sink(raw: str, enabled: bool) -> SinkKind:
    """Parse the sink kind.

    Disabled telemetry always yields ``null``: a configured sink plus a disabled
    flag is a contradiction, and resolving it toward "record nothing" is the
    only safe direction.
    """
    if not enabled:
        return SinkKind.NULL
    try:
        return SinkKind(raw.strip().lower())
    except ValueError:
        return SinkKind.FILE  # Enabled without a named sink means local development


def _parse_int(raw: str | None, default: int) -> int:
    """Parse a positive integer setting, falling back to the default."""
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


__all__ = ["SinkKind", "TelemetryConfig"]

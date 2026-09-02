"""
synapse.telemetry.sinks
=======================
Where events go — behind an interface that names no vendor.

Four implementations ship, and none of them is an analytics provider:

* :class:`NullSink`      — telemetry disabled. The default.
* :class:`InMemorySink`  — tests. Holds events so assertions can read them.
* :class:`LocalFileSink` — local development. Newline-delimited JSON on local
  disk, under a path that is gitignored, with a documented retention and a
  ``prune`` method that implements deletion.
* :class:`FailSafeSink`  — a wrapper. Any exception from the sink it wraps is
  swallowed and counted, because **telemetry must never break a patient
  response** (docs/privacy-logging-policy.md, architecture requirement).

The :class:`TelemetrySink` protocol is deliberately two methods. A narrow
interface is what keeps the domain independent of a vendor: adding an
OpenTelemetry exporter later means writing one class in one file, not touching
the pipeline, the answer layer or the schema. No exporter is configured here,
and configuring one is a deployment decision this repository does not make.
"""

from __future__ import annotations  # Postponed annotations

import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from synapse.logging import get_logger
from synapse.telemetry.schema import TelemetryEvent

logger = get_logger(__name__)

LOCAL_SINK_BANNER = "SYNAPSE LOCAL TELEMETRY — local development only, not an analytics destination"


class TelemetrySink(Protocol):
    """Where a validated event is delivered.

    Implementations must not raise. :class:`FailSafeSink` enforces that for
    implementations that might.
    """

    def emit(self, event: TelemetryEvent) -> None:
        """Deliver one event."""
        ...

    def flush(self) -> None:
        """Deliver anything buffered."""
        ...


class NullSink:
    """Telemetry disabled: events are discarded without being serialised.

    The default. A deployment that has not thought about telemetry records
    nothing, which is the correct default for a system handling health
    questions — the safe state is the one you get by doing nothing.
    """

    def emit(self, event: TelemetryEvent) -> None:
        """Discard the event."""
        return None

    def flush(self) -> None:
        """Nothing is buffered."""
        return None


@dataclass
class InMemorySink:
    """Collects events in memory, for tests.

    Bounded so a runaway test cannot exhaust memory, and the bound is visible in
    ``dropped`` rather than silent.
    """

    max_events: int = 10_000
    events: list[TelemetryEvent] = field(default_factory=list)
    dropped: int = 0

    def emit(self, event: TelemetryEvent) -> None:
        """Retain the event, up to the bound."""
        if len(self.events) >= self.max_events:
            self.dropped += 1
            return
        self.events.append(event)

    def flush(self) -> None:
        """Nothing is buffered."""
        return None

    def clear(self) -> None:
        """Forget everything collected so far."""
        self.events.clear()
        self.dropped = 0

    def serialised(self) -> list[str]:
        """Every event as the JSON a file sink would write.

        Used by the privacy tests: asserting against the *serialised* form is
        what catches a leak through a nested structure that a field-by-field
        assertion would miss.
        """
        return [event.model_dump_json() for event in self.events]


@dataclass
class LocalFileSink:
    """Newline-delimited JSON on local disk. Local development only.

    Every file starts with a banner line identifying it as local-only, so a file
    that escapes into a shared location is recognisable as something that was
    never meant to be an analytics feed.

    Writes are append-only and one line per event. ``prune`` is the documented
    deletion path (docs/privacy-logging-policy.md §6): it deletes whole files
    past the retention window, because the records carry no subject identifier
    to delete by — there is deliberately nothing to delete per person, since
    nothing per person was recorded.
    """

    directory: Path = Path("artifacts/telemetry")
    retention_days: int = 30
    _handle: object = field(default=None, init=False, repr=False)

    def _path_for(self, moment: datetime) -> Path:
        """One file per UTC day, so retention is a file-level decision."""
        return self.directory / f"telemetry-{moment.strftime('%Y-%m-%d')}.jsonl"

    def emit(self, event: TelemetryEvent) -> None:
        """Append one event as a single JSON line."""
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path_for(event.occurred_at)
        is_new = not path.exists()
        with path.open("a", encoding="utf-8") as handle:
            if is_new:
                # ensure_ascii=False so the banner is readable when someone
                # opens the file, which is the entire point of a banner.
                handle.write(json.dumps({"_banner": LOCAL_SINK_BANNER}, ensure_ascii=False) + "\n")
            handle.write(event.model_dump_json() + "\n")

    def flush(self) -> None:
        """Writes are unbuffered; nothing to do."""
        return None

    def read_all(self) -> Iterator[dict]:
        """Every recorded event, banners excluded. For local inspection."""
        if not self.directory.is_dir():
            return
        for path in sorted(self.directory.glob("telemetry-*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                if "_banner" in record:
                    continue
                yield record

    def prune(self, *, now: datetime | None = None) -> int:
        """Delete records past the retention window. Returns files deleted.

        This is the deletion mechanism a deployment schedules. It is a method
        rather than a script so it can be tested, and it is tested.
        """
        if not self.directory.is_dir():
            return 0
        cutoff = (now or datetime.now(UTC)).date() - timedelta(days=self.retention_days)
        deleted = 0
        for path in sorted(self.directory.glob("telemetry-*.jsonl")):
            try:
                stamp = datetime.strptime(path.stem.removeprefix("telemetry-"), "%Y-%m-%d").date()
            except ValueError:
                continue  # Not one of ours; leave it alone
            if stamp < cutoff:
                path.unlink()
                deleted += 1
        if deleted:
            logger.info("telemetry pruned", extra={"files_deleted": deleted})
        return deleted


@dataclass
class FailSafeSink:
    """Wraps a sink so that its failures cannot reach the caller.

    The requirement this exists for: *failure to emit telemetry must never
    expose sensitive data or crash a patient response.* A disk full, a
    permissions change or a bug in a future exporter must degrade to "no
    telemetry", never to "no answer".

    Failures are **counted, not logged with detail**. Logging the exception
    would reintroduce the message this layer exists to keep out, so only the
    type name survives — the same rule as
    :func:`synapse.telemetry.redaction.redact_exception`.
    """

    inner: TelemetrySink
    failures: int = 0
    last_failure_type: str = ""

    def emit(self, event: TelemetryEvent) -> None:
        """Deliver, absorbing any failure."""
        try:
            self.inner.emit(event)
        except Exception as exc:  # Broad by design: nothing a sink raises may reach a patient
            self.failures += 1
            self.last_failure_type = type(exc).__name__
            logger.warning("telemetry sink failed", extra={"error": self.last_failure_type})

    def flush(self) -> None:
        """Flush, absorbing any failure."""
        try:
            self.inner.flush()
        except Exception as exc:  # Same reasoning as emit
            self.failures += 1
            self.last_failure_type = type(exc).__name__


def default_local_directory() -> Path:
    """Where local telemetry goes when nothing is configured.

    Honours ``SYNAPSE_TELEMETRY_DIR`` so a developer can redirect it, and falls
    back to a temporary directory when the repository path is not writable.
    """
    configured = os.environ.get("SYNAPSE_TELEMETRY_DIR")
    if configured:
        return Path(configured)
    candidate = Path("artifacts/telemetry")
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    except OSError:
        return Path(tempfile.gettempdir()) / "synapse-telemetry"


__all__ = [
    "LOCAL_SINK_BANNER",
    "FailSafeSink",
    "InMemorySink",
    "LocalFileSink",
    "NullSink",
    "TelemetrySink",
    "default_local_directory",
]

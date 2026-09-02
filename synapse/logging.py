"""
synapse.logging
===============
Structured logging.

Replaces the ``print()`` calls scattered through the legacy ingestion pipeline.
Prints were unusable for three reasons: they could not be filtered by severity,
they had no machine-readable structure, and three of them fired at *import
time* (``print("WORKS")``), so merely importing the corpus module polluted
stdout — which in Streamlit means the server log on every rerun.

Two formatters are provided:

* ``text``  — human-readable, for a terminal;
* ``json``  — one JSON object per line, for a log pipeline or a CI artifact.

Structured fields are attached with the standard ``extra=`` mechanism and
collected under a ``fields`` key, so a call site never has to build a message
string out of values that a consumer will only have to parse apart again.

**Privacy:** loggers here are for build-time ingestion, whose inputs are topic
strings rather than patient text. Even so, no helper logs record bodies —
abstract text is third-party copyrighted content, and the same logging surface
will later carry query-time events.
"""

from __future__ import annotations  # Postponed annotations

import json  # JSON formatter output
import logging  # Standard library logging; no third-party logging dependency
import sys  # Log records go to stderr, keeping stdout free for machine-readable command output
from typing import Any

LOGGER_ROOT = (
    "synapse"  # All Synapse loggers hang off this name, so one call configures the package
)

_RESERVED_RECORD_KEYS = (
    frozenset(  # LogRecord attributes that are NOT caller-supplied structured fields
        {
            "args",
            "asctime",
            "created",
            "exc_info",
            "exc_text",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "message",
            "module",
            "msecs",
            "msg",
            "name",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "taskName",
            "thread",
            "threadName",
        }
    )
)


def _structured_fields(
    record: logging.LogRecord,
) -> dict[str, Any]:  # Extract only what the call site added via extra=
    """Return the caller-supplied ``extra=`` fields attached to a record."""
    return {
        key: value for key, value in record.__dict__.items() if key not in _RESERVED_RECORD_KEYS
    }


class JsonFormatter(logging.Formatter):  # One JSON object per line
    """Render a log record as a single JSON object."""

    def format(
        self, record: logging.LogRecord
    ) -> str:  # 'format' is the logging API's name, not ours to choose
        """Serialise the record and its structured fields."""
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(
                record, "%Y-%m-%dT%H:%M:%S%z"
            ),  # ISO-8601 so log lines sort chronologically as text
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = _structured_fields(record)  # Everything the call site passed via extra=
        if fields:
            payload["fields"] = (
                fields  # Nested under one key so field names can never collide with the envelope
            )
        if record.exc_info:  # Include a traceback when one was captured
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(
            payload, default=str, ensure_ascii=False
        )  # default=str so a Path or datetime never crashes the logger


class TextFormatter(logging.Formatter):  # Human-readable, for a terminal
    """Render a log record as an aligned single line with its fields appended."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s %(name)s | %(message)s", datefmt="%H:%M:%S"
        )

    def format(self, record: logging.LogRecord) -> str:
        """Append structured fields to the rendered message."""
        base = super().format(record)
        fields = _structured_fields(record)
        if not fields:
            return base
        rendered = " ".join(
            f"{key}={value}" for key, value in sorted(fields.items())
        )  # Sorted so repeated runs produce comparable output
        return f"{base} [{rendered}]"


def configure_logging(level: str = "INFO", *, json_output: bool = False) -> None:
    """Configure the ``synapse`` logger hierarchy.

    Called once from a CLI entry point. Library modules never configure
    logging themselves — doing so would hijack the configuration of any
    application that imports Synapse.
    """
    logger = logging.getLogger(LOGGER_ROOT)
    logger.setLevel(level.upper())
    logger.handlers.clear()  # Idempotent: re-configuring does not stack duplicate handlers
    handler = logging.StreamHandler(
        sys.stderr
    )  # stderr, so stdout stays clean for machine-readable output
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())
    logger.addHandler(handler)
    logger.propagate = (
        False  # Do not also emit through the root logger, which would double every line
    )


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the ``synapse`` hierarchy.

    Modules call this at import time; it allocates a logger object and performs
    no I/O and no configuration, so importing a Synapse module stays silent.
    """
    return logging.getLogger(name if name.startswith(LOGGER_ROOT) else f"{LOGGER_ROOT}.{name}")


__all__ = ["LOGGER_ROOT", "JsonFormatter", "TextFormatter", "configure_logging", "get_logger"]

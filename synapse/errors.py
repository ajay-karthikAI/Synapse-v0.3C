"""
synapse.errors
==============
Typed, user-safe exception hierarchy for artifact handling.

Two design rules drive this module:

1. **Typed.** Callers must be able to distinguish "this file is corrupt" from
   "this file is from a newer schema" from "this file does not exist", because
   the correct operator response differs for each. A single ValueError cannot
   express that.

2. **User-safe.** These exceptions are raised while parsing *untrusted* corpus
   files, manifests and migration inputs. Their messages may end up in logs, in
   a Streamlit error card, or in a CI report. So a message may contain
   structural facts (path, line number, hash prefix, counts, field name) and
   must NEVER contain record payload text — chunk bodies are third-party
   copyrighted abstracts, and in future may sit next to patient-derived text.
   `_SafeError` enforces this by constructing the message itself from an
   allow-list of scalar detail values rather than interpolating caller strings.
"""

from __future__ import annotations  # Postponed annotations so this module has zero import-time cost

from pathlib import Path  # Used only for type hints and for reducing paths to their basename
from typing import Any  # Detail values are heterogeneous scalars

# ---------------------------------------------------------------------------
# Detail sanitisation
# ---------------------------------------------------------------------------

_MAX_DETAIL_CHARS = 120  # Hard cap on any single rendered detail value; bounds worst-case leakage even if a caller passes something unexpected

_SAFE_SCALARS = (
    bool,
    int,
    float,
)  # Types reproduced verbatim in a message: they cannot carry document text


def _safe_value(
    value: Any,
) -> str:  # Render one detail value in a form guaranteed not to leak payload text
    """Reduce an arbitrary detail value to a short, non-sensitive string."""
    if (
        value is None
    ):  # Explicit null renders as a marker rather than the word "None" buried in prose
        return "<none>"
    if isinstance(value, _SAFE_SCALARS):  # Numbers and booleans are structural facts, never content
        return str(value)
    if isinstance(
        value, Path
    ):  # Paths are shown by NAME ONLY — a full path can leak a patient identifier or username from a directory name
        return value.name
    text = str(value)  # Everything else is coerced to text and then truncated
    if (
        len(text) > _MAX_DETAIL_CHARS
    ):  # Truncate over-long values so a stray payload string cannot be dumped wholesale
        return text[:_MAX_DETAIL_CHARS] + "…(truncated)"
    return text


class _SafeError(Exception):  # Private base: every Synapse artifact error derives from this
    """Base class that builds its own message from structured, sanitised details."""

    reason = "artifact error"  # Human-readable category, overridden per subclass; forms the message prefix

    def __init__(
        self, **details: Any
    ) -> (
        None
    ):  # Callers pass keyword details ONLY — there is no free-text message parameter by design
        self.details: dict[str, Any] = (
            details  # Retained unsanitised for programmatic inspection by the caller (never auto-rendered)
        )
        rendered = ", ".join(  # Build the display message from sanitised values only
            f"{key}={_safe_value(value)}" for key, value in details.items()
        )
        self.safe_message = (
            f"{self.reason}: {rendered}" if rendered else self.reason
        )  # The one string safe to surface to a user or a log
        super().__init__(
            self.safe_message
        )  # Hand the sanitised string to Exception so str(exc) is safe too

    def __str__(
        self,
    ) -> str:  # Guarantee that stringifying the exception can never bypass sanitisation
        return self.safe_message


# ---------------------------------------------------------------------------
# Public hierarchy
# ---------------------------------------------------------------------------


class SynapseArtifactError(
    _SafeError
):  # Single catch-all base so callers can `except SynapseArtifactError` at a boundary
    """Base class for every artifact-layer failure."""

    reason = "artifact error"


class ArtifactNotFoundError(SynapseArtifactError):  # A declared artifact is missing from disk
    """A file named by a manifest (or by the caller) does not exist."""

    reason = "artifact not found"


class ArtifactSchemaError(SynapseArtifactError):  # A record failed structural validation
    """A record is malformed: bad JSON, wrong type, unknown field, or failed invariant."""

    reason = "artifact failed schema validation"


class ArtifactVersionError(
    SynapseArtifactError
):  # A record is structurally fine but from an incompatible schema generation
    """A record's schema_version is not compatible with this build of Synapse."""

    reason = "incompatible artifact schema version"


class ArtifactIntegrityError(SynapseArtifactError):  # Content does not match its recorded hash
    """A SHA-256 digest does not match the value recorded in the manifest."""

    reason = "artifact integrity check failed"


class ArtifactCompatibilityError(
    SynapseArtifactError
):  # Individually valid artifacts that do not belong together
    """Artifacts are each internally valid but mutually inconsistent.

    The canonical case this exists for: a FAISS index whose row order no longer
    matches the corpus chunk order. Both files verify in isolation, yet serving
    from them produces answers citing the wrong document — the failure mode
    identified as C8 in docs/quality-architecture.md §1.2.
    """

    reason = "artifacts are mutually incompatible"


class UnsafeArtifactError(SynapseArtifactError):  # A refusal, not a malfunction
    """An operation was refused because it would require unsafe deserialisation."""

    reason = "refused unsafe artifact operation"


__all__ = [  # Explicit export list keeps the public error surface reviewable
    "ArtifactCompatibilityError",
    "ArtifactIntegrityError",
    "ArtifactNotFoundError",
    "ArtifactSchemaError",
    "ArtifactVersionError",
    "SynapseArtifactError",
    "UnsafeArtifactError",
]

"""
synapse.schemas.base
====================
Base model configuration and schema-version compatibility.

Pydantic is used rather than plain dataclasses because this layer's whole job
is validating *untrusted* input, and the difference is material:

  * ``extra="forbid"`` rejects unknown fields. A dataclass silently ignores
    them, so a corpus file carrying an unexpected key would load "successfully"
    while quietly dropping data.
  * Cross-field invariants (``approved`` implies a review record exists) are
    declared next to the fields they constrain, not scattered across callers.
  * Strict typing prevents coercion accidents — the string ``"2220"`` does not
    become the integer ``2220``, so a corrupted count cannot pass verification.
  * ``model_json_schema()`` exports a JSON Schema, so the contract can be
    published and diffed rather than living only in Python.

Versioning policy
-----------------
Versions are ``MAJOR.MINOR``:

  * different ``MAJOR``   → incompatible; refuse.
  * record ``MINOR`` >  reader ``MINOR`` → refuse. The file was written by a
    newer build that may rely on fields this build does not understand.
    Combined with ``extra="forbid"``, accepting it would fail confusingly
    later; refusing is the fail-closed choice for medical provenance data.
  * record ``MINOR`` <= reader ``MINOR`` → accept. Fields added since are
    optional by policy, so an older record still validates.
"""

from __future__ import annotations  # Postponed annotations

from datetime import datetime  # Timezone validation for every timestamp field
from typing import Any, ClassVar  # ClassVar marks schema constants that are NOT pydantic fields

from pydantic import BaseModel, ConfigDict, model_validator

from synapse.errors import (  # Typed failures rather than pydantic's raw ValidationError at the public boundary
    ArtifactSchemaError,
    ArtifactVersionError,
)

SCHEMA_REGISTRY: dict[
    str, str
] = {}  # Populated at import time: {schema_name: current_version}; lets the migration CLI and docs report exactly which versions a build writes


def parse_schema_version(value: str) -> tuple[int, int]:  # Split "1.2" into (1, 2)
    """Parse a ``MAJOR.MINOR`` schema version string."""
    parts = value.split(".")  # Split on the single separator
    if len(parts) != 2:  # Exactly two components; "1", "1.2.3" and "" are all rejected
        raise ArtifactVersionError(problem="malformed schema_version", value=value)
    try:
        major, minor = int(parts[0]), int(parts[1])  # Both components must be integers
    except ValueError as exc:  # Non-numeric component, e.g. "1.x"
        raise ArtifactVersionError(
            problem="non-numeric schema_version", value=value
        ) from exc  # `from exc` preserves the cause without leaking payload text
    if (
        major < 0 or minor < 0
    ):  # Negative versions are meaningless and would break ordering comparisons
        raise ArtifactVersionError(problem="negative schema_version", value=value)
    return major, minor


def is_compatible_version(
    record_version: str, reader_version: str
) -> bool:  # Pure predicate; the policy above expressed as code
    """True when a record at ``record_version`` may be read by ``reader_version``."""
    record_major, record_minor = parse_schema_version(record_version)
    reader_major, reader_minor = parse_schema_version(reader_version)
    if record_major != reader_major:  # A major bump means a breaking change; never read across it
        return False
    return (
        record_minor <= reader_minor
    )  # Refuse records from a NEWER minor: fail closed rather than dropping fields we do not know about


def require_compatible_version(schema_name: str, record_version: str, reader_version: str) -> None:
    """Raise :class:`ArtifactVersionError` unless the versions are compatible."""
    if not is_compatible_version(
        record_version, reader_version
    ):  # Delegate the policy to the predicate so both share one implementation
        raise ArtifactVersionError(
            schema=schema_name,  # Which schema disagreed
            found=record_version,  # What the file claims
            supported=reader_version,  # What this build can read
        )


class SynapseModel(BaseModel):  # Base for EVERY model in the package
    """Base model configured for untrusted input."""

    model_config = ConfigDict(
        extra="forbid",  # SECURITY: unknown fields are an error. A corpus file cannot smuggle extra keys past validation, and a typo'd field name fails loudly instead of being silently dropped.
        frozen=True,  # Immutable after construction: a validated record cannot be mutated into an invalid one downstream
        strict=False,  # Allow documented conveniences such as ISO-8601 strings for datetimes; type coercion between unrelated types is still rejected
        str_strip_whitespace=False,  # NEVER auto-strip: chunk text is content-addressed, so silently trimming it would invalidate its stored hash
        validate_default=True,  # Defaults are validated too, so a bad default is caught in tests rather than in production data
        use_enum_values=False,  # Keep real enum members in memory for type safety; StrEnum still serialises to plain strings
    )

    def to_json_line(
        self,
    ) -> str:  # Canonical single-line JSON serialisation used by the JSONL writer
        """Serialise to one deterministic JSON line (no trailing newline)."""
        return self.model_dump_json(
            exclude_none=False
        )  # Field order follows declaration order, so output is byte-stable across runs — a precondition for hashing


class VersionedModel(
    SynapseModel
):  # Base for models persisted to disk, which therefore need a version stamp
    """A model that carries and enforces a ``schema_version``."""

    SCHEMA_NAME: ClassVar[str] = (
        "base"  # Identifies the schema in errors and in SCHEMA_REGISTRY; ClassVar so pydantic does not treat it as a field
    )
    SCHEMA_VERSION: ClassVar[str] = "1.0"  # The version THIS build writes and can read

    schema_version: str = ""  # Persisted version stamp; filled from SCHEMA_VERSION when absent so callers never have to pass it

    def __init_subclass__(cls, **kwargs: Any) -> None:  # Runs once per subclass definition
        super().__init_subclass__(**kwargs)
        SCHEMA_REGISTRY[cls.SCHEMA_NAME] = (
            cls.SCHEMA_VERSION
        )  # Self-register, so the registry can never drift from the classes it describes

    @model_validator(
        mode="before"
    )  # Runs BEFORE field validation, so it can supply the default and reject early
    @classmethod
    def _stamp_and_check_version(cls, data: Any) -> Any:
        """Default the version stamp when absent; refuse an incompatible one."""
        if not isinstance(
            data, dict
        ):  # Non-dict input (e.g. an already-constructed model) needs no stamping
            return data
        # An ABSENT key means "the caller did not supply one" and is stamped.
        # A key that is PRESENT but empty is a malformed record, not an absent
        # version, and must be refused — otherwise a corrupted file would be
        # silently re-stamped as current.
        if "schema_version" not in data or data["schema_version"] is None:
            return {
                **data,
                "schema_version": cls.SCHEMA_VERSION,
            }  # Copy rather than mutate, so a caller's dict is never modified as a side effect
        raw = data["schema_version"]  # What the incoming record claims
        if not isinstance(
            raw, str
        ):  # A non-string version means the file is malformed, not merely outdated
            raise ArtifactSchemaError(
                schema=cls.SCHEMA_NAME, field="schema_version", problem="not a string"
            )
        require_compatible_version(
            cls.SCHEMA_NAME, raw, cls.SCHEMA_VERSION
        )  # Raises ArtifactVersionError on mismatch — the check required by requirement 7
        return data


def require_timezone_aware(
    value: datetime, field_name: str
) -> datetime:  # Shared validator helper used by every timestamp field
    """Return ``value`` unchanged, or raise if it carries no timezone.

    Naive timestamps are rejected because artifact provenance is compared
    across machines and CI runners in different zones; a naive "built at
    14:03" is not a fact anyone can verify.
    """
    if (
        value.tzinfo is None or value.utcoffset() is None
    ):  # Both checks needed: tzinfo can be present but return None from utcoffset()
        # ValueError, not ArtifactSchemaError: pydantic wraps ValueError raised
        # inside a validator into a ValidationError, so every field-level
        # failure surfaces through one exception type. Raising our own error
        # here would escape that wrapping and make `except ValidationError`
        # silently miss timezone problems.
        raise ValueError(f"{field_name} is not timezone-aware")
    return value


__all__ = [
    "SCHEMA_REGISTRY",
    "SynapseModel",
    "VersionedModel",
    "is_compatible_version",
    "parse_schema_version",
    "require_compatible_version",
    "require_timezone_aware",
]

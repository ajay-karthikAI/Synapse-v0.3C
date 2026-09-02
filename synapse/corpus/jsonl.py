"""
synapse.corpus.jsonl
====================
Typed JSONL reading and writing.

Every function here treats the file as untrusted:

  * lines are parsed with :func:`json.loads` — never ``eval``, never
    ``pickle``, never a YAML loader;
  * each parsed object is validated through its pydantic model, so unknown
    fields, wrong types and failed invariants are rejected rather than
    silently accepted;
  * a failure names the file and the line number, and nothing else. The record
    body is never echoed into the error, because corpus text is third-party
    content and may in future sit alongside patient-derived text;
  * an oversized line is refused before it is parsed, so a malformed or hostile
    file cannot exhaust memory.

Writing is deterministic: the same records always produce the same bytes, which
is what allows a corpus to be rebuilt and still match a stored digest.
"""

from __future__ import annotations  # Postponed annotations

import gzip  # Transparent compression for large corpora
import io  # TextIOWrapper, to layer text encoding over the binary gzip stream
import json  # The ONLY deserialiser used on corpus data
from collections.abc import Iterable, Iterator  # Streaming type hints
from contextlib import contextmanager  # Used so every layer of a gzip stack is closed exactly once
from pathlib import Path
from typing import IO, TypeVar

from pydantic import ValidationError

from synapse.errors import ArtifactNotFoundError, ArtifactSchemaError, ArtifactVersionError
from synapse.hashing import sha256_records
from synapse.schemas.base import SynapseModel

ModelT = TypeVar(
    "ModelT", bound=SynapseModel
)  # Generic model parameter, so read_jsonl returns the caller's concrete type

MAX_LINE_BYTES = (
    4 * 1024 * 1024
)  # 4 MiB per record; a chunk is normally a few hundred bytes, so anything near this is malformed or hostile

GZIP_SUFFIX = (
    ".gz"  # Compression is chosen by suffix, so a path alone fully determines the encoding
)

_GZIP_DETERMINISTIC_MTIME = 0  # Fixed timestamp in the gzip header: without this, two identical corpora written a second apart produce different file digests


class JsonlReadError(
    ArtifactSchemaError
):  # Specialisation so callers can catch JSONL problems distinctly
    """A JSONL file could not be read as the requested model."""

    reason = "malformed JSONL record"


@contextmanager  # A context manager rather than a plain factory, so every layer of the gzip stack is closed in order
def _open_for_write(
    path: Path,
) -> Iterator[IO[str]]:  # Chooses gzip or plain text based on the suffix
    """Open ``path`` for writing, transparently gzipping when the suffix says so."""
    if path.suffix == GZIP_SUFFIX:  # e.g. chunks.jsonl.gz
        # All three layers in one statement: the raw file (GzipFile does NOT
        # close a fileobj it was handed, so it must be managed here), the gzip
        # encoder, and the text wrapper. Exiting closes them innermost-first.
        with (
            path.open("wb") as raw,
            gzip.GzipFile(
                filename="",  # Omit the original filename from the header: it is a non-deterministic field
                mode="wb",
                fileobj=raw,
                mtime=_GZIP_DETERMINISTIC_MTIME,  # Fixed mtime: the other non-deterministic header field
            ) as binary,
            io.TextIOWrapper(
                binary, encoding="utf-8", newline="\n"
            ) as text,  # Text layer with a fixed newline, so output does not vary by platform
        ):
            yield text
    else:
        with path.open(
            "w", encoding="utf-8", newline="\n"
        ) as text:  # Explicit encoding and newline for the same reason
            yield text


@contextmanager
def _open_for_read(path: Path) -> Iterator[IO[str]]:  # Mirror of the above for reading
    """Open ``path`` for reading, transparently decompressing when the suffix says so."""
    if path.suffix == GZIP_SUFFIX:
        with gzip.open(
            path, "rt", encoding="utf-8", newline=""
        ) as handle:  # 'rt' = text mode over the decompressed stream
            yield handle
    else:
        with path.open("r", encoding="utf-8", newline="") as handle:
            yield handle


def canonical_record_lines(
    records: Iterable[SynapseModel],
) -> Iterator[str]:  # Single definition of "the canonical serialisation of a record"
    """Yield the canonical one-line JSON form of each record.

    Used both by the writer and by :func:`records_digest`, so the bytes that
    are hashed are exactly the bytes that are written.
    """
    for record in records:
        yield record.to_json_line()  # Field order follows declaration order in the model, making the output byte-stable


def write_jsonl(
    path: Path, records: Iterable[SynapseModel]
) -> int:  # Returns the number written, so callers can record counts in a manifest
    """Write ``records`` to ``path`` as JSONL, returning the number written.

    Deterministic: identical records always produce identical file bytes,
    including under gzip.
    """
    path.parent.mkdir(
        parents=True, exist_ok=True
    )  # Create the artifact directory on demand rather than requiring callers to
    count = 0  # Running total returned to the caller
    with _open_for_write(path) as handle:
        for line in canonical_record_lines(
            records
        ):  # Stream: a large corpus is never fully materialised in memory
            handle.write(line)
            handle.write("\n")  # One record per line is the entire JSONL contract
            count += 1
    return count


def read_jsonl(
    path: Path, model: type[ModelT]
) -> Iterator[ModelT]:  # Generator, so a caller can stream or list() as it prefers
    """Read and validate every record in ``path`` as ``model``.

    Raises:
        ArtifactNotFoundError: the file does not exist.
        ArtifactVersionError: a record declares an incompatible schema_version.
        JsonlReadError: a line is not valid JSON, or fails model validation.
    """
    if not path.is_file():  # Typed error rather than a bare OSError from open()
        raise ArtifactNotFoundError(path=path)

    with _open_for_read(path) as handle:
        for line_number, raw_line in enumerate(
            handle, start=1
        ):  # 1-based line numbers match what an editor shows
            stripped = raw_line.strip()  # Tolerate trailing newlines and stray whitespace
            if not stripped:  # Blank lines are skipped rather than treated as errors, so a trailing newline is harmless
                continue
            if (
                len(stripped.encode("utf-8")) > MAX_LINE_BYTES
            ):  # Bound the work BEFORE parsing: refuse rather than attempt
                raise JsonlReadError(
                    path=path, line=line_number, problem="record exceeds maximum size"
                )
            try:
                payload = json.loads(
                    stripped
                )  # SAFE deserialisation: json.loads cannot construct arbitrary objects or execute code
            except json.JSONDecodeError as exc:  # Narrow catch: only JSON syntax problems
                raise JsonlReadError(
                    path=path, line=line_number, problem="invalid JSON"
                ) from exc  # Note the decoder's message is NOT included — it can quote the offending record text
            if not isinstance(
                payload, dict
            ):  # A JSON array or scalar is not a record, and would confuse the model constructor
                raise JsonlReadError(
                    path=path, line=line_number, problem="record is not a JSON object"
                )
            try:
                yield model.model_validate(
                    payload
                )  # Full schema validation, including cross-field invariants
            except ArtifactVersionError:  # Raised by the version validator; propagate unchanged so callers can distinguish "too new" from "malformed"
                raise
            except (
                ValidationError
            ) as exc:  # Narrow catch: pydantic validation only, never a blanket `except Exception`
                raise JsonlReadError(
                    path=path,
                    line=line_number,
                    problem="failed schema validation",
                    schema=model.__name__,
                    error_count=exc.error_count(),  # A count is structural; the errors themselves may quote field values, so they are deliberately omitted
                ) from exc


def records_digest(
    records: Iterable[SynapseModel],
) -> str:  # The "corpus SHA-256" recorded in an IndexManifest
    """Order-sensitive SHA-256 over the canonical serialisation of ``records``.

    Independent of compression and file framing, so a corpus that is gzipped,
    decompressed or rewritten still verifies — while any change to record
    *content or order* changes the digest.
    """
    return sha256_records(canonical_record_lines(records))


__all__ = [
    "GZIP_SUFFIX",
    "MAX_LINE_BYTES",
    "JsonlReadError",
    "canonical_record_lines",
    "read_jsonl",
    "records_digest",
    "write_jsonl",
]

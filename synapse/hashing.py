"""
synapse.hashing
===============
SHA-256 helpers for content addressing and artifact integrity.

Three distinct kinds of digest are used by the artifact layer, and conflating
them causes subtle verification bugs, so each has its own function:

  * ``sha256_text``     — digest of *normalised text*. Survives re-serialisation.
  * ``sha256_file``     — digest of *raw file bytes*. Detects any bit flip,
                          including in a binary FAISS index we cannot parse.
  * ``sha256_records``  — digest of an *ordered sequence of records*. Independent
                          of compression and of file framing, so a corpus that is
                          gzipped, un-gzipped, or rewritten with different
                          formatting still verifies. This is what "corpus SHA-256"
                          means in an IndexManifest.

All digests are lowercase hex. All file reads are streamed so a 14 MB FAISS
index is never loaded into memory just to be hashed.
"""

from __future__ import annotations  # Postponed annotations

import hashlib  # Standard library SHA-256; no third-party crypto
from collections.abc import Iterable  # Type hint for the record-sequence digest
from pathlib import Path  # File paths

from synapse.errors import (
    ArtifactNotFoundError,  # Typed error for a missing file, rather than a bare OSError
)
from synapse.normalize import normalize_text  # Text digests are always over normalised text

_READ_CHUNK_BYTES = (
    1024 * 1024
)  # 1 MiB streaming read size; bounds peak memory when hashing large binary artifacts

SHA256_HEX_LENGTH = 64  # A SHA-256 digest is exactly 64 hex characters; used by schema validators to reject malformed digest fields

_RECORD_SEPARATOR = b"\n"  # Explicit separator between records in a sequence digest, so ["ab","c"] and ["a","bc"] cannot collide


def sha256_text(
    text: str,
) -> str:  # Content digest for a chunk body, document body, or any other text field
    """SHA-256 of ``text`` after normalisation, as lowercase hex."""
    normalized = normalize_text(
        text
    )  # Normalise first so cosmetic differences do not change the digest
    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()  # UTF-8 is fixed explicitly; relying on a platform default would make digests machine-dependent


def sha256_bytes(payload: bytes) -> str:  # Digest of an in-memory byte string
    """SHA-256 of raw bytes, as lowercase hex."""
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:  # Digest of a file on disk, streamed
    """SHA-256 of the raw bytes of ``path``, as lowercase hex.

    Raises:
        ArtifactNotFoundError: if the file does not exist.
    """
    if not path.is_file():  # Check explicitly so the caller gets a typed, user-safe error instead of a raw FileNotFoundError
        raise ArtifactNotFoundError(
            path=path
        )  # `path=` is reduced to the basename by the error's sanitiser
    digest = hashlib.sha256()  # Incremental hasher, fed in bounded blocks
    with path.open(
        "rb"
    ) as handle:  # Binary mode: no newline translation, which would corrupt the digest on Windows
        for block in iter(
            lambda: handle.read(_READ_CHUNK_BYTES), b""
        ):  # Read until the sentinel empty-bytes value
            digest.update(block)  # Fold each block into the running digest
    return digest.hexdigest()


def sha256_records(
    records: Iterable[str],
) -> str:  # Order-sensitive digest over a sequence of already-serialised records
    """SHA-256 over an ordered sequence of record strings.

    Order-sensitive by design: reordering a corpus MUST change this digest,
    because FAISS row *i* is bound to corpus record *i*. This is the digest
    that detects the silent-misalignment failure described in
    docs/quality-architecture.md §1.2 (C8).
    """
    digest = hashlib.sha256()
    for record in records:  # Stream rather than materialise: a corpus may be millions of lines
        digest.update(record.encode("utf-8"))  # Fixed encoding, as above
        digest.update(
            _RECORD_SEPARATOR
        )  # Unambiguous framing so record boundaries are part of the digest
    return digest.hexdigest()


def short_digest(
    digest: str, length: int = 12
) -> str:  # Abbreviated digest for human-facing messages and IDs
    """First ``length`` characters of a hex digest, for display and identifier suffixes."""
    return digest[:length]


def is_sha256_hex(
    value: str,
) -> bool:  # Predicate used by pydantic validators on every digest-typed field
    """True when ``value`` looks like a lowercase hex SHA-256 digest."""
    if len(value) != SHA256_HEX_LENGTH:  # Length check first: cheapest rejection
        return False
    return all(
        character in "0123456789abcdef" for character in value
    )  # Lowercase-only, so digests are comparable with `==` without normalisation


__all__ = [
    "SHA256_HEX_LENGTH",
    "is_sha256_hex",
    "sha256_bytes",
    "sha256_file",
    "sha256_records",
    "sha256_text",
    "short_digest",
]

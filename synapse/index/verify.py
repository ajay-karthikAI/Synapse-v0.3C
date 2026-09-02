"""
synapse.index.verify
====================
Fail-closed artifact verification.

Requirement, stated plainly: **an index is verified before it is loaded, and a
mismatched or corrupted artifact is refused with a typed error.** There is no
"warn and continue" path and no fallback, because the failure being guarded
against is silent. A corpus that has drifted from its index still answers
queries; it just cites the wrong paper.

Verification is layered cheapest-first, so a corrupt artifact fails fast:

  1. manifest exists, parses, and is schema-compatible;
  2. the manifest's own digest matches (tamper detection);
  3. every declared artifact exists and its size matches;
  4. every declared artifact's SHA-256 matches;
  5. the FAISS header (when readable) agrees with the manifest's
     dimensionality and vector count;
  6. deep only — every corpus record re-validates, and the recomputed corpus
     and ordering digests match the manifest.

Step 6 is what catches reordering: content digests alone cannot, because a
reordered corpus contains exactly the same records.
"""

from __future__ import annotations  # Postponed annotations

from pathlib import Path

from synapse.corpus.jsonl import read_jsonl, records_digest
from synapse.errors import (
    ArtifactCompatibilityError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
)
from synapse.hashing import sha256_file, sha256_records, short_digest
from synapse.index.manifest import probe_faiss_header, read_manifest
from synapse.schemas.base import SynapseModel
from synapse.schemas.chunk import EvidenceChunk
from synapse.schemas.enums import IndexState
from synapse.schemas.index import IndexManifest

MANIFEST_FILENAME = "manifest.json"  # Fixed name so a caller only ever needs to name the directory

CHUNKS_ROLE = "chunks"  # Manifest role for the corpus chunk file
FAISS_ROLE = "faiss"  # Manifest role for the vector index


class VerificationReport(
    SynapseModel
):  # Returned on success so callers can log what was actually checked
    """What verification examined and confirmed."""

    directory: str  # Artifact directory that was verified
    index_id: str  # Identity of the verified build
    corpus_version: str
    index_state: IndexState
    artifacts_checked: int  # How many files had their digests recomputed
    chunks_validated: int  # How many corpus records were re-validated (0 in shallow mode)
    deep: bool  # Whether the corpus itself was re-read
    faiss_header_checked: bool  # Whether the index header could be probed and compared


def verify_artifacts(directory: Path, *, deep: bool = True) -> VerificationReport:
    """Verify every artifact in ``directory`` against its manifest.

    Args:
        directory: artifact directory containing ``manifest.json``.
        deep: when True (the default), re-read and re-validate every corpus
            record and recompute the corpus and ordering digests. Shallow mode
            checks file digests only and is intended for hot paths where the
            corpus has already been validated in the same process.

    Returns:
        A :class:`VerificationReport` describing what was checked.

    Raises:
        ArtifactNotFoundError: a declared artifact is missing.
        ArtifactSchemaError: the manifest or a record is malformed.
        ArtifactVersionError: the manifest or a record is schema-incompatible.
        ArtifactIntegrityError: a digest or size does not match.
        ArtifactCompatibilityError: the corpus and index do not correspond.
    """
    manifest = read_manifest(
        directory / MANIFEST_FILENAME
    )  # Steps 1 and 2: parse, validate, version-check
    _verify_manifest_self_digest(manifest, directory)
    artifacts_checked = _verify_declared_artifacts(manifest, directory)  # Steps 3 and 4
    faiss_checked = _verify_faiss_header(manifest, directory)  # Step 5

    chunks_validated = 0  # Stays zero in shallow mode, and the report says so
    if deep:
        chunks_validated = _verify_corpus_digests(manifest, directory)  # Step 6

    return VerificationReport(
        directory=str(directory),
        index_id=manifest.index_id,
        corpus_version=manifest.corpus_version,
        index_state=manifest.index_state,
        artifacts_checked=artifacts_checked,
        chunks_validated=chunks_validated,
        deep=deep,
        faiss_header_checked=faiss_checked,
    )


def _verify_manifest_self_digest(manifest: IndexManifest, directory: Path) -> None:
    """Confirm the manifest has not been edited since it was written."""
    if (
        manifest.manifest_sha256 is None
    ):  # A manifest written by an older tool may predate self-hashing; absence is not corruption
        return
    recomputed = (
        manifest.compute_self_digest()
    )  # Recompute over every field except the digest itself
    if recomputed != manifest.manifest_sha256:  # Any edited field changes this
        raise ArtifactIntegrityError(
            artifact=MANIFEST_FILENAME,
            directory=directory,
            expected=short_digest(
                manifest.manifest_sha256
            ),  # Digests are truncated in messages: enough to diagnose, not a full secret-shaped string in a log
            actual=short_digest(recomputed),
        )


def _verify_declared_artifacts(manifest: IndexManifest, directory: Path) -> int:
    """Confirm every declared file exists, is the declared size, and hashes correctly."""
    checked = 0
    for artifact in (
        manifest.artifacts
    ):  # Paths were validated as relative and non-traversing by the schema, so joining them is safe
        target = directory / artifact.path
        if (
            not target.is_file()
        ):  # A declared artifact that is absent is a hard failure, never a skip
            raise ArtifactNotFoundError(artifact=artifact.role, path=target)
        actual_size = (
            target.stat().st_size
        )  # Size is checked before hashing: it is O(1) and catches truncation immediately
        if actual_size != artifact.size_bytes:
            raise ArtifactIntegrityError(
                artifact=artifact.role,
                problem="size mismatch",
                expected=artifact.size_bytes,
                actual=actual_size,
            )
        actual_digest = sha256_file(target)  # Full-content digest
        if actual_digest != artifact.sha256:
            raise ArtifactIntegrityError(
                artifact=artifact.role,
                problem="digest mismatch",
                expected=short_digest(artifact.sha256),
                actual=short_digest(actual_digest),
            )
        checked += 1
    return checked


def _verify_faiss_header(manifest: IndexManifest, directory: Path) -> bool:
    """Cross-check the FAISS header against the manifest, when both are available.

    Returns True when the check ran. An index type whose header we cannot read
    yields False and is not an error — the file digest has already been
    verified, so the artifact is still known to be intact.
    """
    if manifest.index_state is not IndexState.READY:  # Nothing to check when no index is present
        return False
    artifact = manifest.artifact_for(FAISS_ROLE)
    if (
        artifact is None
    ):  # A ready manifest with no faiss artifact declared is internally inconsistent
        raise ArtifactCompatibilityError(
            problem="index_state is 'ready' but no faiss artifact is declared",
            index_id=manifest.index_id,
        )
    header = probe_faiss_header(directory / artifact.path)  # Best effort; None means "cannot check"
    if header is None:
        return False
    if (
        header.dimensions != manifest.embedding.dimensions
    ):  # A dimension mismatch means the index was built with a different embedding model
        raise ArtifactCompatibilityError(
            problem="embedding dimensionality mismatch",
            manifest_dimensions=manifest.embedding.dimensions,
            index_dimensions=header.dimensions,
        )
    if (
        header.vector_count != manifest.chunk_count
    ):  # One vector per chunk, or the row-to-chunk mapping is broken
        raise ArtifactCompatibilityError(
            problem="vector count does not match chunk count",
            chunk_count=manifest.chunk_count,
            vector_count=header.vector_count,
        )
    return True


def _verify_corpus_digests(manifest: IndexManifest, directory: Path) -> int:
    """Re-read the corpus and confirm both its content and its ordering."""
    artifact = manifest.artifact_for(CHUNKS_ROLE)
    if artifact is None:  # Without a corpus there is nothing to align an index to
        raise ArtifactCompatibilityError(
            problem="manifest declares no 'chunks' artifact", index_id=manifest.index_id
        )

    chunks = list(
        read_jsonl(directory / artifact.path, EvidenceChunk)
    )  # Every record is re-validated here, including its own content hash

    if len(chunks) != manifest.chunk_count:  # A count mismatch means records were added or lost
        raise ArtifactIntegrityError(
            artifact=CHUNKS_ROLE,
            problem="chunk count mismatch",
            expected=manifest.chunk_count,
            actual=len(chunks),
        )

    actual_corpus_digest = records_digest(chunks)  # Content digest over the canonical serialisation
    if actual_corpus_digest != manifest.corpus_sha256:
        raise ArtifactIntegrityError(
            artifact=CHUNKS_ROLE,
            problem="corpus digest mismatch",
            expected=short_digest(manifest.corpus_sha256),
            actual=short_digest(actual_corpus_digest),
        )

    actual_ordering_digest = sha256_records(
        chunk.chunk_id for chunk in chunks
    )  # THE ordering check: this is what a reordered corpus fails
    if actual_ordering_digest != manifest.chunk_ids_sha256:
        raise ArtifactCompatibilityError(
            problem="chunk ordering does not match the manifest; FAISS rows would be misaligned with corpus records",
            expected=short_digest(manifest.chunk_ids_sha256),
            actual=short_digest(actual_ordering_digest),
        )

    return len(chunks)


def load_verified_corpus(
    directory: Path, *, deep: bool = True
) -> tuple[IndexManifest, list[EvidenceChunk]]:
    """Verify ``directory`` and return its manifest and chunks.

    This is the ONLY supported way to obtain corpus chunks for serving. It is
    impossible to reach the chunks without verification having passed, which is
    the point: correctness here cannot depend on a caller remembering to check.
    """
    report = verify_artifacts(
        directory, deep=deep
    )  # Raises on any failure; nothing below runs unless verification passed
    manifest = read_manifest(directory / MANIFEST_FILENAME)
    artifact = manifest.artifact_for(CHUNKS_ROLE)
    if (
        artifact is None
    ):  # Already guaranteed in deep mode; re-checked so shallow mode is equally safe
        raise ArtifactCompatibilityError(
            problem="manifest declares no 'chunks' artifact", index_id=report.index_id
        )
    chunks = list(
        read_jsonl(directory / artifact.path, EvidenceChunk)
    )  # Validated on read, as always
    return manifest, chunks


__all__ = [
    "CHUNKS_ROLE",
    "FAISS_ROLE",
    "MANIFEST_FILENAME",
    "VerificationReport",
    "load_verified_corpus",
    "verify_artifacts",
]

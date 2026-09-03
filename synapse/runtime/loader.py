"""
synapse.runtime.loader
======================
Get one verified artifact onto local disk, or serve nothing.

The deployed container does not build an index and does not read a pickle. It
resolves exactly one archive — named by version, pinned by digest — and every
step between object storage and a servable directory is a gate:

    configured? -> cached and valid? -> download -> archive digest
        -> safe extract -> deep manifest verification -> counts and ordering

There is no branch that continues past a failed gate. That is the entire design:
a partially-verified artifact is indistinguishable from a verified one once it
is in the directory the backends read from, so nothing is ever *put* there until
it has passed everything. Extraction stages into a temporary directory and only
a fully-verified tree is moved into the cache, atomically.

Why the cache is safe to trust
------------------------------
A cached artifact is reused only when its directory name matches the configured
version **and** digest, a marker file records the same digest, and deep
verification passes again on every startup. Re-verifying a cache we ourselves
wrote may look redundant; it is not. The disk is persistent and outlives the
process, so between two starts the bytes can change through a failed write, a
disk fault, or anything with filesystem access. Verification is cheap relative
to a container start, and the failure it prevents is a citation pointing at a
document nobody read.

What is deliberately absent
---------------------------
Any fallback to ``hybrid_index/``. That directory carries no manifest, so there
is nothing to verify it against; falling back to it under failure would mean the
system's response to "I cannot prove this index is intact" is to serve an index
it cannot prove anything about. :func:`load_artifact` raises instead, readiness
stays false, and no turn is attempted.
"""

from __future__ import annotations  # Postponed annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from synapse.errors import ArtifactCompatibilityError
from synapse.hashing import short_digest
from synapse.index.manifest import read_manifest
from synapse.index.verify import MANIFEST_FILENAME, VerificationReport, verify_artifacts
from synapse.logging import get_logger
from synapse.runtime.archive import (
    replace_directory,
    safe_extract,
    verify_archive_digest,
)
from synapse.runtime.config import RuntimeArtifactConfig
from synapse.runtime.objectstore import ObjectStore
from synapse.schemas.enums import IndexState
from synapse.schemas.index import IndexManifest

logger = get_logger(__name__)

# Records which archive produced a cached directory. Read as a cheap
# pre-check before the expensive deep verification, never as a substitute for
# it.
MARKER_FILENAME = ".synapse-archive-sha256"


@dataclass(frozen=True)
class LoadedArtifact:
    """A directory that has passed every gate, and what proved it."""

    directory: Path
    manifest: IndexManifest
    report: VerificationReport
    from_cache: bool

    @property
    def chunk_count(self) -> int:
        """Number of corpus records, as verified."""
        return self.manifest.chunk_count

    def describe(self) -> dict[str, object]:
        """Structural facts for a readiness probe. No paths outside the cache."""
        return {
            "index_id": self.manifest.index_id,
            "index_version": self.manifest.index_version,
            "corpus_version": self.manifest.corpus_version,
            "chunk_count": self.manifest.chunk_count,
            "vector_count": self.manifest.vector_count,
            "chunks_validated": self.report.chunks_validated,
            "faiss_header_checked": self.report.faiss_header_checked,
            "from_cache": self.from_cache,
        }


def load_artifact(config: RuntimeArtifactConfig, store: ObjectStore) -> LoadedArtifact:
    """Resolve, verify and return the artifact this deployment serves.

    Raises:
        ArtifactConfigurationError: the deployment named no artifact.
        ArtifactNotFoundError: the archive is not in the bucket.
        ArtifactIntegrityError: the archive digest, a file digest, the manifest
            self-digest, or the corpus digest did not match; or the archive
            contained an unsafe member.
        ArtifactCompatibilityError: the corpus and index do not correspond —
            wrong dimensionality, wrong vector count, or a reordered corpus.
        ArtifactVersionError / ArtifactSchemaError: the manifest or a record is
            malformed or from an incompatible schema generation.
    """
    config.validate()

    cached = _use_cached(config)
    if cached is not None:
        return cached

    return _download_and_verify(config, store)


def _use_cached(config: RuntimeArtifactConfig) -> LoadedArtifact | None:
    """Return the cached artifact if it is present, matching and valid.

    Returns ``None`` — never raises — when there is no usable cache, because
    "the cache is unusable" is a reason to download, not a reason to fail. A
    genuinely corrupt artifact still fails closed: the download that follows
    ends in the same verification, and it is that verification which decides.
    """
    directory = config.artifact_dir
    if not (directory / MANIFEST_FILENAME).is_file():
        return None

    marker = directory / MARKER_FILENAME
    if not marker.is_file():
        logger.info("cached artifact has no provenance marker; re-downloading")
        return None
    try:
        recorded = marker.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return None
    if recorded != config.archive_sha256:
        # The directory name already encodes the digest, so this should be
        # unreachable. It is checked anyway: a cache whose name and contents
        # disagree is exactly the state a partial write leaves behind.
        logger.warning("cached artifact digest marker does not match configuration")
        return None

    try:
        report = verify_artifacts(directory, deep=True)
        manifest = read_manifest(directory / MANIFEST_FILENAME)
        _verify_counts_and_state(manifest, directory)
    except Exception as exc:
        # Broad on purpose: whatever is wrong with the cache, the response is
        # the same — do not serve it, fetch a fresh copy, and let the fresh
        # copy's verification decide whether this process can serve at all.
        logger.warning(
            "cached artifact failed verification; re-downloading",
            extra={"error": type(exc).__name__},
        )
        return None

    logger.info("serving verified cached artifact", extra=config.describe())
    return LoadedArtifact(directory=directory, manifest=manifest, report=report, from_cache=True)


def _download_and_verify(config: RuntimeArtifactConfig, store: ObjectStore) -> LoadedArtifact:
    """Fetch, verify and install the artifact. Every failure propagates."""
    config.download_dir.mkdir(parents=True, exist_ok=True)
    # Staged inside the cache root so the final rename is same-filesystem, and
    # therefore atomic. A cross-device rename would silently become a copy,
    # which is not.
    staging = Path(tempfile.mkdtemp(dir=config.download_dir, prefix="fetch-"))
    try:
        archive = staging / "archive.tar.gz"
        store.download(config.bucket, config.key, archive)

        # Gate 1: the bytes are the bytes we pinned. Checked BEFORE the tar
        # stream is parsed, so a hostile archive is never opened.
        verify_archive_digest(archive, config.archive_sha256)

        # Gate 2: extraction refuses traversal, links, unexpected members and
        # decompression bombs.
        extracted = staging / "artifact"
        safe_extract(archive, extracted)
        archive.unlink(missing_ok=True)  # Reclaim the space before verifying

        # Gate 3: full manifest verification, including the ordering digest
        # that binds FAISS row i to corpus record i.
        report = verify_artifacts(extracted, deep=True)
        manifest = read_manifest(extracted / MANIFEST_FILENAME)
        _verify_counts_and_state(manifest, extracted)

        (extracted / MARKER_FILENAME).write_text(config.archive_sha256, encoding="utf-8")

        # Only now does anything reach the path the backends read from.
        replace_directory(extracted, config.artifact_dir)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    logger.info("runtime artifact installed", extra=config.describe())
    return LoadedArtifact(
        directory=config.artifact_dir, manifest=manifest, report=report, from_cache=False
    )


def _verify_counts_and_state(manifest: IndexManifest, directory: Path) -> None:
    """Confirm the manifest describes a servable index, and that its counts agree.

    :func:`~synapse.index.verify.verify_artifacts` already compares the FAISS
    *header* against ``chunk_count`` — but only when the header could be
    probed, and ``probe_faiss_header`` returns ``None`` for any index type whose
    layout it does not recognise. These checks use the manifest's own declared
    counts, so they hold whatever the index type is.
    """
    if manifest.index_state is not IndexState.READY:
        raise ArtifactCompatibilityError(
            problem="artifact is not servable",
            index_state=manifest.index_state.value,
            directory=directory,
        )
    if manifest.chunk_count <= 0:
        raise ArtifactCompatibilityError(
            problem="artifact declares an empty corpus", directory=directory
        )
    if manifest.vector_count is not None and manifest.vector_count != manifest.chunk_count:
        # One vector per chunk, or row i does not correspond to record i.
        raise ArtifactCompatibilityError(
            problem="declared vector count does not match chunk count",
            chunk_count=manifest.chunk_count,
            vector_count=manifest.vector_count,
        )
    if manifest.index_sha256 is None:
        raise ArtifactCompatibilityError(
            problem="a ready manifest must record the index digest", directory=directory
        )
    if manifest.source_pack_version is None:
        # An index that cannot name the pack that authorised it is unservable:
        # governance would have nothing to check the served documents against.
        raise ArtifactCompatibilityError(
            problem="artifact does not name an authorising source pack", directory=directory
        )


def verify_directory(directory: Path) -> tuple[IndexManifest, VerificationReport]:
    """Run the full artifact check over a directory already on disk.

    The path the packaging CLI uses before building an archive, so an archive is
    never produced from an artifact that would fail on the way in.
    """
    report = verify_artifacts(directory, deep=True)
    manifest = read_manifest(directory / MANIFEST_FILENAME)
    _verify_counts_and_state(manifest, directory)
    logger.info(
        "artifact directory verified",
        extra={
            "index_id": manifest.index_id,
            "chunks_validated": report.chunks_validated,
            "corpus_sha256": short_digest(manifest.corpus_sha256),
        },
    )
    return manifest, report


__all__ = [
    "MARKER_FILENAME",
    "LoadedArtifact",
    "load_artifact",
    "verify_directory",
]

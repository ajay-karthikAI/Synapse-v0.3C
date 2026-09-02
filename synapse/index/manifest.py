"""
synapse.index.manifest
======================
Construction, reading and writing of :class:`IndexManifest`.

Also contains :func:`probe_faiss_header`, which reads a FAISS index's
dimensionality and vector count directly from the file header. That matters
because it lets CI verify corpus/index alignment **without faiss-cpu
installed** — the integrity gate stays runnable in a minimal environment. The
probe is strictly best-effort: an unrecognised file yields ``None`` and the
verifier simply skips that check, so an unfamiliar FAISS build can never cause
a false failure.
"""

from __future__ import annotations  # Postponed annotations

import json  # Manifests are JSON; parsed with json.loads, never eval or a YAML loader
import platform  # Recorded in the manifest so a build can be traced to an environment
import subprocess  # Used ONLY to ask git for the current commit, with a fixed argument list and no shell
import sys  # Python version for the builder record
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from synapse import __version__ as synapse_version
from synapse.corpus.jsonl import canonical_record_lines, records_digest
from synapse.errors import ArtifactNotFoundError, ArtifactSchemaError
from synapse.hashing import sha256_file, sha256_records
from synapse.schemas.base import SynapseModel
from synapse.schemas.chunk import EvidenceChunk
from synapse.schemas.enums import DistanceMetric, IndexState
from synapse.schemas.index import ChunkingConfig, EmbeddingConfig, IndexManifest, ManifestArtifact

# ---------------------------------------------------------------------------
# FAISS header probe
# ---------------------------------------------------------------------------

_FAISS_HEADER_BYTES = 16  # fourcc (4) + dimensionality (int32) + vector count (int64)

_KNOWN_FLAT_FOURCC = (
    b"IxF2",
    b"IxFI",
    b"IxFl",
)  # IndexFlatL2, IndexFlatIP, generic IndexFlat — the layouts whose header offsets are stable and known


class FaissHeader(
    SynapseModel
):  # Small value object rather than a bare tuple, so callers read fields by name
    """Dimensionality and vector count read from a FAISS index header."""

    fourcc: str  # The four-character index type code
    dimensions: int  # Vector dimensionality recorded in the file
    vector_count: int  # Number of vectors recorded in the file


def probe_faiss_header(path: Path) -> FaissHeader | None:
    """Best-effort read of a FAISS index header.

    Returns ``None`` — never raises — when the file is too short or its type
    code is not one whose header layout we know. Verification treats ``None``
    as "cannot check", not as "failed", so an unfamiliar index type degrades to
    a plain file-digest check rather than a spurious error.
    """
    if not path.is_file():  # Missing file is the verifier's problem to report, not this probe's
        return None
    with path.open("rb") as handle:
        header = handle.read(
            _FAISS_HEADER_BYTES
        )  # Read only the header; the body may be many megabytes
    if len(header) < _FAISS_HEADER_BYTES:  # Truncated file: nothing reliable to read
        return None
    fourcc = header[0:4]  # First four bytes identify the index type
    if fourcc not in _KNOWN_FLAT_FOURCC:  # Unknown layout: decline rather than guess at offsets
        return None
    dimensions = int.from_bytes(
        header[4:8], byteorder="little", signed=True
    )  # d is a little-endian int32
    vector_count = int.from_bytes(
        header[8:16], byteorder="little", signed=True
    )  # ntotal is a little-endian int64
    if (
        dimensions <= 0 or vector_count < 0
    ):  # Implausible values mean the layout assumption is wrong; decline rather than report nonsense
        return None
    return FaissHeader(
        fourcc=fourcc.decode("ascii"), dimensions=dimensions, vector_count=vector_count
    )


# ---------------------------------------------------------------------------
# Build provenance
# ---------------------------------------------------------------------------


def detect_git_commit(repository_root: Path) -> str | None:
    """Return the current git commit, or ``None`` when it cannot be determined.

    Never fabricates a value. The working tree may legitimately not be a git
    repository — the Synapse tree currently is not — and recording ``None`` is
    the honest outcome.
    """
    try:
        completed = subprocess.run(  # Fixed argument list, shell=False, no user-controlled input: this is not a shell-injection surface
            ["git", "rev-parse", "HEAD"],  # noqa: S607  # Resolved via PATH by design, so it works in a devcontainer and on a developer machine alike
            cwd=repository_root,
            capture_output=True,
            text=True,
            timeout=5,  # Bound the call so a hung git can never block a build
            check=False,  # Handle a non-zero exit ourselves rather than raising
        )
    except (
        OSError,
        subprocess.SubprocessError,
    ):  # git absent, not executable, or timed out — all mean "unknown", not "error"
        return None
    if completed.returncode != 0:  # Not a git repository, or a detached/empty HEAD
        return None
    commit = completed.stdout.strip()
    return commit or None  # Empty output is treated as unknown rather than stored as ""


def _builder_environment() -> dict[
    str, str
]:  # Recorded so an artifact can be traced to the environment that produced it
    """Describe the environment producing this build."""
    return {
        "synapse": synapse_version,  # Version of this package
        "python": sys.version.split()[0],  # Interpreter version, e.g. '3.11.15'
        "platform": platform.platform(terse=True),  # OS/architecture summary
    }


# ---------------------------------------------------------------------------
# Manifest construction
# ---------------------------------------------------------------------------


def build_index_manifest(
    *,
    index_id: str,
    index_version: str,
    corpus_version: str,
    chunks: Sequence[EvidenceChunk],
    artifacts: Sequence[ManifestArtifact],
    embedding: EmbeddingConfig,
    chunking: ChunkingConfig,
    distance_metric: DistanceMetric,
    document_count: int,
    faiss_path: Path | None = None,
    faiss_index_type: str | None = None,
    source_pack_version: str | None = None,
    source_pack_sha256: str
    | None = None,  # Digest of the source pack that authorised this index; checked against the live pack before serving
    repository_root: Path | None = None,
    built_at: datetime | None = None,
) -> IndexManifest:
    """Assemble a fully-populated, self-hashed :class:`IndexManifest`.

    The two digests computed here are the ones that make verification
    meaningful:

      * ``corpus_sha256``   — over the canonical serialisation of every chunk;
      * ``chunk_ids_sha256`` — over the chunk identifiers *in order*, which is
        what binds FAISS row *i* to corpus record *i*.
    """
    corpus_sha256 = records_digest(chunks)  # Content digest: any edit to any chunk changes it
    chunk_ids_sha256 = sha256_records(
        chunk.chunk_id for chunk in chunks
    )  # Ordering digest: any reordering changes it, even if content is identical

    index_state = (
        IndexState.READY if faiss_path is not None else IndexState.REBUILD_REQUIRED
    )  # No index file means the corpus cannot be served yet, and the manifest says so explicitly
    index_sha256: str | None = None  # Populated only when an index is present
    vector_count: int | None = None
    if faiss_path is not None:
        index_sha256 = sha256_file(faiss_path)  # Raw-bytes digest of the binary index
        header = probe_faiss_header(faiss_path)  # Best-effort structural read
        vector_count = (
            header.vector_count if header is not None else len(chunks)
        )  # Fall back to the chunk count when the header cannot be read; the model then enforces they match

    manifest = IndexManifest(
        index_id=index_id,
        index_version=index_version,
        corpus_version=corpus_version,
        source_pack_version=source_pack_version,
        source_pack_sha256=source_pack_sha256,
        corpus_sha256=corpus_sha256,
        index_sha256=index_sha256,
        chunk_ids_sha256=chunk_ids_sha256,
        artifacts=list(artifacts),
        embedding=embedding,
        chunking=chunking,
        distance_metric=distance_metric,
        vectors_l2_normalized=embedding.l2_normalized,  # Mirrors the embedding configuration so the loader can check it without reaching into a nested object
        faiss_index_type=faiss_index_type,
        document_count=document_count,
        chunk_count=len(chunks),
        vector_count=vector_count,
        index_state=index_state,
        built_at=built_at
        or datetime.now(UTC),  # Timezone-aware by construction; the model rejects naive timestamps
        git_commit=detect_git_commit(repository_root) if repository_root is not None else None,
        builder=_builder_environment(),
    )
    return (
        manifest.with_self_digest()
    )  # Stamp the manifest with its own digest last, so tampering with any field is detectable


def make_artifact(
    path: Path, role: str, *, relative_to: Path
) -> ManifestArtifact:  # Helper used by the CLI when assembling the artifact list
    """Describe a file on disk as a :class:`ManifestArtifact`."""
    if not path.is_file():
        raise ArtifactNotFoundError(path=path, role=role)
    return ManifestArtifact(
        role=role,
        path=path.relative_to(
            relative_to
        ).as_posix(),  # Stored relative and POSIX-style so a manifest is portable across platforms
        sha256=sha256_file(path),
        size_bytes=path.stat().st_size,
    )


# ---------------------------------------------------------------------------
# Manifest I/O
# ---------------------------------------------------------------------------


def write_manifest(
    path: Path, manifest: IndexManifest
) -> None:  # Single writer, so formatting stays consistent
    """Write ``manifest`` to ``path`` as indented JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = manifest.model_dump(mode="json")  # mode="json" renders datetimes and enums as strings
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )  # Indented and key-sorted so a manifest diff is readable in review


def read_manifest(path: Path) -> IndexManifest:  # Single reader, so validation cannot be bypassed
    """Read and validate a manifest.

    Raises:
        ArtifactNotFoundError: the manifest does not exist.
        ArtifactSchemaError: the manifest is not valid JSON, or fails validation.
        ArtifactVersionError: the manifest declares an incompatible schema_version.
    """
    if not path.is_file():
        raise ArtifactNotFoundError(path=path)
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8")
        )  # SAFE: json.loads cannot execute code or construct arbitrary objects
    except json.JSONDecodeError as exc:  # Narrow catch: syntax only
        raise ArtifactSchemaError(path=path, problem="manifest is not valid JSON") from exc
    if not isinstance(payload, dict):  # A JSON array or scalar is not a manifest
        raise ArtifactSchemaError(path=path, problem="manifest is not a JSON object")
    try:
        return IndexManifest.model_validate(
            payload
        )  # Full validation, including the state-consistency invariants
    except ValidationError as exc:  # Narrow catch: pydantic only, never a blanket except
        raise ArtifactSchemaError(
            path=path, problem="manifest failed schema validation", error_count=exc.error_count()
        ) from exc


__all__ = [
    "FaissHeader",
    "build_index_manifest",
    "canonical_record_lines",
    "detect_git_commit",
    "make_artifact",
    "probe_faiss_header",
    "read_manifest",
    "write_manifest",
]

"""
synapse.schemas.index
=====================
:class:`IndexManifest` — the record that makes an index verifiable.

The current system has no manifest at all. ``HybridRetriever.load()`` reads
whatever files happen to be on disk and trusts that the BM25 chunk list, the
vector store's chunk list and the FAISS row order still correspond. Nothing
checks it. A partial rebuild therefore produces answers that look completely
normal while citing the wrong document (docs/quality-architecture.md §1.2, C8).

This manifest records everything needed to detect that before a single query
runs: file digests, the corpus digest, the *ordering* digest that binds FAISS
row *i* to corpus record *i*, the embedding model and dimensionality, the
chunking configuration, the distance metric and normalisation, the build
timestamp, and the source commit when one is available.

Security note: a manifest is untrusted input. ``ManifestArtifact.path``
therefore rejects absolute paths, parent-directory traversal and Windows
drive prefixes — otherwise a hostile manifest could direct hashing or loading
at an arbitrary file outside the artifact directory.
"""

from __future__ import annotations  # Postponed annotations

import json  # Canonical serialisation for the self-digest
from datetime import datetime
from pathlib import (
    PurePosixPath,  # Pure path manipulation with no filesystem access, for validating manifest-supplied paths
)
from typing import ClassVar

from pydantic import Field, field_validator, model_validator

from synapse.hashing import is_sha256_hex, sha256_bytes
from synapse.schemas.base import SynapseModel, VersionedModel, require_timezone_aware
from synapse.schemas.enums import ChunkingAlgorithm, DistanceMetric, IndexState

_SELF_DIGEST_FIELD = "manifest_sha256"  # Excluded when the manifest hashes itself, otherwise the digest would depend on its own value


class ManifestArtifact(SynapseModel):  # One file referenced by a manifest
    """A single artifact file, its role, and its digest."""

    role: str = Field(
        min_length=1, description="What this file is, e.g. 'chunks', 'documents', 'faiss'."
    )
    path: str = Field(
        min_length=1,
        description="Path RELATIVE to the artifact directory. Never absolute, never traversing upward.",
    )
    sha256: str = Field(description="SHA-256 of the raw file bytes.")
    size_bytes: int = Field(
        ge=0,
        description="File size, recorded so a truncated file is detected even before hashing completes.",
    )

    @field_validator("path")
    @classmethod
    def _validate_path_is_contained(cls, value: str) -> str:
        """SECURITY: reject any path that could escape the artifact directory.

        A manifest arrives from disk and is therefore untrusted. Without this
        check, ``"../../.ssh/id_rsa"`` or ``"/etc/passwd"`` in a manifest would
        be dutifully opened and hashed by the verifier.
        """
        if value.startswith("/") or value.startswith("\\"):  # Absolute POSIX or UNC path
            raise ValueError("artifact path must be relative to the artifact directory")
        if ":" in value.split("/")[0]:  # Windows drive prefix such as 'C:'
            raise ValueError("artifact path must not contain a drive prefix")
        parts = PurePosixPath(value).parts  # Decompose without touching the filesystem
        if ".." in parts:  # Parent traversal at any depth
            raise ValueError("artifact path must not contain '..'")
        if not parts:  # An empty path resolves to the directory itself
            raise ValueError("artifact path must not be empty")
        return value

    @field_validator("sha256")
    @classmethod
    def _validate_digest_shape(cls, value: str) -> str:
        """Reject digests that are not lowercase 64-character hex."""
        if not is_sha256_hex(value):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return value


class EmbeddingConfig(SynapseModel):  # Requirement 6: embedding model and dimensions
    """Embedding model configuration used to build the vector index."""

    provider: str = Field(min_length=1, description="Embedding provider, e.g. 'openai'.")
    model: str = Field(
        min_length=1, description="Exact model identifier, e.g. 'text-embedding-3-small'."
    )
    dimensions: int = Field(
        gt=0, description="Vector dimensionality. Checked against the FAISS index at load."
    )
    l2_normalized: bool = Field(
        description="Whether vectors were L2-normalised before indexing. With L2 distance this is what makes ranking equivalent to cosine."
    )
    batch_size: int | None = Field(
        default=None, gt=0, description="Embedding request batch size used at build time."
    )


class ChunkingConfig(SynapseModel):  # Requirement 6: chunking algorithm and configuration
    """Chunking configuration used to produce the corpus."""

    algorithm: ChunkingAlgorithm = Field(description="Which splitter produced these chunks.")
    chunk_size: int = Field(
        gt=0, description="Target maximum chunk size, in the algorithm's own units."
    )
    overlap: int = Field(ge=0, description="Overlap between consecutive chunks.")
    min_chunk_chars: int = Field(
        ge=0,
        description="Minimum retained chunk length; chunks below this were dropped at build time.",
    )
    separators_sha256: str | None = Field(
        default=None,
        description="Digest of the separator list, so a changed separator set is detectable.",
    )
    tokenizer_id: str | None = Field(
        default=None, description="Versioned tokenizer identifier used for token counts and BM25."
    )

    @model_validator(mode="after")
    def _validate_overlap(self) -> ChunkingConfig:
        """Overlap must be smaller than chunk size, or chunking cannot make progress."""
        if self.overlap >= self.chunk_size:
            raise ValueError("overlap must be smaller than chunk_size")
        return self


class IndexManifest(VersionedModel):
    """Everything needed to verify an index before loading it."""

    SCHEMA_NAME: ClassVar[str] = "index_manifest"
    SCHEMA_VERSION: ClassVar[str] = (
        "1.1"  # 1.1 adds source_pack_sha256; optional, so 1.0 manifests still load
    )

    # -- identity -----------------------------------------------------------
    index_id: str = Field(min_length=1, description="Stable identifier for this build.")
    index_version: str = Field(
        min_length=1, description="Version of the index itself, incremented per rebuild."
    )
    corpus_version: str = Field(
        min_length=1, description="Version of the corpus this index was built from."
    )
    source_pack_version: str | None = Field(
        default=None,
        description="Source pack that authorised this index, as '<pack_id>@<semver>'. An index that cannot name its authorising pack is unservable.",
    )
    source_pack_sha256: str | None = Field(
        default=None,
        description="The pack's corpus digest at build time. Compared against the live pack before serving, so a pack edited after the build is detected.",
    )

    # -- integrity ----------------------------------------------------------
    corpus_sha256: str = Field(
        description="Order-sensitive digest over the serialised corpus records. Compression-independent."
    )
    index_sha256: str | None = Field(
        default=None,
        description="SHA-256 of the raw FAISS index bytes. Null only when index_state is 'rebuild_required'.",
    )
    chunk_ids_sha256: str = Field(
        description="Order-sensitive digest over chunk identifiers. Binds FAISS row i to corpus record i — the check that catches silent misalignment."
    )
    artifacts: list[ManifestArtifact] = Field(
        default_factory=list, description="Every file this manifest governs, with digests."
    )

    # -- build configuration ------------------------------------------------
    embedding: EmbeddingConfig = Field(description="Embedding model and dimensionality.")
    chunking: ChunkingConfig = Field(description="Chunking algorithm and parameters.")
    distance_metric: DistanceMetric = Field(
        description="Distance function used by the vector index."
    )
    vectors_l2_normalized: bool = Field(
        description="Whether the indexed vectors are L2-normalised."
    )
    faiss_index_type: str | None = Field(
        default=None, description="FAISS index class, e.g. 'IndexFlatL2'."
    )

    # -- counts -------------------------------------------------------------
    document_count: int = Field(ge=0, description="Number of source documents represented.")
    chunk_count: int = Field(ge=0, description="Number of chunks in the corpus.")
    vector_count: int | None = Field(
        default=None,
        ge=0,
        description="Number of vectors in the FAISS index; must equal chunk_count when the index is ready.",
    )

    # -- provenance ---------------------------------------------------------
    index_state: IndexState = Field(
        default=IndexState.READY, description="Whether a usable index exists."
    )
    built_at: datetime = Field(description="Build timestamp (timezone-aware).")
    git_commit: str | None = Field(
        default=None,
        description="Source commit when the build ran inside a git checkout; null otherwise. Never fabricated.",
    )
    builder: dict[str, str] = Field(
        default_factory=dict,
        description="Build environment: synapse version, python version, platform.",
    )

    manifest_sha256: str | None = Field(
        default=None,
        description="Digest of this manifest with this field excluded; detects manifest tampering.",
    )

    @field_validator("corpus_sha256", "chunk_ids_sha256")
    @classmethod
    def _validate_required_digests(cls, value: str) -> str:
        """Reject digests that are not lowercase 64-character hex."""
        if not is_sha256_hex(value):
            raise ValueError("digest must be 64 lowercase hexadecimal characters")
        return value

    @field_validator("index_sha256", "manifest_sha256", "source_pack_sha256")
    @classmethod
    def _validate_optional_digests(cls, value: str | None) -> str | None:
        """Same shape check, but the field may legitimately be absent."""
        if value is not None and not is_sha256_hex(value):
            raise ValueError("digest must be 64 lowercase hexadecimal characters")
        return value

    @model_validator(mode="after")
    def _validate_state_consistency(self) -> IndexManifest:
        """Enforce that the declared state matches the fields actually present."""
        if self.index_state is IndexState.READY:  # A ready index must be fully described
            if self.index_sha256 is None:  # Without a digest there is nothing to verify against
                raise ValueError("index_state 'ready' requires index_sha256")
            if (
                self.vector_count is None
            ):  # Without a vector count, corpus/index alignment cannot be checked
                raise ValueError("index_state 'ready' requires vector_count")
            if (
                self.vector_count != self.chunk_count
            ):  # THE alignment invariant: one vector per chunk, or the mapping is broken
                raise ValueError("vector_count must equal chunk_count when index_state is 'ready'")
        else:  # REBUILD_REQUIRED: the corpus exists but no index does
            if (
                self.index_sha256 is not None
            ):  # Claiming a digest for an index that is not ready is contradictory
                raise ValueError("index_state 'rebuild_required' must not carry index_sha256")
            if self.vector_count is not None:
                raise ValueError("index_state 'rebuild_required' must not carry vector_count")
        require_timezone_aware(self.built_at, "built_at")
        roles = [
            artifact.role for artifact in self.artifacts
        ]  # Duplicate roles would make lookup ambiguous
        if len(roles) != len(set(roles)):
            raise ValueError("artifact roles must be unique within a manifest")
        return self

    def artifact_for(
        self, role: str
    ) -> ManifestArtifact | None:  # Lookup helper used by the verifier
        """Return the artifact with the given role, or None."""
        for artifact in self.artifacts:  # Linear scan is fine: a manifest holds a handful of files
            if artifact.role == role:
                return artifact
        return None

    def compute_self_digest(self) -> str:  # Self-hash, excluding the field that holds it
        """SHA-256 over this manifest's canonical JSON, with ``manifest_sha256`` excluded.

        Excluding the field is what makes the digest well-defined: including it
        would require the value to hash to itself.
        """
        payload = self.model_dump(
            mode="json", exclude={_SELF_DIGEST_FIELD}
        )  # mode="json" renders datetimes/enums as strings, so the digest is stable across Python versions
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )  # sort_keys + tight separators = one canonical byte sequence for a given content
        return sha256_bytes(canonical.encode("utf-8"))

    def with_self_digest(
        self,
    ) -> IndexManifest:  # Returns a copy carrying its own digest, since the model is frozen
        """Return a copy of this manifest with ``manifest_sha256`` populated."""
        return self.model_copy(
            update={_SELF_DIGEST_FIELD: self.compute_self_digest()}
        )  # model_copy is the frozen-model-safe way to produce a modified instance


__all__ = ["ChunkingConfig", "EmbeddingConfig", "IndexManifest", "ManifestArtifact"]

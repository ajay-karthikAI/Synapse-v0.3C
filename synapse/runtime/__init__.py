"""
synapse.runtime
===============
The deployed runtime: one verified artifact, or nothing served.

This package is what replaces "load whatever files happen to be on disk and
trust them". The deployed container downloads exactly one archive, named by
version and pinned by SHA-256, proves it is intact and internally consistent,
and only then loads retrieval backends over it.

    synapse.runtime.config       which artifact, and where the cache lives
    synapse.runtime.objectstore  fetching it from private S3-compatible storage
    synapse.runtime.archive      safe packing and extraction; the security gate
    synapse.runtime.loader       the gate sequence, and the cache rules
    synapse.runtime.readiness    default-false readiness, and the two codes

The backends themselves live in :mod:`synapse.retrieval.native`, beside the
legacy adapters they replace, because they implement the retrieval protocols
rather than the artifact contract.

**No pickle anywhere on this path.** The corpus is ordered JSONL, the index is
FAISS, and BM25 state is rebuilt at startup rather than deserialised. The
quarantined restricted reader remains reachable from exactly one place —
``python -m synapse.cli.migrate_artifacts`` — which is an offline operator
command, not a runtime path.

**No fallback.** There is no branch that serves ``hybrid_index/`` when
verification fails. That directory has no manifest, so falling back to it would
mean answering "I cannot prove this index is intact" by serving an index nothing
can prove anything about.
"""

from __future__ import annotations  # Postponed annotations

from synapse.runtime.archive import (
    RUNTIME_MEMBERS,
    pack_archive,
    safe_extract,
    verify_archive_digest,
)
from synapse.runtime.config import (
    DEFAULT_CACHE_ROOT,
    ArtifactConfigurationError,
    RuntimeArtifactConfig,
)
from synapse.runtime.loader import LoadedArtifact, load_artifact, verify_directory
from synapse.runtime.objectstore import LocalDirectoryStore, ObjectStore, S3ObjectStore
from synapse.runtime.readiness import (
    NotReadyError,
    Readiness,
    ReadinessState,
    RuntimeBackends,
    RuntimeIndex,
)

__all__ = [
    "DEFAULT_CACHE_ROOT",
    "RUNTIME_MEMBERS",
    "ArtifactConfigurationError",
    "LoadedArtifact",
    "LocalDirectoryStore",
    "NotReadyError",
    "ObjectStore",
    "Readiness",
    "ReadinessState",
    "RuntimeArtifactConfig",
    "RuntimeBackends",
    "RuntimeIndex",
    "S3ObjectStore",
    "load_artifact",
    "pack_archive",
    "safe_extract",
    "verify_archive_digest",
    "verify_directory",
]

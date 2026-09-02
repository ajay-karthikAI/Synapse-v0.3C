"""
synapse.retrieval.index_gate
============================
Verify the index before serving from it, and fail closed when it does not.

The P1 artifact layer already knows how to verify an index
(:mod:`synapse.index.verify`): manifest self-digest, declared artifact digests,
FAISS header agreement, corpus digests. What was missing is a call to it on the
serving path. An index that fails verification is not a slow index or a stale
one — it is an index whose contents are not what its manifest says they are, and
serving from it means a citation can point at a different document than the one
that was actually read.

So: verification failure raises, the retrieval path never runs, and
``synapse.ui.errors.classify`` maps the artifact error to
``index_unverified``. No answer is generated. That is the fourth rung of the
degradation ladder, and unlike the other three it produces nothing at all.

The unmanaged case
------------------
The committed ``hybrid_index/`` was built by the legacy tree and carries no
manifest, so there is nothing to verify against. That is reported as
:attr:`IndexStatus.UNMANAGED` rather than quietly treated as success — the
distinction between "verified" and "nothing to verify" is exactly the one that
gets lost when a gate returns a bare boolean.

``require_manifest=True`` turns the unmanaged case into a failure. It is off by
default only because the production index has not been migrated yet; the
deadline for flipping it is in docs/retrieval-runtime.md §8.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from synapse.errors import ArtifactNotFoundError
from synapse.index.verify import verify_artifacts
from synapse.logging import get_logger

logger = get_logger(__name__)

MANIFEST_NAME = "manifest.json"


class IndexStatus(StrEnum):
    """The outcome of the pre-serving check."""

    VERIFIED = "verified"  # A manifest was present and every check passed
    UNMANAGED = "unmanaged"  # No manifest: nothing to verify against
    # There is no FAILED member: a failure raises. A status a caller could
    # ignore is a gate that does not gate.


@dataclass(frozen=True)
class IndexGateResult:
    """What the gate found, for the run metadata."""

    status: IndexStatus
    directory: str
    artifacts_checked: int = 0
    detail: str = ""

    def as_metadata(self) -> dict[str, object]:
        """Machine-readable form, recorded with the retrieval trace."""
        return {
            "index_status": self.status.value,
            "index_directory": self.directory,
            "artifacts_checked": self.artifacts_checked,
            "detail": self.detail,
        }


def check_index(directory: Path, *, require_manifest: bool = False) -> IndexGateResult:
    """Verify an index directory before it is used to answer anything.

    Raises:
        ArtifactNotFoundError: the directory does not exist, or a manifest is
            required and absent.
        ArtifactIntegrityError: a digest or size does not match.
        ArtifactSchemaError: the manifest or a record is malformed.
        ArtifactVersionError: the manifest is schema-incompatible.
        ArtifactCompatibilityError: the corpus and index do not correspond.

    Every one of those is raised rather than returned, deliberately: a caller
    can ignore a status field, and cannot ignore an exception.
    """
    if not directory.is_dir():
        raise ArtifactNotFoundError(path=directory, problem="index directory does not exist")

    if not (directory / MANIFEST_NAME).is_file():
        if require_manifest:
            raise ArtifactNotFoundError(path=directory, problem="index manifest is required")
        logger.warning(
            "index has no manifest; integrity was not verified",
            extra={"directory": directory.name},
        )
        return IndexGateResult(
            status=IndexStatus.UNMANAGED,
            directory=directory.name,
            detail="no manifest present; contents were not verified",
        )

    # verify_artifacts returns only on success and raises a typed
    # SynapseArtifactError otherwise, so there is no branch here in which an
    # unverified index proceeds. The exceptions propagate untouched: they are
    # already payload-free, and synapse.ui.errors.classify maps every one of
    # them to `index_unverified`.
    report = verify_artifacts(directory, deep=True)
    logger.info(
        "index verified", extra={"directory": directory.name, "artifacts": report.artifacts_checked}
    )
    return IndexGateResult(
        status=IndexStatus.VERIFIED,
        directory=directory.name,
        artifacts_checked=report.artifacts_checked,
        detail="manifest and artifact digests verified",
    )


__all__ = ["MANIFEST_NAME", "IndexGateResult", "IndexStatus", "check_index"]

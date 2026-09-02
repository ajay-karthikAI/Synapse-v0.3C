"""
synapse.governance.versioning
=============================
Semantic versioning for source packs, and the rule that a content change
invalidates a prior approval.

The governance point, which is stronger than ordinary semver:

    **Changing a pack's contents revokes its approval.**

A clinician approved a specific set of sources for a specific scope. Adding a
source, removing one, or changing the declared scope means the thing that was
approved no longer exists. Carrying the approval forward would attribute to the
reviewer a decision they never made. So :func:`bump_version` moves an approved
pack back to ``draft`` and clears the approval fields, and it does so
automatically rather than relying on an operator to remember.

Change classification:

* **MAJOR** — the scope changed (population, intended use, excluded uses, topic
  boundaries) or the reviewer requirements changed. Prior reviews were
  conducted against different terms and cannot carry over.
* **MINOR** — sources added or removed.
* **PATCH** — source metadata corrected, or review records added without
  changing the source set.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from synapse.governance.states import GovernanceError
from synapse.logging import get_logger
from synapse.schemas.enums import PackApprovalState
from synapse.schemas.source_pack import SEMVER_PATTERN, PackSource, SourcePackManifest

logger = get_logger(__name__)


class ChangeLevel(StrEnum):
    """Semantic level of a change between two pack versions."""

    NONE = "none"  # Byte-identical contents and scope
    PATCH = "patch"  # Metadata or review records changed; the source set did not
    MINOR = "minor"  # Sources added or removed
    MAJOR = "major"  # Scope or reviewer requirements changed


_LEVEL_ORDER = {
    ChangeLevel.NONE: 0,
    ChangeLevel.PATCH: 1,
    ChangeLevel.MINOR: 2,
    ChangeLevel.MAJOR: 3,
}  # For taking the maximum of several detected changes


@dataclass(frozen=True)
class VersionTriple:
    """A parsed semantic version."""

    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def bumped(self, level: ChangeLevel) -> VersionTriple:
        """Return the next version for the given change level.

        Lower components reset, per semver: a minor bump zeroes the patch, and
        a major bump zeroes both.
        """
        if level is ChangeLevel.MAJOR:
            return VersionTriple(self.major + 1, 0, 0)
        if level is ChangeLevel.MINOR:
            return VersionTriple(self.major, self.minor + 1, 0)
        if level is ChangeLevel.PATCH:
            return VersionTriple(self.major, self.minor, self.patch + 1)
        return self  # ChangeLevel.NONE leaves the version untouched


def parse_version(value: str) -> VersionTriple:
    """Parse a strict MAJOR.MINOR.PATCH version string."""
    match = SEMVER_PATTERN.match(value)
    if match is None:
        raise GovernanceError(problem="version is not a valid semantic version", value=value)
    return VersionTriple(int(match.group(1)), int(match.group(2)), int(match.group(3)))


def classify_change(
    previous_manifest: SourcePackManifest,
    previous_sources: list[PackSource],
    current_manifest: SourcePackManifest,
    current_sources: list[PackSource],
) -> ChangeLevel:
    """Determine the semantic level of the change between two pack states."""
    levels: list[ChangeLevel] = []

    # MAJOR: the terms a reviewer agreed to have changed.
    if previous_manifest.scope != current_manifest.scope:
        levels.append(ChangeLevel.MAJOR)
    if previous_manifest.reviewer_requirements != current_manifest.reviewer_requirements:
        levels.append(ChangeLevel.MAJOR)
    if previous_manifest.selection_policy != current_manifest.selection_policy:
        # The selection policy defines what "eligible" means, so changing it
        # changes what an approval asserted.
        levels.append(ChangeLevel.MAJOR)

    # MINOR: the source set changed.
    previous_ids = {s.document_id for s in previous_sources}
    current_ids = {s.document_id for s in current_sources}
    if previous_ids != current_ids:
        levels.append(ChangeLevel.MINOR)

    # PATCH: same sources, but some record differs.
    previous_by_id = {s.document_id: s for s in previous_sources}
    for source in current_sources:
        earlier = previous_by_id.get(source.document_id)
        if earlier is not None and earlier != source:
            levels.append(ChangeLevel.PATCH)
            break  # One difference is enough to establish the level

    if not levels:
        return ChangeLevel.NONE
    return max(levels, key=lambda level: _LEVEL_ORDER[level])  # The highest level wins


def bump_version(
    manifest: SourcePackManifest,
    level: ChangeLevel,
    *,
    now: datetime | None = None,
) -> SourcePackManifest:
    """Produce the next version of a manifest, revoking approval if content changed.

    The revocation is the governance mechanism. It is applied here, in the one
    function that creates a new version, so there is no path that bumps a
    version while quietly retaining an approval.
    """
    if level is ChangeLevel.NONE:
        raise GovernanceError(
            problem="no change detected; nothing to version", version=manifest.version
        )

    current = parse_version(manifest.version)
    next_version = current.bumped(level)
    timestamp = now or datetime.now(UTC)

    updates: dict[str, object] = {"version": str(next_version), "updated_at": timestamp}

    if manifest.approval_state is PackApprovalState.APPROVED:
        # A clinician approved the previous contents. Those contents no longer
        # exist, so the approval is cleared rather than carried forward.
        updates.update(
            {
                "approval_state": PackApprovalState.DRAFT,
                "approved_at": None,
                "approved_by": None,
                "review_due_at": None,
            }
        )
        logger.warning(
            "pack approval revoked by version bump",
            extra={
                "pack_id": manifest.pack_id,
                "from_version": manifest.version,
                "to_version": str(next_version),
                "level": level.value,
            },
        )

    return manifest.model_copy(update=updates)


def require_version_increase(previous: str, current: str) -> None:
    """Raise unless ``current`` is strictly greater than ``previous``.

    Used when writing a new version, so a pack cannot be republished under a
    version already in use — which would make two different contents share one
    identity, and make the changelog a fiction.
    """
    earlier, later = parse_version(previous), parse_version(current)
    if (later.major, later.minor, later.patch) <= (earlier.major, earlier.minor, earlier.patch):
        raise GovernanceError(problem="version must increase", previous=previous, current=current)


__all__ = [
    "ChangeLevel",
    "VersionTriple",
    "bump_version",
    "classify_change",
    "parse_version",
    "require_version_increase",
]

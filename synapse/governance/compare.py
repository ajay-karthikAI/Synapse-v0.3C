"""
synapse.governance.compare
==========================
Comparing two source-pack versions.

The comparison a reviewer actually needs before approving a new version is not
"what changed in the files" but "what changed in the *governance*": which
sources are new and therefore unreviewed, which approvals were lost, and
whether the scope shifted underneath the existing reviews.

So the diff is organised by governance consequence rather than by field, and
:attr:`PackDiff.requires_re_review` answers the one question directly.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass, field

from synapse.governance.versioning import ChangeLevel, classify_change
from synapse.schemas.enums import SourceLifecycleState
from synapse.schemas.source_pack import PackSource, SourcePackManifest


@dataclass(frozen=True)
class SourceChange:
    """One source that changed between versions."""

    document_id: str
    previous_state: str | None  # None when the source is newly added
    current_state: str | None  # None when the source was removed
    detail: str  # Human-readable summary of what changed


@dataclass
class PackDiff:
    """The governance-relevant difference between two pack versions."""

    previous_version: str
    current_version: str
    change_level: ChangeLevel

    added: list[SourceChange] = field(default_factory=list)
    removed: list[SourceChange] = field(default_factory=list)
    state_changed: list[SourceChange] = field(default_factory=list)
    metadata_changed: list[SourceChange] = field(default_factory=list)

    scope_changed: bool = False
    policy_changed: bool = False
    reviewer_requirements_changed: bool = False
    approval_lost: bool = False

    @property
    def requires_re_review(self) -> bool:
        """True when the changes invalidate the previous approval.

        Any scope, policy or reviewer-requirement change invalidates every
        existing review, because those reviews were conducted against different
        terms. A newly added source is unreviewed by definition.
        """
        return (
            self.scope_changed
            or self.policy_changed
            or self.reviewer_requirements_changed
            or bool(self.added)
        )

    @property
    def approvals_lost(self) -> list[SourceChange]:
        """Sources that were approved before and are not approved now."""
        return [
            c for c in self.state_changed if c.previous_state == SourceLifecycleState.APPROVED.value
        ]

    def as_dict(self) -> dict:
        """Machine-readable form, for CI and for review packets."""

        def render(changes: list[SourceChange]) -> list[dict]:
            return [
                {
                    "document_id": c.document_id,
                    "previous_state": c.previous_state,
                    "current_state": c.current_state,
                    "detail": c.detail,
                }
                for c in changes
            ]

        return {
            "previous_version": self.previous_version,
            "current_version": self.current_version,
            "change_level": self.change_level.value,
            "requires_re_review": self.requires_re_review,
            "scope_changed": self.scope_changed,
            "policy_changed": self.policy_changed,
            "reviewer_requirements_changed": self.reviewer_requirements_changed,
            "approval_lost": self.approval_lost,
            "counts": {
                "added": len(self.added),
                "removed": len(self.removed),
                "state_changed": len(self.state_changed),
                "metadata_changed": len(self.metadata_changed),
            },
            "added": render(self.added),
            "removed": render(self.removed),
            "state_changed": render(self.state_changed),
            "metadata_changed": render(self.metadata_changed),
        }

    def render(self) -> str:
        """Console rendering, ordered by governance consequence."""
        lines = [
            f"{self.previous_version} -> {self.current_version}  ({self.change_level.value})",
            f"  scope changed                : {self.scope_changed}",
            f"  selection policy changed     : {self.policy_changed}",
            f"  reviewer requirements changed: {self.reviewer_requirements_changed}",
            f"  pack approval lost           : {self.approval_lost}",
            f"  sources added                : {len(self.added)}",
            f"  sources removed              : {len(self.removed)}",
            f"  lifecycle states changed     : {len(self.state_changed)}",
            f"  metadata changed             : {len(self.metadata_changed)}",
            f"  REQUIRES RE-REVIEW           : {self.requires_re_review}",
        ]
        if (
            self.approvals_lost
        ):  # Called out separately: losing an approval is the most consequential single change
            lines.append(f"  approvals lost               : {len(self.approvals_lost)}")
            lines.extend(f"      {c.document_id}: {c.detail}" for c in self.approvals_lost)
        return "\n".join(lines)


def diff_packs(
    previous_manifest: SourcePackManifest,
    previous_sources: list[PackSource],
    current_manifest: SourcePackManifest,
    current_sources: list[PackSource],
) -> PackDiff:
    """Compare two pack versions."""
    previous_by_id = {s.document_id: s for s in previous_sources}
    current_by_id = {s.document_id: s for s in current_sources}

    diff = PackDiff(
        previous_version=previous_manifest.version,
        current_version=current_manifest.version,
        change_level=classify_change(
            previous_manifest, previous_sources, current_manifest, current_sources
        ),
        scope_changed=previous_manifest.scope != current_manifest.scope,
        policy_changed=previous_manifest.selection_policy != current_manifest.selection_policy,
        reviewer_requirements_changed=previous_manifest.reviewer_requirements
        != current_manifest.reviewer_requirements,
        approval_lost=(
            previous_manifest.approval_state.value == "approved"
            and current_manifest.approval_state.value != "approved"
        ),
    )

    for document_id, source in current_by_id.items():
        if document_id not in previous_by_id:  # Newly added: unreviewed by definition
            diff.added.append(
                SourceChange(
                    document_id,
                    None,
                    source.lifecycle_state.value,
                    "added to pack; requires review",
                )
            )

    for document_id, source in previous_by_id.items():
        if (
            document_id not in current_by_id
        ):  # Removed: any approval it carried no longer applies to anything
            diff.removed.append(
                SourceChange(document_id, source.lifecycle_state.value, None, "removed from pack")
            )

    for document_id, current in current_by_id.items():
        earlier = previous_by_id.get(document_id)
        if earlier is None:
            continue
        if earlier.lifecycle_state is not current.lifecycle_state:
            diff.state_changed.append(
                SourceChange(
                    document_id,
                    earlier.lifecycle_state.value,
                    current.lifecycle_state.value,
                    f"{earlier.lifecycle_state.value} -> {current.lifecycle_state.value}",
                )
            )
        elif earlier != current:
            # Same state, different record. Content-hash changes matter most:
            # they mean the underlying source text changed after review.
            detail = (
                "content hash changed after review"
                if earlier.content_sha256 != current.content_sha256
                else "metadata changed"
            )
            diff.metadata_changed.append(
                SourceChange(
                    document_id,
                    earlier.lifecycle_state.value,
                    current.lifecycle_state.value,
                    detail,
                )
            )

    # Sorted so two runs over the same inputs produce byte-identical output.
    diff.added.sort(key=lambda c: c.document_id)
    diff.removed.sort(key=lambda c: c.document_id)
    diff.state_changed.sort(key=lambda c: c.document_id)
    diff.metadata_changed.sort(key=lambda c: c.document_id)
    return diff


__all__ = ["PackDiff", "SourceChange", "diff_packs"]

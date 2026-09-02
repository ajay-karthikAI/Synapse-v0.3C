"""
synapse.governance.eligibility
==============================
Which sources may enter a production index, and the pack↔index hash agreement
check that must pass before traffic is served.

Requirement 5, stated as code:

* expired, rejected, retracted and superseded sources are excluded **by
  default** — the default is restrictive, and widening it is an explicit,
  recorded act;
* a pack and the index built from it must agree on their hashes before serving.

The eligibility policy is a value object rather than a set of flags scattered
across callers, so that whatever policy was applied gets *recorded* into the
index manifest and the build report. An index that cannot say which policy
produced it is an index nobody can audit.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass
from datetime import date

from synapse.governance.states import GovernanceError
from synapse.logging import get_logger
from synapse.schemas.enums import PackApprovalState, RetractionStatus, SourceLifecycleState
from synapse.schemas.index import IndexManifest
from synapse.schemas.source_pack import PackSource, SourcePackManifest

logger = get_logger(__name__)

# Lifecycle states excluded from a production index by default. Note that
# DISCOVERED and SCREENED are here too: automated discovery and human triage
# are both short of approval, so neither licenses patient-facing use.
DEFAULT_EXCLUDED_STATES: frozenset[SourceLifecycleState] = frozenset(
    {
        SourceLifecycleState.DISCOVERED,
        SourceLifecycleState.SCREENED,
        SourceLifecycleState.REJECTED,
        SourceLifecycleState.EXPIRED,
        SourceLifecycleState.SUPERSEDED,
    }
)

# Retraction states excluded regardless of lifecycle state. A source can be
# approved and later retracted; the retraction wins.
EXCLUDED_RETRACTION_STATES: frozenset[RetractionStatus] = frozenset(
    {RetractionStatus.RETRACTED, RetractionStatus.EXPRESSION_OF_CONCERN}
)


@dataclass(frozen=True)
class EligibilityPolicy:
    """What a source must satisfy to enter a production index.

    Defaults are the restrictive ones. ``allow_unapproved`` exists for local
    experimentation and is recorded into the index manifest whenever it is set,
    so an index built with it can never be mistaken for a governed one.
    """

    require_approved: bool = True  # Only APPROVED sources are eligible
    exclude_retracted: bool = True  # Retracted and expression-of-concern sources are excluded
    honour_expiry: bool = True  # An approval past its review-due date stops being eligible
    allow_example_packs: bool = False  # unapproved_example packs never reach production
    allow_unapproved: bool = (
        False  # ESCAPE HATCH for local experimentation only; recorded wherever it is used
    )

    def describe(self) -> str:
        """One-line description, recorded into manifests and reports."""
        if (
            self.allow_unapproved
        ):  # Deliberately alarming wording: this must stand out in a manifest
            return "UNGOVERNED: unapproved sources permitted (local experimentation only)"
        parts = ["approved-only" if self.require_approved else "any-state"]
        if self.exclude_retracted:
            parts.append("retracted-excluded")
        if self.honour_expiry:
            parts.append("expiry-honoured")
        return "; ".join(parts)


@dataclass(frozen=True)
class EligibilityDecision:
    """Why one source was or was not admitted."""

    document_id: str
    eligible: bool
    reason: str  # Machine-readable reason code


def evaluate_source(
    source: PackSource, policy: EligibilityPolicy, *, as_of: date
) -> EligibilityDecision:
    """Decide whether one source may enter a production index.

    Checks run cheapest-and-most-decisive first, and the first failure wins, so
    the reason code names the *primary* disqualifier rather than an incidental
    one.
    """
    if policy.exclude_retracted and source.retraction_status in EXCLUDED_RETRACTION_STATES:
        # Checked before lifecycle state: a retracted source is excluded even
        # if some reviewer approved it before the retraction was published.
        return EligibilityDecision(
            source.document_id, False, f"retracted:{source.retraction_status.value}"
        )

    if policy.allow_unapproved:  # Escape hatch, evaluated only after the retraction guard
        return EligibilityDecision(source.document_id, True, "ungoverned_override")

    if policy.require_approved and source.lifecycle_state is not SourceLifecycleState.APPROVED:
        return EligibilityDecision(
            source.document_id, False, f"not_approved:{source.lifecycle_state.value}"
        )

    if source.lifecycle_state in DEFAULT_EXCLUDED_STATES:
        return EligibilityDecision(
            source.document_id, False, f"excluded_state:{source.lifecycle_state.value}"
        )

    if policy.honour_expiry and source.review_expired_on(as_of):
        # An approval that has passed its review-due date is not an approval.
        return EligibilityDecision(source.document_id, False, "approval_expired")

    return EligibilityDecision(source.document_id, True, "approved")


def select_eligible(
    sources: list[PackSource],
    manifest: SourcePackManifest,
    policy: EligibilityPolicy | None = None,
    *,
    as_of: date,
) -> tuple[list[PackSource], list[EligibilityDecision]]:
    """Filter sources to those eligible for a production index.

    Returns the eligible sources and a decision for **every** source, so a
    build report can explain each exclusion rather than only reporting a count.

    Raises:
        GovernanceError: the pack itself is not in a state that permits an index.
    """
    policy = policy or EligibilityPolicy()

    if (
        manifest.approval_state is PackApprovalState.UNAPPROVED_EXAMPLE
        and not policy.allow_example_packs
    ):
        # A demonstration pack must never produce a servable index, whatever
        # its sources happen to say.
        raise GovernanceError(
            problem="pack is an unapproved example and cannot produce a production index",
            pack_id=manifest.pack_id,
        )
    if policy.require_approved and manifest.approval_state is not PackApprovalState.APPROVED:
        raise GovernanceError(
            problem="pack is not approved; no production index may be built from it",
            pack_id=manifest.pack_id,
            approval_state=manifest.approval_state.value,
        )

    decisions = [evaluate_source(source, policy, as_of=as_of) for source in sources]
    eligible_ids = {d.document_id for d in decisions if d.eligible}
    eligible = [s for s in sources if s.document_id in eligible_ids]

    logger.info(
        "eligibility evaluated",
        extra={
            "pack_id": manifest.pack_id,
            "total": len(sources),
            "eligible": len(eligible),
            "policy": policy.describe(),
        },
    )
    return eligible, decisions


def require_pack_index_agreement(manifest: SourcePackManifest, index: IndexManifest) -> None:
    """Verify a pack and an index describe the same governed content.

    Requirement 5's final rule. Two independent checks, because they catch
    different mistakes:

    * the index must name the pack version it was built from — an index that
      cannot say which governance decision authorised it is unservable;
    * the pack's corpus digest recorded in the index must match the pack's
      current digest — otherwise the pack changed after the index was built,
      and the index is serving content that the current pack does not govern.

    Raises:
        GovernanceError: the pack and index disagree.
    """
    expected_version = f"{manifest.pack_id}@{manifest.version}"
    if index.source_pack_version != expected_version:
        raise GovernanceError(
            problem="index was not built from this source pack version",
            expected=expected_version,
            found=index.source_pack_version or "<none>",
        )
    if index.source_pack_sha256 is None:
        raise GovernanceError(
            problem="index manifest records no source-pack digest and cannot be verified against a pack",
            index_id=index.index_id,
        )
    if index.source_pack_sha256 != manifest.corpus_sha256:
        raise GovernanceError(
            problem="source pack changed after the index was built; rebuild the index before serving",
            pack_id=manifest.pack_id,
        )


__all__ = [
    "DEFAULT_EXCLUDED_STATES",
    "EXCLUDED_RETRACTION_STATES",
    "EligibilityDecision",
    "EligibilityPolicy",
    "evaluate_source",
    "require_pack_index_agreement",
    "select_eligible",
]

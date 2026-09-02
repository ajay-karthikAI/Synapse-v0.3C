"""
synapse.governance.review
=========================
Recording a human review, and importing automated discoveries.

Two functions, deliberately kept in one module because they are the two halves
of the separation this whole subsystem exists to enforce:

* :func:`import_discovered` is the **only** way automation adds sources, and it
  hard-codes ``DISCOVERED``. It takes no lifecycle-state parameter, so there is
  no argument an over-eager caller could pass to skip review.
* :func:`record_review` is the **only** way a source advances past discovery,
  and it requires a fully-populated :class:`ClinicianReview` whose reviewer
  identifier is supplied by the operator.

**No function in this module generates a reviewer identity.** Reviewer IDs are
pseudonyms issued out-of-band and typed in by an operator. If Synapse minted
them, a review record would prove only that this software ran.
"""

from __future__ import annotations  # Postponed annotations

from datetime import UTC, date, datetime, timedelta

from synapse.governance.states import (
    GovernanceError,
    TransitionActor,
    require_automation_creatable,
    require_transition,
)
from synapse.logging import get_logger
from synapse.schemas.enums import ReviewDecision, SourceLifecycleState
from synapse.schemas.source import SourceDocument
from synapse.schemas.source_pack import ClinicianReview, PackSource, SourcePackManifest

logger = get_logger(__name__)

# Decision → the lifecycle state it produces. A review's outcome determines the
# state; a caller cannot choose a state independently of the decision, which is
# what stops a "needs changes" review from being filed as an approval.
_DECISION_TO_STATE = {
    ReviewDecision.APPROVED: SourceLifecycleState.APPROVED,
    ReviewDecision.REJECTED: SourceLifecycleState.REJECTED,
    ReviewDecision.NEEDS_CHANGES: SourceLifecycleState.SCREENED,  # Triaged as in-scope, but not approved
}


def import_discovered(
    documents: list[SourceDocument],
    *,
    discovery_method: str,
    existing: list[PackSource] | None = None,
) -> tuple[list[PackSource], list[str]]:
    """Convert ingested corpus documents into ``discovered`` pack sources.

    This is the automation boundary. Note what the signature does *not* accept:
    there is no ``lifecycle_state`` parameter and no ``review`` parameter, so
    the automated path is structurally incapable of producing anything but a
    discovered entry.

    Sources already present in the pack are skipped rather than overwritten —
    re-running discovery must never reset a completed review back to discovered.

    Returns:
        The new pack sources, and the document IDs skipped as already present.
    """
    require_automation_creatable(
        SourceLifecycleState.DISCOVERED
    )  # Guard against a future edit widening what automation may create

    already_present = {s.document_id for s in (existing or [])}
    created: list[PackSource] = []
    skipped: list[str] = []

    for document in documents:
        if document.document_id in already_present:
            # Idempotence with teeth: re-importing must not clobber a review.
            skipped.append(document.document_id)
            continue
        created.append(
            PackSource(
                document_id=document.document_id,
                canonical_url=document.source_url,
                pmid=document.identifiers.pmid,
                doi=document.identifiers.doi,
                guideline_id=document.identifiers.guideline_id,
                title=document.title,
                authors=list(document.authors),
                container=document.container,
                publication_date=document.publication_date,
                revision_date=document.revision_date,
                evidence_type=document.evidence_type,
                language=document.language,
                lifecycle_state=SourceLifecycleState.DISCOVERED,  # Hard-coded, never a parameter
                review=None,  # Automation never produces a review record
                retraction_status=document.retraction_status,
                content_sha256=document.content_sha256,
                discovered_at=document.retrieved_at,
                discovery_method=discovery_method,
            )
        )

    logger.info(
        "discovered sources imported",
        extra={
            "created_count": len(created),
            "skipped_count": len(skipped),
            "method": discovery_method,
        },
    )
    return created, skipped


def default_review_due(reviewed_at: datetime, manifest: SourcePackManifest) -> date:
    """Compute a review-due date from the pack's review interval.

    Derived from pack policy rather than hard-coded, so a pack that requires
    six-monthly review gets six-monthly expiry without a second setting to
    forget.
    """
    return (
        reviewed_at + timedelta(days=manifest.reviewer_requirements.review_interval_days)
    ).date()


def record_review(
    source: PackSource,
    review: ClinicianReview,
    *,
    manifest: SourcePackManifest,
    exclusion_reason: str | None = None,
    now: datetime | None = None,
) -> PackSource:
    """Apply a human review to a source, advancing its lifecycle state.

    The state is derived from the review's decision, not chosen by the caller.
    The transition is then validated against the table in
    :mod:`synapse.governance.states` as a ``HUMAN_REVIEW`` actor.

    Raises:
        GovernanceError: the reviewer does not meet the pack's requirements, the
            transition is not permitted, or a rejection carries no reason.
    """
    del now  # Timestamps come from the review record itself, which the operator authored

    requirements = manifest.reviewer_requirements
    if review.reviewer_role is not requirements.required_role:
        # Checked here as well as in pack validation, so an unqualified review
        # is refused at the moment it is filed rather than discovered later.
        raise GovernanceError(
            problem="reviewer role does not meet the pack's requirements",
            required=requirements.required_role.value,
            supplied=review.reviewer_role.value,
        )
    if review.rubric_version != requirements.rubric_version:
        raise GovernanceError(
            problem="review rubric version does not match the pack's requirement",
            required=requirements.rubric_version,
            supplied=review.rubric_version,
        )

    target_state = _DECISION_TO_STATE[review.decision]
    require_transition(source.lifecycle_state, target_state, actor=TransitionActor.HUMAN_REVIEW)

    if target_state is SourceLifecycleState.REJECTED and not exclusion_reason:
        # A rejection without a reason cannot be revisited or audited, so it is
        # refused rather than stored as an unexplained exclusion.
        raise GovernanceError(
            problem="a rejection requires an exclusion_reason", document_id=source.document_id
        )

    updated = source.model_copy(
        update={
            "lifecycle_state": target_state,
            "review": review,
            "exclusion_reason": exclusion_reason
            if target_state is SourceLifecycleState.REJECTED
            else source.exclusion_reason,
        }
    )
    logger.info(
        "review recorded",
        extra={
            "document_id": source.document_id,
            "decision": review.decision.value,
            "state": target_state.value,
            "reviewer_id": review.reviewer_id,
        },
    )
    return updated


def expire_overdue(
    sources: list[PackSource], *, as_of: date | None = None
) -> tuple[list[PackSource], list[str]]:
    """Move approvals past their review-due date into ``expired``.

    A machine-applied transition: the passage of a date is a fact, not a
    judgement, so automation is permitted to record it. What automation may not
    do is the reverse — re-approving an expired source requires a fresh review.

    Returns the updated source list and the identifiers that were expired.
    """
    today = as_of or datetime.now(UTC).date()
    updated: list[PackSource] = []
    expired_ids: list[str] = []

    for source in sources:
        if source.lifecycle_state is SourceLifecycleState.APPROVED and source.review_expired_on(
            today
        ):
            require_transition(
                source.lifecycle_state,
                SourceLifecycleState.EXPIRED,
                actor=TransitionActor.AUTOMATION,
            )
            due = source.review.review_due_at.isoformat() if source.review else "unknown"
            updated.append(
                source.model_copy(
                    update={
                        "lifecycle_state": SourceLifecycleState.EXPIRED,
                        "exclusion_reason": f"approval expired on {due}; requires re-review",  # Reason is mandatory for expired, and states the date
                    }
                )
            )
            expired_ids.append(source.document_id)
        else:
            updated.append(source)

    if expired_ids:
        logger.warning("approvals expired", extra={"count": len(expired_ids)})
    return updated, expired_ids


__all__ = ["default_review_due", "expire_overdue", "import_discovered", "record_review"]
